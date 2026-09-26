"""
الكلاس الرئيسي للبوت. مسؤول فقط عن الاتصال وتحميل الـ Cogs — لا منطق أعمال هنا.
"""
import discord
from discord.ext import commands

from bot.config.settings import settings
from bot.core.error_handler import handle_app_command_error
from bot.core.logger import logger
from bot.database.engine import init_db
from bot.services.logging_service import announce_shutdown


class DiscordBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True

        # max_messages: عدد الرسائل التي يحفظها البوت في الذاكرة. كلما زاد، زادت فرصة عرض
        # محتوى الرسالة المحذوفة في السجلات (الافتراضي 1000).
        super().__init__(command_prefix="!", intents=intents, max_messages=5000)

    async def setup_hook(self):
        """يُستدعى تلقائيًا مرة واحدة قبل أن يتصل البوت — هنا نهيئ القاعدة ونحمّل الـ Cogs ونزامن الأوامر."""
        await init_db()
        logger.info("تم تهيئة قاعدة البيانات بنجاح.")

        self.tree.on_error = handle_app_command_error  # معالج أخطاء موحّد لكل أوامر Slash

        await self.load_extension("bot.cogs.ping_cog")
        await self.load_extension("bot.cogs.config_cog")
        await self.load_extension("bot.cogs.log_events")
        await self.load_extension("bot.cogs.permissions_cog")
        await self.load_extension("bot.cogs.verification_cog")
        await self.load_extension("bot.cogs.welcome_cog")
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

    async def close(self):
        """يُستدعى عند إيقاف البوت: نُعلن الإيقاف في سجل البوت ثم نغلق الاتصال."""
        if not self.is_closed():
            await announce_shutdown(self)
        await super().close()
