"""Temporary voice rooms and room-scoped access controls."""
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, ForeignKeyConstraint, Index,
    Integer, String, func,
)
from sqlalchemy.orm import Mapped, mapped_column

from bot.database.engine import Base


class TempVoiceRoom(Base):
    __tablename__ = "temp_voice_rooms"
    __table_args__ = (Index("ix_temp_voice_owner", "guild_id", "owner_id"),)

    guild_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("guilds.guild_id", ondelete="CASCADE"), primary_key=True,
        autoincrement=False,
    )
    channel_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    owner_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    category_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    user_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    panel_message_id: Mapped[int | None] = mapped_column(BigInteger)


class TempVoiceRoomBan(Base):
    __tablename__ = "temp_voice_room_bans"
    __table_args__ = (
        ForeignKeyConstraint(
            ["guild_id", "channel_id"],
            ["temp_voice_rooms.guild_id", "temp_voice_rooms.channel_id"],
            ondelete="CASCADE",
        ),
    )

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)


class TempVoiceRoomAccess(Base):
    __tablename__ = "temp_voice_room_access"
    __table_args__ = (
        ForeignKeyConstraint(
            ["guild_id", "channel_id"],
            ["temp_voice_rooms.guild_id", "temp_voice_rooms.channel_id"],
            ondelete="CASCADE",
        ),
    )

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
