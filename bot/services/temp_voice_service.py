"""Persistence helpers for guild-scoped temporary voice rooms."""
from sqlalchemy import select

from bot.database.engine import async_session
from bot.database.models.temp_voice import TempVoiceRoom, TempVoiceRoomAccess, TempVoiceRoomBan
from bot.services.config_service import get_or_create_guild


async def create_room(
    guild_id: int, channel_id: int, owner_id: int, category_id: int | None,
    name: str, user_limit: int,
) -> TempVoiceRoom:
    await get_or_create_guild(guild_id)
    async with async_session() as session:
        async with session.begin():
            room = TempVoiceRoom(
                guild_id=guild_id, channel_id=channel_id, owner_id=owner_id,
                category_id=category_id, name=name, user_limit=user_limit, locked=False,
            )
            session.add(room)
        return room


async def get_room(guild_id: int, channel_id: int) -> TempVoiceRoom | None:
    async with async_session() as session:
        return await session.get(TempVoiceRoom, (guild_id, channel_id))


async def list_rooms(guild_id: int | None = None) -> list[TempVoiceRoom]:
    async with async_session() as session:
        statement = select(TempVoiceRoom)
        if guild_id is not None:
            statement = statement.where(TempVoiceRoom.guild_id == guild_id)
        result = await session.execute(statement)
        return list(result.scalars().all())


async def update_room(guild_id: int, channel_id: int, **values) -> bool:
    allowed = {"owner_id", "name", "user_limit", "locked", "panel_message_id"}
    if not values or values.keys() - allowed:
        raise ValueError("Invalid temporary room fields")
    async with async_session() as session:
        async with session.begin():
            room = await session.get(TempVoiceRoom, (guild_id, channel_id))
            if room is None:
                return False
            for key, value in values.items():
                setattr(room, key, value)
        return True


async def delete_room(guild_id: int, channel_id: int) -> bool:
    async with async_session() as session:
        async with session.begin():
            room = await session.get(TempVoiceRoom, (guild_id, channel_id))
            if room is None:
                return False
            await session.delete(room)
        return True


async def is_room_banned(guild_id: int, channel_id: int, user_id: int) -> bool:
    async with async_session() as session:
        return await session.get(TempVoiceRoomBan, (guild_id, channel_id, user_id)) is not None


async def set_room_ban(guild_id: int, channel_id: int, user_id: int, banned: bool) -> bool:
    async with async_session() as session:
        async with session.begin():
            key = (guild_id, channel_id, user_id)
            row = await session.get(TempVoiceRoomBan, key)
            if banned and row is None:
                session.add(TempVoiceRoomBan(guild_id=guild_id, channel_id=channel_id, user_id=user_id))
            elif not banned and row is not None:
                await session.delete(row)
        return True


async def list_room_access(guild_id: int, channel_id: int) -> list[int]:
    async with async_session() as session:
        rows = await session.execute(
            select(TempVoiceRoomAccess.user_id).where(
                TempVoiceRoomAccess.guild_id == guild_id,
                TempVoiceRoomAccess.channel_id == channel_id,
            )
        )
        return list(rows.scalars().all())


async def list_room_bans(guild_id: int, channel_id: int) -> list[int]:
    async with async_session() as session:
        rows = await session.execute(
            select(TempVoiceRoomBan.user_id).where(
                TempVoiceRoomBan.guild_id == guild_id,
                TempVoiceRoomBan.channel_id == channel_id,
            )
        )
        return list(rows.scalars().all())


async def set_room_access(guild_id: int, channel_id: int, user_id: int, granted: bool) -> None:
    async with async_session() as session:
        async with session.begin():
            key = (guild_id, channel_id, user_id)
            row = await session.get(TempVoiceRoomAccess, key)
            if granted and row is None:
                session.add(TempVoiceRoomAccess(guild_id=guild_id, channel_id=channel_id, user_id=user_id))
            elif not granted and row is not None:
                await session.delete(row)
