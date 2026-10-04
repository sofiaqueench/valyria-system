"""Guild-scoped normal member XP; separate from StaffXPHistory."""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from bot.database.engine import Base


class MemberXP(Base):
    __tablename__ = "member_xp"
    __table_args__ = (Index("ix_member_xp_guild_total", "guild_id", "total_xp"),)

    guild_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("guilds.guild_id", ondelete="CASCADE"), primary_key=True
    )
    member_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    total_xp: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_message_xp_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class MemberXPHistory(Base):
    """Append-only normal Member XP changes; separate from Staff XP history."""
    __tablename__ = "member_xp_history"
    __table_args__ = (
        UniqueConstraint("guild_id", "event_id", name="uq_member_xp_history_guild_event"),
        Index("ix_member_xp_history_guild_member", "guild_id", "member_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.guild_id", ondelete="CASCADE"), nullable=False)
    member_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    total_after: Mapped[int] = mapped_column(Integer, nullable=False)
    event_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
