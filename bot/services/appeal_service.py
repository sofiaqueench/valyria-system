"""Persistence and state transitions for the Appeals feature."""
import asyncio
from datetime import datetime, timezone

from sqlalchemy import select, update
from bot.database.engine import async_session
from bot.database.models.appeal import Appeal, AppealSequence
from bot.database.models.ticket import Ticket
from bot.services.ticket_service import close_ticket_in_session

_LOCKS: dict[int, asyncio.Lock] = {}


async def find_pending_appeal(guild_id: int, user_id: int) -> Appeal | None:
    async with async_session() as session:
        return await session.scalar(select(Appeal).where(
            Appeal.guild_id == guild_id, Appeal.user_id == user_id, Appeal.status == "Pending"
        ).limit(1))


async def get_appeal(guild_id: int, appeal_id: int) -> Appeal | None:
    async with async_session() as session:
        return await session.scalar(select(Appeal).where(
            Appeal.guild_id == guild_id, Appeal.id == appeal_id
        ))


async def set_appeal_message(guild_id: int, appeal_id: int, message_id: int) -> bool:
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(update(Appeal).where(
                Appeal.guild_id == guild_id, Appeal.id == appeal_id
            ).values(opening_message_id=message_id))
            return result.rowcount == 1


async def reserve_appeal_number(guild_id: int) -> int:
    lock = _LOCKS.setdefault(guild_id, asyncio.Lock())
    async with lock:
        async with async_session() as session:
            async with session.begin():
                sequence = await session.scalar(select(AppealSequence).where(
                    AppealSequence.guild_id == guild_id
                ).with_for_update())
                if sequence is None:
                    sequence = AppealSequence(guild_id=guild_id, last_number=0)
                    session.add(sequence)
                    await session.flush()
                sequence.last_number += 1
                await session.flush()
                return sequence.last_number


async def create_appeal(*, guild_id: int, appeal_number: int, ticket_id: int,
                        user_id: int, username: str, reason: str, explanation: str,
                        additional_information: str | None) -> Appeal:
    lock = _LOCKS.setdefault(guild_id, asyncio.Lock())
    async with lock:
        async with async_session() as session:
            async with session.begin():
                pending = await session.scalar(select(Appeal.id).where(
                    Appeal.guild_id == guild_id, Appeal.user_id == user_id,
                    Appeal.status == "Pending"
                ).limit(1))
                if pending:
                    raise ValueError("pending_appeal_exists")
                appeal = Appeal(
                    guild_id=guild_id, appeal_number=appeal_number, ticket_id=ticket_id,
                    user_id=user_id, username=username[:128], reason=reason,
                    explanation=explanation, additional_information=additional_information or None,
                    status="Pending",
                )
                session.add(appeal)
                await session.flush()
                await session.refresh(appeal)
                return appeal


async def decide_appeal(*, guild_id: int, appeal_id: int, staff_id: int,
                        status: str, staff_decision: str | None) -> Appeal | None:
    if status not in {"Accepted", "Rejected"}:
        raise ValueError("invalid_appeal_decision")
    lock = _LOCKS.setdefault(guild_id, asyncio.Lock())
    async with lock:
        async with async_session() as session:
            async with session.begin():
                now = datetime.now(timezone.utc)
                result = await session.execute(update(Appeal).where(
                    Appeal.guild_id == guild_id, Appeal.id == appeal_id, Appeal.status == "Pending"
                ).values(status=status, staff_id=staff_id, staff_decision=staff_decision or None,
                         decided_at=now, updated_at=now))
                if result.rowcount != 1:
                    return None
                return await session.scalar(select(Appeal).where(Appeal.guild_id == guild_id, Appeal.id == appeal_id))


async def close_appeal(*, guild_id: int, appeal_id: int, staff_id: int) -> Appeal | None:
    """Close the appeal and its shared ticket in one database transaction."""
    lock = _LOCKS.setdefault(guild_id, asyncio.Lock())
    async with lock:
        async with async_session() as session:
            async with session.begin():
                appeal = await session.scalar(select(Appeal).where(
                    Appeal.guild_id == guild_id, Appeal.id == appeal_id
                ).with_for_update())
                if appeal is None or appeal.status == "Closed":
                    return None
                result = await session.execute(update(Appeal).where(
                    Appeal.guild_id == guild_id, Appeal.id == appeal_id, Appeal.status != "Closed"
                ).values(status="Closed", closed_by=staff_id, updated_at=datetime.now(timezone.utc)))
                if result.rowcount != 1:
                    return None
                ticket = await session.get(Ticket, appeal.ticket_id)
                if ticket is not None:
                    await close_ticket_in_session(session, guild_id, ticket.channel_id, staff_id)
                return await session.scalar(select(Appeal).where(Appeal.guild_id == guild_id, Appeal.id == appeal_id))
