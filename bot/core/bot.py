"""
الكلاس الرئيسي للبوت. مسؤول فقط عن الاتصال وتحميل الـ Cogs — لا منطق أعمال هنا.
"""
import importlib
import pkgutil

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
        intents.presences = True
        intents.message_content = True
        intents.invites = True
        intents.moderation = True

        # max_messages: عدد الرسائل التي يحفظها البوت في الذاكرة. كلما زاد، زادت فرصة عرض
        # محتوى الرسالة المحذوفة في السجلات (الافتراضي 1000).
        super().__init__(command_prefix="!", intents=intents, max_messages=5000)

    async def setup_hook(self):
        """يُستدعى تلقائيًا مرة واحدة قبل أن يتصل البوت — هنا نهيئ القاعدة ونحمّل الـ Cogs ونزامن الأوامر."""
        await init_db()
        logger.info("تم تهيئة قاعدة البيانات بنجاح.")

        self.tree.on_error = handle_app_command_error  # معالج أخطاء موحّد لكل أوامر Slash

        cogs_package = importlib.import_module("bot.cogs")
        extensions = sorted(
            f"{cogs_package.__name__}.{module.name}"
            for module in pkgutil.iter_modules(cogs_package.__path__)
            if not module.ispkg and not module.name.startswith("_")
        )
        for extension in extensions:
            try:
                await self.load_extension(extension)
            except commands.ExtensionError:
                logger.exception("تعذر تحميل الامتداد %s.", extension)
                raise
        logger.info("تم تحميل %s امتدادًا تلقائيًا.", len(extensions))

        command_names = [command.qualified_name for command in self.tree.walk_commands()]
        duplicates = sorted({name for name in command_names if command_names.count(name) > 1})
        if duplicates:
            raise RuntimeError(f"Duplicate slash command registration detected: {', '.join(duplicates)}")
        logger.info("تم التحقق من عدم تكرار أوامر Slash (%s أمرًا).", len(command_names))

        guild = discord.Object(id=settings.guild_id)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        logger.info("تمت مزامنة %s أمر(أوامر) مع السيرفر %s.", len(synced), settings.guild_id)

    async def on_ready(self):
        logger.info(f"البوت متصل الآن باسم {self.user} (ID: {self.user.id})")

    async def close(self):
        """يُستدعى عند إيقاف البوت: نُعلن الإيقاف في سجل البوت ثم نغلق الاتصال."""
        if not self.is_closed():
            await announce_shutdown(self)
        await super().close()
