"""Persist Staff warnings and their linked XP penalties."""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from bot.database.engine import Base


class StaffWarning(Base):
    __tablename__ = "staff_warnings"
    __table_args__ = (UniqueConstraint("interaction_id", name="uq_staff_warning_interaction"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.guild_id", ondelete="CASCADE"), nullable=False)
    staff_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    moderator_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    xp_penalty: Mapped[int] = mapped_column(Integer, nullable=False)
    interaction_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
