"""
تحميل متغيّرات البيئة من .env وتوفيرها كـ Settings واحد.

ملاحظة: هذا الملف للأسرار وإعدادات التشغيل فقط (التوكن، مسار قاعدة البيانات).
إعدادات السيرفر (الرومات، الرتب، XP...) موجودة في bot/config/schema.py وتُحفظ في قاعدة البيانات.
"""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # يقرأ ملف .env ويحمّل القيم في os.environ

# جذر المشروع (المجلد الذي يحتوي على bot/ و alembic/)
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# as_posix() يحوّل \ إلى / حتى يعمل المسار في ويندوز داخل رابط قاعدة البيانات
DEFAULT_DATABASE_URL = f"sqlite+aiosqlite:///{(PROJECT_ROOT / 'bot_database.db').as_posix()}"


@dataclass
class Settings:
    discord_token: str
    guild_id: int
    database_url: str


def load_settings() -> Settings:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN غير موجود في .env — تأكد من إنشاء الملف قبل تشغيل البوت."
        )

    guild_id_raw = os.getenv("GUILD_ID")
    if not guild_id_raw:
        raise RuntimeError("GUILD_ID is missing from .env — set the development server ID before starting the bot.")
    try:
        guild_id = int(guild_id_raw)
    except ValueError as exc:
        raise RuntimeError("GUILD_ID must contain a numeric Discord server ID.") from exc
    if guild_id <= 0:
        raise RuntimeError("GUILD_ID must be a positive Discord server ID.")

    database_url = os.getenv("DATABASE_URL") or DEFAULT_DATABASE_URL

    return Settings(discord_token=token, guild_id=guild_id, database_url=database_url)


settings = load_settings()
