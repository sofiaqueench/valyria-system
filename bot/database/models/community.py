"""Guild-scoped community feature persistence."""
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Index, func
from sqlalchemy.orm import Mapped, mapped_column

from bot.database.engine import Base


class ServerStat(Base):
    __tablename__ = "server_stats"
    __table_args__ = (UniqueConstraint("guild_id", "stat_type", name="uq_server_stat_guild_type"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.guild_id", ondelete="CASCADE"), nullable=False)
    stat_type: Mapped[str] = mapped_column(String(24), nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_name: Mapped[str | None] = mapped_column(String(100), nullable=True)


class CommunitySequence(Base):
    __tablename__ = "community_sequences"
    __table_args__ = (UniqueConstraint("guild_id", "kind", name="uq_community_sequence_guild_kind"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.guild_id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    last_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Suggestion(Base):
    __tablename__ = "suggestions"
    __table_args__ = (UniqueConstraint("guild_id", "suggestion_number", name="uq_suggestion_guild_number"), Index("ix_suggestion_guild_status", "guild_id", "status"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.guild_id", ondelete="CASCADE"), nullable=False)
    suggestion_number: Mapped[int] = mapped_column(Integer, nullable=False)
    author_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="PENDING")
    review_message_id: Mapped[int | None] = mapped_column(BigInteger)
    review_channel_id: Mapped[int | None] = mapped_column(BigInteger)
    approved_message_id: Mapped[int | None] = mapped_column(BigInteger)
    approved_channel_id: Mapped[int | None] = mapped_column(BigInteger)
    reviewed_by: Mapped[int | None] = mapped_column(BigInteger)
    review_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Poll(Base):
    __tablename__ = "community_polls"
    __table_args__ = (UniqueConstraint("guild_id", "poll_number", name="uq_poll_guild_number"), Index("ix_poll_guild_status", "guild_id", "status"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.guild_id", ondelete="CASCADE"), nullable=False)
    poll_number: Mapped[int] = mapped_column(Integer, nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    creator_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    question: Mapped[str] = mapped_column(String(300), nullable=False)
    options_json: Mapped[str] = mapped_column(Text, nullable=False)
    multiple: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="OPEN")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class PollVote(Base):
    __tablename__ = "poll_votes"
    __table_args__ = (UniqueConstraint("poll_id", "user_id", "option_index", name="uq_poll_vote_option"), Index("ix_poll_vote_poll", "poll_id"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    poll_id: Mapped[int] = mapped_column(Integer, ForeignKey("community_polls.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    option_index: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Confession(Base):
    __tablename__ = "confessions"
    __table_args__ = (UniqueConstraint("guild_id", "confession_number", name="uq_confession_guild_number"), Index("ix_confession_guild_author_time", "guild_id", "author_id", "created_at"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.guild_id", ondelete="CASCADE"), nullable=False)
    confession_number: Mapped[int] = mapped_column(Integer, nullable=False)
    author_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    channel_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
