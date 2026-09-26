"""Staff warnings that apply penalties through the existing Staff XP ledger and rank state."""
from __future__ import annotations

import discord
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from bot.core.logger import logger
from bot.database.engine import async_session
from bot.database.models.staff_promotion import StaffPromotionState
from bot.database.models.staff_warning import StaffWarning
from bot.database.models.verification import StaffXPHistory
from bot.services import config_service
from bot.services.logging_service import LogEvent, LogType, send_log
from bot.services.staff_promotion_service import (
    StaffRank, get_promotion_state, get_rank_config,
    is_rank_config_complete,
)
from bot.services.staff_xp_service import get_staff_xp_mutation_lock


async def can_warn(moderator: discord.Member, target: discord.Member, ranks: dict[int, StaffRank]) -> bool:
    from bot.core.permissions import StaffLevel, get_staff_level

    if moderator.id == target.id or moderator.guild.id != target.guild.id:
        return False
    moderator_level = await get_staff_level(moderator)
    target_level = await get_staff_level(target)
    if target_level < StaffLevel.STAFF:
        return False
    if moderator_level > target_level:
        return True
    if moderator_level != StaffLevel.STAFF or target_level != StaffLevel.STAFF:
        return False
    rank_by_role = {rank.role_id: rank.rank for rank in ranks.values()}
    mod_rank = max((rank_by_role[role.id] for role in moderator.roles if role.id in rank_by_role), default=0)
    target_rank = max((rank_by_role[role.id] for role in target.roles if role.id in rank_by_role), default=0)
    return mod_rank > 0 and target_rank > 0 and mod_rank > target_rank


async def issue_staff_warning(
    target: discord.Member, moderator: discord.Member, *, reason: str,
    penalty: int, interaction_id: int,
) -> dict[str, object]:
    if target.bot:
        raise ValueError("Bots cannot receive Staff warnings.")
    if penalty < 0:
        raise ValueError("The Staff Warn penalty cannot be negative.")
    guild = target.guild
    if moderator.guild.id != guild.id:
        raise ValueError("Moderator and target must belong to this server.")
    if not reason.strip():
        raise ValueError("A reason is required for a Staff warning.")

    async with get_staff_xp_mutation_lock(guild.id, target.id):
        ranks = await get_rank_config(guild.id)
        if not await can_warn(moderator, target, ranks):
            raise PermissionError("You cannot warn this Staff member because your Staff rank is not higher.")
        async with async_session() as session:
            if await session.scalar(select(StaffWarning.id).where(StaffWarning.interaction_id == interaction_id)):
                raise ValueError("This Staff warning was already recorded.")
            xp_before = int(await session.scalar(select(func.coalesce(func.sum(StaffXPHistory.xp_amount), 0)).where(
                StaffXPHistory.guild_id == guild.id, StaffXPHistory.staff_id == target.id,
            )) or 0)
        applied_penalty = min(penalty, xp_before)
        xp_after = max(0, xp_before - penalty)

        rank_by_role = {item.role_id: item for item in ranks.values()}
        original_roles = list(target.roles)
        held_rank = max((rank_by_role[role.id] for role in target.roles if role.id in rank_by_role), key=lambda item: item.rank, default=None)
        previous_state = await get_promotion_state(guild.id, target.id)
        previous = held_rank or (ranks.get(previous_state.rank) if previous_state else None)
        config_active = is_rank_config_complete(ranks) and all(guild.get_role(item.role_id) is not None for item in ranks.values())
        earned = [item for item in ranks.values() if xp_after >= item.required_xp] if config_active else []
        current = max(earned, key=lambda item: item.rank) if earned else None
        rank_changed = config_active and (previous.rank if previous else 0) != (current.rank if current else 0)

        # Apply the Discord role update before persisting the new rank state. A failed role
        # update leaves the warning and XP intact only if no rank update was required.
        if rank_changed:
            managed_ids = set(rank_by_role)
            desired = [role for role in target.roles if role.id not in managed_ids]
            if current is not None:
                desired.append(guild.get_role(current.role_id))
            bot_member = guild.me
            if bot_member is None or not bot_member.guild_permissions.manage_roles:
                raise RuntimeError("The bot needs Manage Roles to update the Staff rank.")
            for rank in (previous, current):
                role = guild.get_role(rank.role_id) if rank else None
                if role is not None and bot_member.top_role <= role:
                    raise RuntimeError("The bot's highest role must be above the Staff rank role.")
            try:
                await target.edit(roles=desired, reason=f"Staff Warn by {moderator} (XP penalty)")
            except (discord.Forbidden, discord.HTTPException) as exc:
                raise RuntimeError("Discord could not update the Staff rank role; the warning and XP penalty were not saved.") from exc

        try:
            async with async_session() as session:
                async with session.begin():
                    session.add(StaffWarning(
                        guild_id=guild.id, staff_user_id=target.id, moderator_id=moderator.id,
                        reason=reason[:2000], xp_penalty=applied_penalty, interaction_id=interaction_id,
                    ))
                    await session.flush()
                    session.add(StaffXPHistory(
                        guild_id=guild.id, staff_id=target.id, target_id=target.id,
                        action="staff_warn", xp_amount=-applied_penalty,
                    ))
                    if config_active:
                        state = await session.get(StaffPromotionState, (guild.id, target.id))
                        if current is None:
                            if state is None and previous is not None:
                                session.add(StaffPromotionState(
                                    guild_id=guild.id, user_id=target.id, rank=0,
                                    role_id=previous.role_id, rank_type=previous.rank_type,
                                    xp_at_promotion=xp_after, source="staff_warn",
                                ))
                            elif state is not None:
                                # Keep a rank-0 state so the member remains recognized as
                                # Staff for future warnings even after losing the first rank.
                                state.rank = 0
                                state.role_id = previous.role_id if previous else state.role_id
                                state.rank_type = previous.rank_type if previous else state.rank_type
                                state.xp_at_promotion = xp_after
                                state.source = "staff_warn"
                                state.promoted_at = discord.utils.utcnow()
                        elif state is None:
                            session.add(StaffPromotionState(
                                guild_id=guild.id, user_id=target.id, rank=current.rank,
                                role_id=current.role_id, rank_type=current.rank_type,
                                xp_at_promotion=xp_after, source="staff_warn",
                            ))
                        else:
                            state.rank = current.rank
                            state.role_id = current.role_id
                            state.rank_type = current.rank_type
                            state.xp_at_promotion = xp_after
                            state.source = "staff_warn"
                            state.promoted_at = discord.utils.utcnow()
        except IntegrityError as exc:
            if rank_changed:
                try:
                    await target.edit(roles=original_roles, reason="Rollback failed Staff Warn database transaction")
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception("Could not restore Staff roles after a failed warning transaction for %s.", target.id)
            raise ValueError("This Staff warning was already saved; no duplicate XP was deducted.") from exc
        except Exception:
            if rank_changed:
                try:
                    await target.edit(roles=original_roles, reason="Rollback failed Staff Warn database transaction")
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception("Could not restore Staff roles after a failed warning transaction for %s.", target.id)
            raise

        await _log_warning(target, moderator, reason, xp_before, xp_after, applied_penalty, previous, current)
        if previous and (current is None or current.rank < previous.rank):
            await _announce_demotion(target, reason, xp_before, xp_after, applied_penalty, previous, current)
        return {
            "xp_before": xp_before, "xp_after": xp_after, "penalty": applied_penalty,
            "previous_rank": previous, "current_rank": current,
            "rank_updated": bool(config_active),
        }


async def _log_warning(
    target: discord.Member, moderator: discord.Member, reason: str,
    xp_before: int, xp_after: int, penalty: int,
    previous: StaffRank | None, current: StaffRank | None,
) -> None:
    old_role = target.guild.get_role(previous.role_id) if previous else None
    new_role = target.guild.get_role(current.role_id) if current else None
    tier = f"{previous.rank_type.upper()} → {current.rank_type.upper()}" if previous and current and previous.rank_type != current.rank_type else (current.rank_type.upper() if current else "No configured rank")
    try:
        await send_log(target.guild, LogEvent(
            LogType.XP, "⚠️ STAFF WARN", user=target, staff=moderator,
            reason=reason, xp_change=-penalty,
            fields=[
                ("XP Before", f"{xp_before:,}", True), ("XP Penalty", f"-{penalty:,}", True), ("XP After", f"{xp_after:,}", True),
                ("Previous Rank", f"Rank {previous.rank} · {old_role.mention}" if previous and old_role else "No configured rank", True),
                ("Current Rank", f"Rank {current.rank} · {new_role.mention}" if current and new_role else "No configured rank", True),
                ("Rank Change", tier, True),
            ], ids={"Guild ID": target.guild.id, "Warning Target": target.id, "Moderator": moderator.id},
        ))
    except Exception:
        logger.exception("Could not log Staff warning for member %s.", target.id)


async def _announce_demotion(
    target: discord.Member, reason: str, xp_before: int, xp_after: int, penalty: int,
    previous: StaffRank, current: StaffRank | None,
) -> None:
    try:
        channel_id = await config_service.get_setting(target.guild.id, "staff_demotion_channel")
    except Exception:
        logger.exception("Could not read Staff demotion channel in guild %s.", target.guild.id)
        return
    channel = target.guild.get_channel(channel_id) if channel_id else None
    if not isinstance(channel, discord.TextChannel):
        return
    old_role = target.guild.get_role(previous.role_id)
    new_role = target.guild.get_role(current.role_id) if current else None
    tier = f"{previous.rank_type.upper()} → {current.rank_type.upper()}" if current and previous.rank_type != current.rank_type else previous.rank_type.upper()
    new_rank_text = new_role.mention if new_role else "No promotion rank"
    embed = discord.Embed(
        title="⚠️ STAFF RANK REDUCTION",
        description=f"{target.mention}\n\n{old_role.mention if old_role else f'Rank {previous.rank}'} → {new_rank_text}\n\nXP: {xp_before:,} → {xp_after:,}\n\nReason: {reason}",
        color=discord.Color.orange(), timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Rank Change", value=tier, inline=True)
    embed.add_field(name="XP Penalty", value=f"-{penalty:,} XP", inline=True)
    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions(users=True, roles=True))
    except (discord.Forbidden, discord.HTTPException):
        logger.exception("Could not send Staff demotion notification in guild %s.", target.guild.id)
