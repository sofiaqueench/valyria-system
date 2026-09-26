"""Guild-scoped Staff rank configuration, XP progress, and automatic promotions."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import discord
from sqlalchemy import func, select

from bot.core.logger import logger
from bot.database.engine import async_session
from bot.database.models.staff_promotion import StaffPromotionState
from bot.database.models.verification import StaffXPHistory
from bot.services import config_service
from bot.services.logging_service import LogEvent, LogType, SendResult, send_log

RANK_COUNT = 11
RANK_TYPES = {"staff", "high_staff"}
_PROMOTION_LOCKS: dict[tuple[int, int], asyncio.Lock] = {}
_INCOMPLETE_WARNED: set[int] = set()
_HIERARCHY_WARNED: set[tuple[int, int]] = set()
_MISSING_ROLE_WARNED: set[tuple[int, int, int]] = set()


@dataclass(frozen=True)
class StaffRank:
    rank: int
    role_id: int
    rank_type: str
    required_xp: int


def parse_rank_config(raw: str | None) -> dict[int, StaffRank]:
    try:
        decoded = json.loads(raw or "{}")
        if not isinstance(decoded, dict):
            raise ValueError("rank configuration must be an object")
        ranks: dict[int, StaffRank] = {}
        for rank_text, value in decoded.items():
            rank = int(rank_text)
            if not isinstance(value, dict):
                raise ValueError(f"rank {rank} must be an object")
            role_id = int(value["role_id"])
            rank_type = str(value["type"]).lower()
            required_xp = int(value["required_xp"])
            if not 1 <= rank <= RANK_COUNT or role_id <= 0 or required_xp <= 0 or rank_type not in RANK_TYPES:
                raise ValueError(f"invalid values for rank {rank}")
            ranks[rank] = StaffRank(rank, role_id, rank_type, required_xp)
        _validate_order(ranks)
        if len({rank.role_id for rank in ranks.values()}) != len(ranks):
            raise ValueError("each configured rank must use a different role")
        return ranks
    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid Staff rank configuration: {exc}") from exc


def _validate_order(ranks: dict[int, StaffRank]) -> None:
    ordered = [ranks[number] for number in sorted(ranks)]
    for previous, current in zip(ordered, ordered[1:]):
        if current.required_xp <= previous.required_xp:
            raise ValueError("Promotion XP must increase with each rank.")
        if previous.rank_type == "high_staff" and current.rank_type != "high_staff":
            raise ValueError("High Staff ranks must follow all Staff ranks.")


async def get_rank_config(guild_id: int) -> dict[int, StaffRank]:
    raw = await config_service.get_setting(guild_id, "staff_rank_config")
    try:
        return parse_rank_config(str(raw or "{}"))
    except ValueError:
        logger.exception("Invalid Staff rank configuration for guild %s.", guild_id)
        return {}


async def set_rank_config(
    guild_id: int, rank: int, role_id: int, rank_type: str, required_xp: int,
    *, updated_by: int,
) -> dict[int, StaffRank]:
    if not 1 <= rank <= RANK_COUNT:
        raise ValueError("Rank number must be between 1 and 11.")
    if role_id <= 0 or required_xp <= 0:
        raise ValueError("Role and required XP must be valid; XP must be positive.")
    rank_type = rank_type.lower()
    if rank_type not in RANK_TYPES:
        raise ValueError("Rank type must be STAFF or HIGH_STAFF.")

    current = await get_rank_config(guild_id)
    current[rank] = StaffRank(rank, role_id, rank_type, required_xp)
    if len({item.role_id for item in current.values()}) != len(current):
        raise ValueError("Each configured rank must use a different role.")
    try:
        _validate_order(current)
    except ValueError as exc:
        if "Promotion XP" in str(exc):
            raise ValueError("⚠️ Promotion XP must increase with each rank.") from exc
        raise

    stored = json.dumps({
        str(number): {
            "role_id": item.role_id,
            "type": item.rank_type,
            "required_xp": item.required_xp,
        }
        for number, item in sorted(current.items())
    }, separators=(",", ":"))
    await config_service.set_setting(guild_id, "staff_rank_config", stored, updated_by=updated_by)
    return current


async def remove_rank_config(guild_id: int, rank: int, *, updated_by: int) -> dict[int, StaffRank]:
    current = await get_rank_config(guild_id)
    if rank not in current:
        raise ValueError(f"Rank {rank} is not configured.")
    del current[rank]
    stored = json.dumps({
        str(number): {
            "role_id": item.role_id,
            "type": item.rank_type,
            "required_xp": item.required_xp,
        }
        for number, item in sorted(current.items())
    }, separators=(",", ":"))
    await config_service.set_setting(guild_id, "staff_rank_config", stored, updated_by=updated_by)
    return current


def is_rank_config_complete(ranks: dict[int, StaffRank]) -> bool:
    return set(ranks) == set(range(1, RANK_COUNT + 1))


async def get_staff_xp_total(guild_id: int, user_id: int) -> int:
    async with async_session() as session:
        total = await session.scalar(select(func.coalesce(func.sum(StaffXPHistory.xp_amount), 0)).where(
            StaffXPHistory.guild_id == guild_id,
            StaffXPHistory.staff_id == user_id,
        ))
    return int(total or 0)


async def get_promotion_state(guild_id: int, user_id: int) -> StaffPromotionState | None:
    async with async_session() as session:
        return await session.get(StaffPromotionState, (guild_id, user_id))


async def _save_promotion_state(
    member: discord.Member, rank: StaffRank, xp: int, source: str,
) -> None:
    async with async_session() as session:
        async with session.begin():
            state = await session.get(StaffPromotionState, (member.guild.id, member.id))
            if state is None:
                session.add(StaffPromotionState(
                    guild_id=member.guild.id, user_id=member.id, rank=rank.rank,
                    role_id=rank.role_id, rank_type=rank.rank_type,
                    xp_at_promotion=xp, source=source[:40],
                ))
            else:
                state.rank = rank.rank
                state.role_id = rank.role_id
                state.rank_type = rank.rank_type
                state.xp_at_promotion = xp
                state.source = source[:40]
                state.promoted_at = discord.utils.utcnow()


async def on_staff_xp_awarded(member: discord.Member, *, amount: int, source: str) -> None:
    """Reconcile rank after a committed award; XP itself remains in the existing ledger."""
    key = (member.guild.id, member.id)
    async with _PROMOTION_LOCKS.setdefault(key, asyncio.Lock()):
        ranks = await get_rank_config(member.guild.id)
        if not is_rank_config_complete(ranks):
            if member.guild.id not in _INCOMPLETE_WARNED:
                _INCOMPLETE_WARNED.add(member.guild.id)
                missing = sorted(set(range(1, RANK_COUNT + 1)) - set(ranks))
                logger.warning(
                    "Staff promotion is inactive in guild %s; configure ranks %s of 1–11.",
                    member.guild.id, ", ".join(map(str, missing)),
                )
            return
        missing_roles = [item for item in ranks.values() if member.guild.get_role(item.role_id) is None]
        if missing_roles:
            for item in missing_roles:
                warn_key = (member.guild.id, item.rank, item.role_id)
                if warn_key not in _MISSING_ROLE_WARNED:
                    _MISSING_ROLE_WARNED.add(warn_key)
                    logger.warning(
                        "Staff promotion is inactive in guild %s; configured role for rank %s (%s) is missing.",
                        member.guild.id, item.rank, item.role_id,
                    )
            return

        from bot.core.permissions import StaffLevel, get_staff_level
        try:
            if await get_staff_level(member) < StaffLevel.STAFF:
                return
        except Exception:
            logger.exception("Could not check Staff eligibility for member %s.", member.id)
            return

        xp = await get_staff_xp_total(member.guild.id, member.id)
        earned = [rank for rank in ranks.values() if xp >= rank.required_xp]
        if not earned:
            return
        target = max(earned, key=lambda item: item.rank)
        rank_by_role = {item.role_id: item for item in ranks.values()}
        actual_ranks = [rank_by_role[role.id] for role in member.roles if role.id in rank_by_role]
        actual = max(actual_ranks, key=lambda item: item.rank) if actual_ranks else None
        state = await get_promotion_state(member.guild.id, member.id)
        # A rank-0 state only preserves Staff eligibility after XP loss; it is not
        # a previously held promotion rank and must not appear as one on promotion.
        if state is not None and state.rank <= 0:
            state = None

        # A manually removed managed role can be restored on a later Staff XP/status update.
        # An already-held rank at or above the earned threshold is never demoted.
        current_rank = max(actual.rank if actual else 0, state.rank if state else 0)
        if target.rank < current_rank:
            return
        is_new_promotion = target.rank > current_rank
        if (
            target.rank == current_rank and actual is not None
            and actual.rank == target.rank and len(actual_ranks) <= 1
        ):
            if state is None or state.rank != target.rank or state.role_id != target.role_id:
                await _save_promotion_state(member, target, xp, source)
            return

        target_role = member.guild.get_role(target.role_id)
        if target_role is None:
            logger.warning("Configured Staff rank %s role %s is missing in guild %s.", target.rank, target.role_id, member.guild.id)
            return
        old_roles = [role for role in member.roles if role.id in rank_by_role]
        bot_member = member.guild.me
        changing = [*old_roles, target_role]
        if bot_member is None or not bot_member.guild_permissions.manage_roles:
            logger.warning("Cannot promote member %s in guild %s: bot lacks Manage Roles.", member.id, member.guild.id)
            return
        for role in changing:
            if bot_member.top_role <= role:
                warn_key = (member.guild.id, role.id)
                if warn_key not in _HIERARCHY_WARNED:
                    _HIERARCHY_WARNED.add(warn_key)
                    logger.warning(
                        "Cannot promote member %s in guild %s: Staff rank role %s is at or above the bot's highest role.",
                        member.id, member.guild.id, role.id,
                    )
                return

        desired_roles = [role for role in member.roles if role.id not in rank_by_role]
        if all(role.id != target_role.id for role in desired_roles):
            desired_roles.append(target_role)
        previous = actual if actual and actual.rank == current_rank else state
        previous_role_id = previous.role_id if previous else None
        old_xp = max(0, xp - amount)
        try:
            await member.edit(roles=desired_roles, reason=f"Staff XP promotion to rank {target.rank}")
        except discord.Forbidden:
            logger.warning("Discord refused Staff rank promotion for member %s in guild %s.", member.id, member.guild.id)
            return
        except discord.HTTPException:
            logger.exception("Could not apply Staff rank %s to member %s.", target.rank, member.id)
            return

        try:
            await _save_promotion_state(member, target, xp, source)
        except Exception:
            logger.exception("Staff role changed but promotion state could not be saved for member %s.", member.id)
            return

        if is_new_promotion:
            await _announce_promotion(
                member, previous_role_id=previous_role_id, target=target,
                previous_rank_number=previous.rank if previous else None,
                previous_rank_type=previous.rank_type if previous else None,
                old_xp=old_xp, xp=xp, source=source,
            )


async def _announce_promotion(
    member: discord.Member, *, previous_role_id: int | None, target: StaffRank,
    previous_rank_number: int | None, previous_rank_type: str | None,
    old_xp: int, xp: int, source: str,
) -> None:
    guild = member.guild
    channel_key = "high_staff_promotion_channel" if target.rank_type == "high_staff" else "staff_promotion_channel"
    try:
        channel_id = await config_service.get_setting(guild.id, channel_key)
    except Exception:
        logger.exception("Could not read Staff promotion channel setting %s in guild %s.", channel_key, guild.id)
        channel_id = None
    channel = guild.get_channel(channel_id) if channel_id else None
    title = "👑 HIGH STAFF PROMOTION" if target.rank_type == "high_staff" else "🎉 STAFF PROMOTION"
    new_role = guild.get_role(target.role_id)
    previous_role = guild.get_role(previous_role_id) if previous_role_id else None
    if previous_rank_type == "staff" and target.rank_type == "high_staff":
        rank_type = "STAFF → HIGH STAFF"
    else:
        rank_type = "HIGH STAFF" if target.rank_type == "high_staff" else "STAFF"
    try:
        await send_log(guild, LogEvent(
            LogType.XP, title, user=member,
            fields=[
                ("Previous Rank", f"Rank {previous_rank_number} · {previous_role.mention}" if previous_rank_number is not None and previous_role else "No previous configured rank", True),
                ("New Rank", f"Rank {target.rank} · {new_role.mention}" if new_role else f"Rank {target.rank}", True),
                ("Promotion Type", rank_type, True),
                ("Staff XP", f"{old_xp:,} → {xp:,}", True),
                ("Source", source, True),
            ],
            ids={"Guild ID": guild.id, "Member ID": member.id, "Previous Rank": previous_rank_number or "none", "Previous Role ID": previous_role_id or "none", "New Rank": target.rank, "New Role ID": target.role_id},
        ))
    except Exception:
        logger.exception("Could not write Staff promotion log for member %s.", member.id)
    if not isinstance(channel, discord.TextChannel):
        logger.warning("Staff promotion announcement skipped: %s is not configured as a text channel in guild %s.", channel_key, guild.id)
        return

    embed = discord.Embed(
        title=title,
        description=f"Congratulations {member.mention}! You have been promoted to {new_role.mention if new_role else f'Rank {target.rank}' }.",
        color=discord.Color.gold() if target.rank_type == "high_staff" else discord.Color.blurple(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="New Rank", value=new_role.mention if new_role else f"Rank {target.rank}", inline=True)
    embed.add_field(name="Staff XP", value=f"{xp:,} XP", inline=True)
    if previous_role is not None:
        embed.add_field(name="Previous Rank", value=f"Rank {previous_rank_number} · {previous_role.mention}", inline=True)
    embed.set_footer(text="Valyria • Staff Progression")
    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions(users=True, roles=True))
    except discord.Forbidden:
        logger.warning("Cannot send Staff promotion announcement in channel %s (guild %s).", channel.id, guild.id)
    except discord.HTTPException:
        logger.exception("Could not send Staff promotion announcement for member %s.", member.id)
