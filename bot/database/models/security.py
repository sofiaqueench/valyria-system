"""Persistent state for Anti-Raid and member Jail."""
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from bot.database.engine import Base


class RaidState(Base):
    __tablename__ = "raid_states"
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.guild_id", ondelete="CASCADE"), primary_key=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    activated_by: Mapped[int | None] = mapped_column(BigInteger)
    reason: Mapped[str | None] = mapped_column(String(300))
    join_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class JailRecord(Base):
    __tablename__ = "jail_records"
    __table_args__ = (
        Index("uq_jail_active_member", "guild_id", "user_id", unique=True,
              sqlite_where=text("status = 'ACTIVE'"), postgresql_where=text("status = 'ACTIVE'")),
        Index("ix_jail_guild_user_status", "guild_id", "user_id", "status"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.guild_id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    moderator_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    original_role_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    jail_role_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE", server_default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    unjailed_by: Mapped[int | None] = mapped_column(BigInteger)
    unjailed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JailChannelPermission(Base):
    __tablename__ = "jail_channel_permissions"
    __table_args__ = (UniqueConstraint("guild_id", "role_id", "channel_id", name="uq_jail_channel_permission"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.guild_id", ondelete="CASCADE"), nullable=False)
    role_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    had_overwrite: Mapped[bool] = mapped_column(Boolean, nullable=False)
    allow_bits: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    deny_bits: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
