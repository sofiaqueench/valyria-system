"""Database operations for tickets; SQL records remain the source of truth."""
import asyncio
from datetime import datetime, timezone
from sqlalchemy import select, update
from bot.database.engine import async_session
from bot.database.models.ticket import Ticket, TicketSequence

_LOCKS: dict[int, asyncio.Lock] = {}


async def find_open_ticket(guild_id: int, creator_id: int) -> Ticket | None:
    async with async_session() as session:
        return await session.scalar(select(Ticket).where(
            Ticket.guild_id == guild_id, Ticket.creator_id == creator_id, Ticket.status == "open",
            Ticket.ticket_type != "appeal",
        ).order_by(Ticket.ticket_number.desc()).limit(1))


async def get_ticket(guild_id: int, channel_id: int) -> Ticket | None:
    async with async_session() as session:
        return await session.scalar(select(Ticket).where(Ticket.guild_id == guild_id, Ticket.channel_id == channel_id))


async def create_ticket(*, guild_id: int, ticket_type: str, creator_id: int, category_id: int,
                        reason: str, channel_id: int, ticket_number: int) -> Ticket:
    lock = _LOCKS.setdefault(guild_id, asyncio.Lock())
    async with lock:
        async with async_session() as session:
            async with session.begin():
                if ticket_type != "appeal":
                    existing = await session.scalar(select(Ticket).where(
                        Ticket.guild_id == guild_id, Ticket.creator_id == creator_id, Ticket.status == "open",
                        Ticket.ticket_type != "appeal",
                    ).limit(1))
                    if existing:
                        raise ValueError("open_ticket_exists")
                row = Ticket(guild_id=guild_id, ticket_number=ticket_number, ticket_type=ticket_type,
                    creator_id=creator_id, channel_id=channel_id, category_id=category_id, reason=reason, status="open")
                session.add(row)
                await session.flush()
                await session.refresh(row)
                return row


async def reserve_ticket_number(guild_id: int) -> int:
    """Atomically advance a persistent per-guild counter; failed creates leave a gap."""
    lock = _LOCKS.setdefault(guild_id, asyncio.Lock())
    async with lock:
        async with async_session() as session:
            async with session.begin():
                seq = await session.get(TicketSequence, guild_id)
                if seq is None:
                    seq = TicketSequence(guild_id=guild_id, last_number=0)
                    session.add(seq)
                    await session.flush()
                seq.last_number += 1
                await session.flush()
                return seq.last_number


async def claim_ticket(guild_id: int, channel_id: int, staff_id: int) -> bool:
    async with async_session() as session:
        async with session.begin():
            result = await session.execute(update(Ticket).where(
                Ticket.guild_id == guild_id, Ticket.channel_id == channel_id,
                Ticket.status == "open", Ticket.claimed_by.is_(None)
            ).values(claimed_by=staff_id))
            return result.rowcount == 1


async def close_ticket(guild_id: int, channel_id: int, staff_id: int) -> bool:
    async with async_session() as session:
        async with session.begin():
            return await close_ticket_in_session(session, guild_id, channel_id, staff_id)


async def close_ticket_in_session(session, guild_id: int, channel_id: int, staff_id: int) -> bool:
    """Close an open ticket inside a caller-owned transaction."""
    result = await session.execute(update(Ticket).where(
        Ticket.guild_id == guild_id, Ticket.channel_id == channel_id, Ticket.status == "open"
    ).values(status="closed", closed_by=staff_id, closed_at=datetime.now(timezone.utc)))
    return result.rowcount == 1
