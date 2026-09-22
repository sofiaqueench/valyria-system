"""
تحميل متغيرات البيئة من .env وتوفيرها لباقي المشروع كـ Settings واحدة.
"""
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()  # يقرأ ملف .env ويحمّل القيم في os.environ


@dataclass
class Settings:
    discord_token: str
    guild_id: int | None


def load_settings() -> Settings:
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError(
            "DISCORD_TOKEN غير موجود في .env — تأكدي من ملء الملف قبل التشغيل."
        )

    guild_id_raw = os.getenv("GUILD_ID")
    guild_id = int(guild_id_raw) if guild_id_raw else None

    return Settings(discord_token=token, guild_id=guild_id)


settings = load_settings()