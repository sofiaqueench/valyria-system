"""
الخدمة المركزية لقراءة/كتابة إعدادات أي سيرفر.
كل نظام مستقبلي (Verification, Welcome, Tickets...) سيستدعي هذه الخدمة
بدل الوصول المباشر لقاعدة البيانات أو استخدام IDs مكتوبة يدويًا.
"""
from sqlalchemy import select

from bot.database.engine import async_session
from bot.database.models.guild import Guild


async def get_or_create_guild(guild_id: int) -> Guild:
    """يجلب إعدادات السيرفر، أو ينشئها فارغة إذا كانت أول مرة."""
    async with async_session() as session:
        result = await session.execute(
            select(Guild).where(Guild.guild_id == guild_id)
        )
        guild = result.scalar_one_or_none()

        if guild is None:
            guild = Guild(guild_id=guild_id)
            session.add(guild)
            await session.commit()
            await session.refresh(guild)

        return guild


async def set_welcome_channel(guild_id: int, channel_id: int) -> None:
    """يحفظ قناة الترحيب لهذا السيرفر."""
    async with async_session() as session:
        result = await session.execute(
            select(Guild).where(Guild.guild_id == guild_id)
        )
        guild = result.scalar_one_or_none()

        if guild is None:
            guild = Guild(guild_id=guild_id, welcome_channel_id=channel_id)
            session.add(guild)
        else:
            guild.welcome_channel_id = channel_id

        await session.commit()