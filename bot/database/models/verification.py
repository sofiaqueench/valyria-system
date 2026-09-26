from datetime import datetime
from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column
from bot.database.engine import Base

class VerificationRecord(Base):
    __tablename__ = "verification_records"
    __table_args__ = (Index("ix_verification_guild_member_status", "guild_id", "member_id", "status"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.guild_id", ondelete="CASCADE"), nullable=False)
    member_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    staff_id: Mapped[int | None] = mapped_column(BigInteger)
    gender: Mapped[str | None] = mapped_column(String(16))
    is_other_server_staff: Mapped[bool | None] = mapped_column(Boolean)
    other_server_name: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

class StaffXPHistory(Base):
    __tablename__ = "staff_xp_history"
    __table_args__ = (UniqueConstraint("verification_record_id", name="uq_staff_xp_verification_record"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.guild_id", ondelete="CASCADE"), nullable=False)
    staff_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    xp_amount: Mapped[int] = mapped_column(Integer, nullable=False)
    target_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    verification_record_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("verification_records.id", ondelete="CASCADE"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
