"""Central Member XP ledger and 100-XP-per-level progression."""
from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from weakref import WeakValueDictionary

import discord
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.core.logger import logger
from bot.database.engine import async_session
from bot.database.models.member_xp import MemberXP, MemberXPHistory
from bot.services import config_service

XP_PER_LEVEL = 100
_locks: WeakValueDictionary[tuple[int, int], asyncio.Lock] = WeakValueDictionary()


@dataclass(frozen=True)
class MemberXPResult:
    old_total: int
    new_total: int
    applied_amount: int
    old_level: int
    new_level: int
    cooldown_remaining: int = 0

    @property
    def awarded(self) -> bool:
        return self.applied_amount > 0


def calculate_level(total_xp: int) -> int:
    return max(0, int(total_xp)) // XP_PER_LEVEL


def _lock(guild_id: int, member_id: int) -> asyncio.Lock:
    key = (guild_id, member_id)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def get_member_xp_mutation_lock(guild_id: int, member_id: int) -> asyncio.Lock:
    """Shared lock for atomic XP changes outside ordinary awards (such as AutoMod)."""
    return _lock(guild_id, member_id)


async def apply_member_xp_delta_in_session(
    session: AsyncSession,
    member: discord.Member,
    amount: int,
    *,
    source: str,
    event_id: int,
    reason: str,
) -> MemberXPResult | None:
    """Apply one idempotent normal Member XP change inside the caller's transaction."""
    guild_id, member_id = member.guild.id, member.id
    existing = await session.scalar(select(MemberXPHistory).where(
        MemberXPHistory.guild_id == guild_id, MemberXPHistory.event_id == event_id,
    ))
    if existing is not None:
        return None
    row = await session.get(MemberXP, (guild_id, member_id))
    if row is None:
        row = MemberXP(guild_id=guild_id, member_id=member_id, total_xp=0)
        session.add(row)
        await session.flush()
    old_total = row.total_xp
    new_total = max(0, old_total + amount)
    applied = new_total - old_total
    row.total_xp = new_total
    session.add(MemberXPHistory(
        guild_id=guild_id, member_id=member_id, source=source,
        amount=applied, total_after=new_total, event_id=event_id, reason=reason,
    ))
    return MemberXPResult(old_total, new_total, applied,
                          calculate_level(old_total), calculate_level(new_total))


async def process_member_xp_change(
    member: discord.Member, result: MemberXPResult, *, source_channel: discord.abc.Messageable | None = None,
) -> None:
    """Run the same Level Role/announcement processing used by ordinary Member XP."""
    try:
        from bot.services.level_progression_service import process_level_progression
        await process_level_progression(member, result, source_channel=source_channel)
    except Exception:
        logger.exception("Could not process Member XP change for %s in guild %s.", member.id, member.guild.id)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


async def get_member_xp(guild_id: int, member_id: int) -> int:
    async with async_session() as session:
        row = await session.get(MemberXP, (guild_id, member_id))
        return row.total_xp if row else 0


async def add_member_xp(
    member: discord.Member,
    amount: int,
    *,
    source: str,
    message_cooldown: int | None = None,
    source_channel: discord.abc.Messageable | None = None,
) -> MemberXPResult | None:
    """Add XP from message, voice, or authorized staff action, then process levels."""
    if member.bot or amount == 0:
        return None
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise TypeError("Member XP amount must be an integer")

    guild_id, member_id = member.guild.id, member.id
    await config_service.get_or_create_guild(guild_id)
    async with _lock(guild_id, member_id):
        now = discord.utils.utcnow()
        async with async_session() as session:
            async with session.begin():
                row = await session.get(MemberXP, (guild_id, member_id))
                if row is None:
                    row = MemberXP(guild_id=guild_id, member_id=member_id, total_xp=0)
                    session.add(row)
                    await session.flush()
                old_total = row.total_xp
                if source == "message" and message_cooldown is not None and row.last_message_xp_at:
                    elapsed = (now - _as_utc(row.last_message_xp_at)).total_seconds()
                    remaining = max(0, int(message_cooldown - elapsed + 0.999))
                    if remaining > 0:
                        level = calculate_level(old_total)
                        return MemberXPResult(old_total, old_total, 0, level, level, remaining)

                new_total = max(0, old_total + amount)
                applied = new_total - old_total
                row.total_xp = new_total
                if source == "message":
                    row.last_message_xp_at = now
                result = MemberXPResult(
                    old_total=old_total,
                    new_total=new_total,
                    applied_amount=applied,
                    old_level=calculate_level(old_total),
                    new_level=calculate_level(new_total),
                )

        if result.old_level != result.new_level or source == "admin":
            try:
                from bot.services.level_progression_service import process_level_progression
                await process_level_progression(member, result, source_channel=source_channel)
            except Exception:
                logger.exception("Could not process level progression for member %s in guild %s.", member_id, guild_id)
        return result


async def award_message_xp(
    member: discord.Member, *, source_channel: discord.abc.Messageable | None = None
) -> MemberXPResult | None:
    minimum = int(await config_service.get_setting(member.guild.id, "message_xp_min") or 0)
    maximum = int(await config_service.get_setting(member.guild.id, "message_xp_max") or minimum)
    cooldown = int(await config_service.get_setting(member.guild.id, "message_xp_cooldown") or 0)
    if minimum <= 0 or maximum < minimum:
        logger.warning("Invalid message XP range in guild %s; no XP awarded.", member.guild.id)
        return None
    amount = random.randint(minimum, maximum)
    return await add_member_xp(
        member,
        amount,
        source="message",
        message_cooldown=cooldown,
        source_channel=source_channel,
    )


async def award_voice_xp(member: discord.Member, amount: int) -> MemberXPResult | None:
    return await add_member_xp(member, amount, source="voice")


async def set_member_xp(
    member: discord.Member,
    total_xp: int,
    *,
    source_channel: discord.abc.Messageable | None = None,
) -> MemberXPResult:
    """Set a member's normal XP total, preserving the shared progression flow."""
    if member.bot:
        raise ValueError("Bots cannot receive Member XP")
    if isinstance(total_xp, bool) or not isinstance(total_xp, int) or total_xp < 0:
        raise ValueError("Member XP total must be a non-negative integer")
    guild_id, member_id = member.guild.id, member.id
    await config_service.get_or_create_guild(guild_id)
    async with _lock(guild_id, member_id):
        async with async_session() as session:
            async with session.begin():
                row = await session.get(MemberXP, (guild_id, member_id))
                if row is None:
                    row = MemberXP(guild_id=guild_id, member_id=member_id, total_xp=total_xp)
                    session.add(row)
                    old_total = 0
                else:
                    old_total = row.total_xp
                    row.total_xp = total_xp
                result = MemberXPResult(
                    old_total=old_total,
                    new_total=total_xp,
                    applied_amount=total_xp - old_total,
                    old_level=calculate_level(old_total),
                    new_level=calculate_level(total_xp),
                )
        try:
            from bot.services.level_progression_service import process_level_progression
            await process_level_progression(member, result, source_channel=source_channel)
        except Exception:
            logger.exception("Could not sync level roles for member %s in guild %s.", member_id, guild_id)
    return result


async def get_leaderboard(guild_id: int, limit: int = 10) -> list[tuple[int, int]]:
    async with async_session() as session:
        rows = await session.execute(
            select(MemberXP.member_id, MemberXP.total_xp)
            .where(MemberXP.guild_id == guild_id, MemberXP.total_xp > 0)
            .order_by(MemberXP.total_xp.desc(), MemberXP.member_id.asc())
            .limit(limit)
        )
        return [(member_id, total) for member_id, total in rows.all()]
