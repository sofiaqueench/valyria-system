"""Persistent guild-scoped ticket records and ticket number counters."""
from datetime import datetime
from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column
from bot.database.engine import Base


class TicketSequence(Base):
    __tablename__ = "ticket_sequences"
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.guild_id", ondelete="CASCADE"), primary_key=True)
    last_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Ticket(Base):
    __tablename__ = "tickets"
    __table_args__ = (
        UniqueConstraint("guild_id", "ticket_number", name="uq_ticket_guild_number"),
        UniqueConstraint("guild_id", "channel_id", name="uq_ticket_guild_channel"),
        Index("ix_ticket_guild_creator_status", "guild_id", "creator_id", "status"),
        Index("uq_ticket_one_open_per_member", "guild_id", "creator_id", unique=True,
              sqlite_where=text("status = 'open' AND ticket_type != 'appeal'"),
              postgresql_where=text("status = 'open' AND ticket_type != 'appeal'")),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.guild_id", ondelete="CASCADE"), nullable=False)
    ticket_number: Mapped[int] = mapped_column(Integer, nullable=False)
    ticket_type: Mapped[str] = mapped_column(String(32), nullable=False)
    creator_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    category_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    claimed_by: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by: Mapped[int | None] = mapped_column(BigInteger)
    opening_message_id: Mapped[int | None] = mapped_column(BigInteger)
