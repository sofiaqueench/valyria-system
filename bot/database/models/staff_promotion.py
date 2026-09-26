"""Persist the last applied Staff promotion for idempotent reconciliation."""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from bot.database.engine import Base


class StaffPromotionState(Base):
    __tablename__ = "staff_promotion_states"

    guild_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("guilds.guild_id", ondelete="CASCADE"),
        primary_key=True, autoincrement=False,
    )
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    role_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    rank_type: Mapped[str] = mapped_column(String(16), nullable=False)
    xp_at_promotion: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(40), nullable=False)
    promoted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
