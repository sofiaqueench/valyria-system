"""
جدول guilds: إعدادات كل سيرفر (بديل عن hardcoded IDs، كما اتفقنا في Phase 0).
"""
from sqlalchemy import BigInteger
from sqlalchemy.orm import Mapped, mapped_column

from bot.database.engine import Base


class Guild(Base):
    __tablename__ = "guilds"

    guild_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)

    # هذه الأعمدة فارغة الآن (None) — سنملؤها في Phase 4 (Configuration)
    verification_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    verification_voice_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    welcome_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    log_channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)