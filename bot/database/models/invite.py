"""First known invite attribution for each member in each guild."""
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from bot.database.engine import Base


class InviteAttribution(Base):
    __tablename__ = "invite_attributions"
    __table_args__ = (
        UniqueConstraint("guild_id", "member_id", name="uq_invite_attributions_guild_member"),
        Index("ix_invite_attributions_guild_inviter", "guild_id", "inviter_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.guild_id", ondelete="CASCADE"), nullable=False)
    member_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    inviter_id: Mapped[int | None] = mapped_column(BigInteger)
    invite_code: Mapped[str | None] = mapped_column(String(32))
    uses_before: Mapped[int | None] = mapped_column(Integer)
    uses_after: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    xp_awarded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    xp_amount: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
