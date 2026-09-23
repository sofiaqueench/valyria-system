"""
معالج أخطاء موحّد لكل أوامر Slash. يُسجَّل مرة واحدة في bot.py (tree.on_error).
بهذا لا نكرر كود معالجة الأخطاء داخل كل Cog.
"""
import discord
from discord import app_commands

from bot.core.errors import BotError
from bot.core.logger import logger
from bot.services.logging_service import log_error


async def handle_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    # الخطأ الأصلي يأتي داخل CommandInvokeError، نفتحه لنرى السبب الحقيقي
    original = getattr(error, "original", error)

    if isinstance(error, app_commands.MissingPermissions):
        message = "❌ ليست لديك الصلاحية لاستعمال هذا الأمر."
    elif isinstance(error, app_commands.CommandOnCooldown):
        message = f"⏳ انتظر {error.retry_after:.0f} ثانية قبل استعمال هذا الأمر مجددًا."
    elif isinstance(error, app_commands.CheckFailure):
        message = "❌ لا يمكنك استعمال هذا الأمر هنا."
    elif isinstance(original, BotError):
        message = f"❌ {original}"  # أخطاء مقصودة ورسالتها مكتوبة للمستخدم (مثل ConfigError)
    else:
        command_name = interaction.command.qualified_name if interaction.command else "unknown"
        logger.error("خطأ غير متوقع في الأمر /%s", command_name, exc_info=original)
        await log_error(
            interaction.guild,
            "Command Error",
            original,
            context=f"/{command_name} — used by {interaction.user} ({interaction.user.id})",
        )
        message = "❌ حدث خطأ غير متوقع، تم تسجيله."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        logger.warning("تعذّر إرسال رسالة الخطأ للمستخدم (ربما انتهت صلاحية التفاعل).")
