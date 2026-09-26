"""Persistence and XP integration for the Staff Games system."""
from __future__ import annotations

import asyncio
from datetime import datetime

import discord
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from bot.config.game_registry import GAMES
from bot.core.logger import logger
from bot.database.engine import async_session
from bot.database.models.staff_game import StaffGameSession
from bot.services.staff_xp_service import (
    add_staff_xp_to_session, get_staff_xp_mutation_lock, on_staff_xp_awarded,
)

_GAME_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}


async def get_active_game(guild_id: int, game_type: str) -> StaffGameSession | None:
    async with async_session() as session:
        return await session.scalar(select(StaffGameSession).where(
            StaffGameSession.guild_id == guild_id,
            StaffGameSession.game_type == game_type,
            StaffGameSession.status == "active",
        ))


async def get_pending_result(guild_id: int, game_type: str) -> StaffGameSession | None:
    async with async_session() as session:
        return await session.scalar(select(StaffGameSession).where(
            StaffGameSession.guild_id == guild_id,
            StaffGameSession.game_type == game_type,
            StaffGameSession.status == "completed",
            StaffGameSession.result_message_id.is_(None),
        ).order_by(StaffGameSession.id.desc()).limit(1))


async def start_game(
    *, guild_id: int, game_type: str, host_id: int, created_by: int,
    panel_channel_id: int, started_at: datetime | None = None,
) -> StaffGameSession:
    if game_type not in GAMES:
        raise ValueError("Unknown Staff game.")
    lock = _GAME_LOCKS.setdefault((guild_id, game_type), asyncio.Lock())
    async with lock:
        async with async_session() as session:
            try:
                async with session.begin():
                    active = await session.scalar(select(StaffGameSession).where(
                        StaffGameSession.guild_id == guild_id,
                        StaffGameSession.game_type == game_type,
                        StaffGameSession.status == "active",
                    ))
                    if active is not None:
                        raise ValueError(f"There is already an active {GAMES[game_type].label} game (ID {active.id}).")
                    game = StaffGameSession(
                        guild_id=guild_id, game_type=game_type, host_id=host_id,
                        created_by=created_by, panel_channel_id=panel_channel_id,
                        status="active", started_at=started_at or discord.utils.utcnow(),
                    )
                    session.add(game)
                    await session.flush()
            except IntegrityError as exc:
                raise ValueError(f"There is already an active {GAMES[game_type].label} game.") from exc
            return game


async def finish_game(
    *, guild_id: int, game_type: str, session_id: int, host: discord.Member,
    winner: str, xp_amount: int, ended_at: datetime | None = None,
) -> StaffGameSession:
    if game_type not in GAMES:
        raise ValueError("Unknown Staff game.")
    if xp_amount < 0:
        raise ValueError("Game Host XP cannot be negative.")
    game_lock = _GAME_LOCKS.setdefault((guild_id, game_type), asyncio.Lock())
    xp_lock = get_staff_xp_mutation_lock(guild_id, host.id)
    async with game_lock:
        async with xp_lock:
            async with async_session() as session:
                async with session.begin():
                    game = await session.scalar(select(StaffGameSession).where(
                        StaffGameSession.guild_id == guild_id,
                        StaffGameSession.game_type == game_type,
                        StaffGameSession.status == "active",
                    ))
                    if game is None:
                        raise ValueError("This game is no longer active or its result was already submitted.")
                    if game.id != session_id:
                        raise ValueError("This result form belongs to an older game session. Open Submit Result again.")
                    game.status = "completed"
                    game.host_id = host.id
                    game.winner = winner[:200]
                    game.ended_at = ended_at or discord.utils.utcnow()
                    game.xp_awarded = xp_amount
                    awarded = await add_staff_xp_to_session(
                        session, guild_id=guild_id, staff_id=host.id,
                        target_id=game.id, action="game_host", amount=xp_amount,
                        game_session_id=game.id,
                    )
                    if not awarded:
                        raise RuntimeError("Game Host XP could not be recorded; the result was not completed.")
                    await session.flush()
        if xp_amount > 0:
            try:
                await on_staff_xp_awarded(host, amount=xp_amount, source="game_host")
            except Exception:
                logger.exception("Game Host XP was stored but promotion processing failed for %s.", host.id)
        return game


async def cancel_game(
    *, guild_id: int, game_type: str, cancelled_by: int,
    ended_at: datetime | None = None,
) -> StaffGameSession:
    lock = _GAME_LOCKS.setdefault((guild_id, game_type), asyncio.Lock())
    async with lock:
        async with async_session() as session:
            async with session.begin():
                game = await session.scalar(select(StaffGameSession).where(
                    StaffGameSession.guild_id == guild_id,
                    StaffGameSession.game_type == game_type,
                    StaffGameSession.status == "active",
                ))
                if game is None:
                    raise ValueError("There is no active game to cancel.")
                game.status = "cancelled"
                game.ended_at = ended_at or discord.utils.utcnow()
                game.cancelled_at = game.ended_at
                game.cancelled_by = cancelled_by
                return game


async def save_result_message(game_id: int, guild_id: int, message_id: int) -> None:
    async with async_session() as session:
        async with session.begin():
            game = await session.get(StaffGameSession, game_id)
            if game is not None and game.guild_id == guild_id and game.status == "completed":
                game.result_message_id = message_id
