"""
جدول guild_settings: قيمة واحدة لكل (سيرفر + مفتاح إعداد).
المفاتيح وأنواعها وقيمها الافتراضية معرّفة في bot/config/schema.py.
لا يوجد صف = القيمة الافتراضية.
"""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from bot.database.engine import Base


class GuildSetting(Base):
    __tablename__ = "guild_settings"

    guild_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("guilds.guild_id", ondelete="CASCADE"),
        primary_key=True,
        autoincrement=False,
    )
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
