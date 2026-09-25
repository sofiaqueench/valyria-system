"""
جدول guilds: سجل لكل سيرفر يعمل فيه البوت.
إعدادات السيرفر نفسها (الرومات، الرتب، XP...) في جدول guild_settings.
"""
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from bot.database.engine import Base


class Guild(Base):
    __tablename__ = "guilds"

    # المعرّف يأتي من Discord، لذلك لا نريد ترقيمًا تلقائيًا
    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
