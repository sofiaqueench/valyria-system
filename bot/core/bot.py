"""
الكلاس الرئيسي للبوت. مسؤول فقط عن الاتصال وتحميل الـ Cogs — لا منطق أعمال هنا.
"""
import discord
from discord.ext import commands

from bot.config.settings import settings
from bot.core.logger import logger


class DiscordBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True           # لازم لـ Welcome, Invite tracking, Verification
        intents.message_content = True   # لازم لـ Leveling من الرسائل و AutoMod

        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        """يُستدعى تلقائيًا مرة واحدة قبل أن يتصل البوت — هنا نحمّل الـ Cogs ونزامن الأوامر."""
        await self.load_extension("bot.cogs.ping_cog")
        logger.info("تم تحميل جميع الـ Cogs بنجاح.")

        if settings.guild_id:
            guild = discord.Object(id=settings.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info(f"تمت مزامنة {len(synced)} أمر(أوامر) مع السيرفر التجريبي.")
        else:
            synced = await self.tree.sync()
            logger.info(f"تمت مزامنة {len(synced)} أمر(أوامر) عالميًا.")

    async def on_ready(self):
        logger.info(f"البوت متصل الآن باسم {self.user} (ID: {self.user.id})")