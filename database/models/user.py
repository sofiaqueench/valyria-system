"""
جدول users: بيانات الأعضاء الأساسية (سنوسّعه لاحقًا في Phase 9 و10 لـ Invite/XP).
"""
from sqlalchemy import BigInteger, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from bot.database.engine import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    discord_id: Mapped[int] = mapped_column(BigInteger)
    guild_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("guilds.guild_id"))