"""Persistent Staff game sessions and their completed/cancelled result history."""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.orm import Mapped, mapped_column

from bot.database.engine import Base


class StaffGameSession(Base):
    __tablename__ = "staff_game_sessions"
    __table_args__ = (
        Index(
            "uq_staff_game_one_active_per_guild_type",
            "guild_id", "game_type", unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.guild_id", ondelete="CASCADE"), nullable=False, index=True)
    game_type: Mapped[str] = mapped_column(String(32), nullable=False)
    host_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    panel_channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    winner: Mapped[str | None] = mapped_column(String(200))
    xp_awarded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[int | None] = mapped_column(BigInteger)
    result_message_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
