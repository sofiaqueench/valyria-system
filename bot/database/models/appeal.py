"""Persistent guild-scoped moderation appeals linked to the shared ticket system."""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from bot.database.engine import Base


class AppealSequence(Base):
    __tablename__ = "appeal_sequences"

    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.guild_id", ondelete="CASCADE"), primary_key=True)
    last_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Appeal(Base):
    __tablename__ = "appeals"
    __table_args__ = (
        UniqueConstraint("guild_id", "appeal_number", name="uq_appeal_guild_number"),
        UniqueConstraint("guild_id", "ticket_id", name="uq_appeal_guild_ticket"),
        Index("uq_appeal_one_pending_per_user", "guild_id", "user_id", unique=True,
              sqlite_where=text("status = 'Pending'"), postgresql_where=text("status = 'Pending'")),
        Index("ix_appeal_guild_status", "guild_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.guild_id", ondelete="CASCADE"), nullable=False)
    appeal_number: Mapped[int] = mapped_column(Integer, nullable=False)
    ticket_id: Mapped[int] = mapped_column(Integer, ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    username: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    additional_information: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="Pending")
    staff_id: Mapped[int | None] = mapped_column(BigInteger)
    closed_by: Mapped[int | None] = mapped_column(BigInteger)
    opening_message_id: Mapped[int | None] = mapped_column(BigInteger)
    staff_decision: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
