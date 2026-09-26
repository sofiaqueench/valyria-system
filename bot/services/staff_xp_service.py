"""Central persistence for Staff XP awards from all bot systems."""
import asyncio
import discord
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.core.logger import logger
from bot.database.engine import async_session
from bot.database.models.verification import StaffXPHistory


def get_staff_xp_mutation_lock(guild_id: int, staff_id: int) -> asyncio.Lock:
    """Share a per-member lock between XP awards, penalties, and rank reconciliation."""
    from bot.services.staff_promotion_service import _PROMOTION_LOCKS
    return _PROMOTION_LOCKS.setdefault((guild_id, staff_id), asyncio.Lock())


async def get_latest_staff_xp_change(guild_id: int, staff_id: int) -> StaffXPHistory | None:
    async with async_session() as session:
        return await session.scalar(select(StaffXPHistory).where(
            StaffXPHistory.guild_id == guild_id,
            StaffXPHistory.staff_id == staff_id,
        ).order_by(StaffXPHistory.id.desc()).limit(1))


async def add_staff_xp_to_session(
    session: AsyncSession,
    *,
    guild_id: int,
    staff_id: int,
    target_id: int,
    action: str,
    amount: int,
    verification_record_id: int | None = None,
    game_session_id: int | None = None,
) -> bool:
    """Add a ledger row inside a caller-owned transaction."""
    if amount < 0 or (amount == 0 and game_session_id is None):
        return False
    session.add(StaffXPHistory(
        guild_id=guild_id,
        staff_id=staff_id,
        target_id=target_id,
        action=action,
        xp_amount=amount,
        verification_record_id=verification_record_id,
        game_session_id=game_session_id,
    ))
    await session.flush()
    return True


async def add_staff_xp(
    *,
    guild_id: int,
    staff_id: int,
    target_id: int,
    action: str,
    amount: int,
    verification_record_id: int | None = None,
    game_session_id: int | None = None,
    member: discord.Member | None = None,
) -> bool:
    """Persist one award using the shared staff XP ledger."""
    if amount < 0 or (amount == 0 and game_session_id is None):
        return False
    awarded = False
    async with get_staff_xp_mutation_lock(guild_id, staff_id):
        async with async_session() as session:
            try:
                async with session.begin():
                    awarded = await add_staff_xp_to_session(
                        session,
                        guild_id=guild_id,
                        staff_id=staff_id,
                        target_id=target_id,
                        action=action,
                        amount=amount,
                    verification_record_id=verification_record_id,
                    game_session_id=game_session_id,
                    )
            except IntegrityError:
                await session.rollback()
                return False
    if awarded and member is not None:
        try:
            await on_staff_xp_awarded(member, amount=amount, source=action)
        except Exception:
            logger.exception("Staff XP was saved but promotion processing failed for member %s.", member.id)
    return awarded


async def on_staff_xp_awarded(member: discord.Member, *, amount: int, source: str) -> None:
    """Single post-commit promotion hook shared by every Staff XP source."""
    from bot.services.staff_promotion_service import on_staff_xp_awarded as process_promotion
    await process_promotion(member, amount=amount, source=source)


async def add_verification_xp(
    *, guild_id: int, staff_id: int, target_id: int,
    verification_record_id: int, amount: int, member: discord.Member | None = None,
) -> bool:
    """Compatibility wrapper: award XP for a verification exactly once."""
    return await add_staff_xp(
        guild_id=guild_id,
        staff_id=staff_id,
        target_id=target_id,
        action="verification",
        amount=amount,
        verification_record_id=verification_record_id,
        member=member,
    )
