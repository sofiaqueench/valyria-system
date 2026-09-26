"""Central persistence for Staff XP awards from all bot systems."""
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.database.engine import async_session
from bot.database.models.verification import StaffXPHistory


async def add_staff_xp_to_session(
    session: AsyncSession,
    *,
    guild_id: int,
    staff_id: int,
    target_id: int,
    action: str,
    amount: int,
    verification_record_id: int | None = None,
) -> bool:
    """Add a ledger row inside a caller-owned transaction."""
    if amount <= 0:
        return False
    session.add(StaffXPHistory(
        guild_id=guild_id,
        staff_id=staff_id,
        target_id=target_id,
        action=action,
        xp_amount=amount,
        verification_record_id=verification_record_id,
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
) -> bool:
    """Persist one award using the shared staff XP ledger."""
    if amount <= 0:
        return False
    async with async_session() as session:
        try:
            async with session.begin():
                return await add_staff_xp_to_session(
                    session,
                    guild_id=guild_id,
                    staff_id=staff_id,
                    target_id=target_id,
                    action=action,
                    amount=amount,
                    verification_record_id=verification_record_id,
                )
        except IntegrityError:
            await session.rollback()
            return False


async def add_verification_xp(
    *, guild_id: int, staff_id: int, target_id: int,
    verification_record_id: int, amount: int,
) -> bool:
    """Compatibility wrapper: award XP for a verification exactly once."""
    return await add_staff_xp(
        guild_id=guild_id,
        staff_id=staff_id,
        target_id=target_id,
        action="verification",
        amount=amount,
        verification_record_id=verification_record_id,
    )
