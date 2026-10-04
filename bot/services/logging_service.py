"""
خدمة السجلات المركزية (Central Logging Service).

كل نظام في البوت (Verification, Moderation, Tickets, Jail...) يرسل سجلاته من هنا
بدل أن يكتب Embed خاصًا به. مثال:

    await send_log(guild, LogEvent(
        LogType.MODERATION, "🔨 Member Warned",
        user=member, staff=moderator, reason="Spam",
    ))

الخدمة تعرف بنفسها أي روم تستعمل (من الإعدادات log_*)، وتبني الـ Embed بشكل موحّد،
ولا تُسقط البوت أبدًا إذا كان الروم غير مضبوط أو لا توجد صلاحية.
"""
import asyncio
import traceback
from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum
from typing import Callable

import discord

from bot.config.schema import SETTINGS
from bot.core.logger import logger
from bot.services.config_service import get_setting

FOOTER_TEXT = "Valyria Logs"


class LogType(str, Enum):
    """نوع السجل. القيمة = مفتاح الإعداد الذي يحدد الروم (في schema.py)."""

    MEMBER = "log_member"
    VERIFICATION = "log_verification"
    MODERATION = "log_moderation"
    AUTOMOD = "automod_log_channel"
    TICKET = "log_ticket"
    VOICE = "log_voice"
    INVITE = "log_invite"
    XP = "log_xp"
    MESSAGE = "log_message"
    SECURITY = "log_security"
    BOT = "log_bot"
    CHANNEL = "log_channel"
    ROLE = "log_role"


# نتأكد عند التشغيل أن كل نوع له إعداد فعلي (يمنع الأخطاء الإملائية)
for _log_type in LogType:
    if _log_type.value not in SETTINGS:
        raise RuntimeError(f"LogType {_log_type.name} بلا إعداد في schema.py: {_log_type.value}")

_COLORS = {
    LogType.MEMBER: 0x3498DB,
    LogType.VERIFICATION: 0x2ECC71,
    LogType.MODERATION: 0xE67E22,
    LogType.AUTOMOD: 0xE74C3C,
    LogType.TICKET: 0x9B59B6,
    LogType.VOICE: 0x1ABC9C,
    LogType.INVITE: 0x5865F2,
    LogType.XP: 0xF1C40F,
    LogType.MESSAGE: 0x95A5A6,
    LogType.SECURITY: 0xE74C3C,
    LogType.BOT: 0x34495E,
    LogType.CHANNEL: 0x16A085,
    LogType.ROLE: 0x8E44AD,
}


class SendResult(str, Enum):
    SENT = "sent"
    NOT_CONFIGURED = "not_configured"
    FAILED = "failed"


@dataclass
class LogEvent:
    """وصف حدث واحد. الحقول الفارغة لا تظهر في الـ Embed."""

    log_type: LogType
    title: str
    description: str | None = None
    user: discord.abc.User | None = None      # صاحب الحدث (العضو المعني)
    user_label: str = "User"                  # اسم الحقل (مثلًا Author للرسائل)
    target: discord.abc.User | None = None    # الهدف (مثلًا من تم تحذيره)
    staff: discord.abc.User | None = None     # الإداري المنفّذ
    reason: str | None = None
    xp_change: int | None = None
    fields: list[tuple[str, str, bool]] = field(default_factory=list)  # (الاسم, القيمة, inline)
    ids: dict[str, int | str] = field(default_factory=dict)            # معرّفات مفيدة
    color: int | None = None
    thumbnail_url: str | None = None


def truncate(text: str, limit: int) -> str:
    """يقص النص ليناسب حدود Discord (الحقل 1024 حرفًا، والوصف 4096)، ولا يرجع نصًا فارغًا."""
    text = text.strip() or "\u200b"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_user(user: discord.abc.User) -> str:
    return f"{user.mention} (`{user.name}` · `{user.id}`)"


def build_embed(event: LogEvent) -> discord.Embed:
    embed = discord.Embed(
        title=event.title,
        description=truncate(event.description, 4000) if event.description else None,
        color=event.color if event.color is not None else _COLORS[event.log_type],
        timestamp=discord.utils.utcnow(),
    )

    if event.user is not None:
        embed.add_field(name=event.user_label, value=format_user(event.user), inline=True)
    if event.target is not None:
        embed.add_field(name="Target", value=format_user(event.target), inline=True)
    if event.staff is not None:
        embed.add_field(name="Staff", value=format_user(event.staff), inline=True)
    if event.reason:
        embed.add_field(name="Reason", value=truncate(event.reason, 1024), inline=False)
    if event.xp_change is not None:
        embed.add_field(name="XP Change", value=f"{event.xp_change:+d}", inline=True)

    for name, value, inline in event.fields:
        embed.add_field(name=name, value=truncate(value, 1024), inline=inline)

    if event.ids:
        lines = "\n".join(f"{name}: `{value}`" for name, value in event.ids.items())
        embed.add_field(name="IDs", value=truncate(lines, 1024), inline=False)

    if event.thumbnail_url:
        embed.set_thumbnail(url=event.thumbnail_url)

    embed.set_footer(text=FOOTER_TEXT)
    return embed


async def send_log(guild: discord.Guild, event: LogEvent) -> SendResult:
    """يرسل الحدث إلى روم السجل المناسب. لا يرفع أخطاء أبدًا."""
    channel_id = await get_setting(guild.id, event.log_type.value)
    if channel_id is None:
        return SendResult.NOT_CONFIGURED  # طبيعي: الأدمن لم يضبط هذا السجل بعد

    channel = guild.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        logger.warning(
            "روم السجل %s (%s) غير موجود أو ليس روم نصيًا في السيرفر %s.",
            event.log_type.value, channel_id, guild.id,
        )
        return SendResult.FAILED

    try:
        # allowed_mentions.none: حتى لا يقوم أي منشن داخل محتوى الرسالة بإزعاج أحد
        await channel.send(embed=build_embed(event), allowed_mentions=discord.AllowedMentions.none())
    except discord.Forbidden:
        logger.warning(
            "لا أملك صلاحية الإرسال في روم السجل %s (#%s) في السيرفر %s.",
            event.log_type.value, channel.name, guild.id,
        )
        return SendResult.FAILED
    except discord.HTTPException:
        logger.exception("فشل إرسال السجل %s في السيرفر %s.", event.log_type.value, guild.id)
        return SendResult.FAILED

    return SendResult.SENT


async def broadcast(guilds: list[discord.Guild], event: LogEvent) -> None:
    """يرسل نفس الحدث لكل السيرفرات (مثل: البوت اشتغل / البوت توقف)."""
    for guild in guilds:
        await send_log(guild, event)


async def is_log_channel(guild_id: int, channel_id: int) -> bool:
    """هل هذا الروم هو أحد رومات السجلات؟ نستعملها لمنع حلقة: حذف رسالة سجل ← سجل جديد ← ..."""
    for log_type in LogType:
        if await get_setting(guild_id, log_type.value) == channel_id:
            return True
    return False


async def log_error(
    guild: discord.Guild | None, title: str, error: BaseException, context: str | None = None
) -> None:
    """يسجّل خطأ في سجل البوت (log_bot). إن لم يوجد سيرفر فقط نكتفي بملف bot.log."""
    if guild is None:
        return

    trace = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    fields = [("Context", context, False)] if context else []
    event = LogEvent(
        LogType.BOT,
        f"🛑 {title}",
        description=f"```py\n{trace[-1800:]}\n```",  # آخر 1800 حرف (فيها سبب الخطأ)
        fields=fields,
        color=0xE74C3C,
    )
    await send_log(guild, event)


async def announce_shutdown(bot: discord.Client) -> None:
    """يخبر سجل البوت أن البوت سيتوقف. له مهلة 5 ثوانٍ حتى لا يعطّل الإغلاق."""
    event = LogEvent(LogType.BOT, "🔴 Bot Shutting Down", description=f"{bot.user} is going offline.")
    try:
        await asyncio.wait_for(broadcast(bot.guilds, event), timeout=5)
    except Exception:
        logger.warning("تعذّر إرسال رسالة الإيقاف إلى سجل البوت.")


async def find_audit_executor(
    guild: discord.Guild,
    action: discord.AuditLogAction,
    target_id: int,
    *,
    channel_id: int | None = None,
    predicate: Callable[[discord.AuditLogEntry], bool] | None = None,
    max_age_seconds: int = 10,
) -> tuple[discord.abc.User | None, str]:
    """
    يحاول معرفة من نفّذ إجراءً معيّنًا عبر Audit Log (أفضل جهد وليس ضمانًا).
    يرجع (المنفّذ, الحالة) والحالة: found / not_found / no_permission / error
    """
    cutoff = discord.utils.utcnow() - timedelta(seconds=max_age_seconds)
    try:
        async for entry in guild.audit_logs(limit=15, action=action):
            if entry.created_at < cutoff:
                break  # الإدخالات مرتبة من الأحدث، فما بعده أقدم
            if getattr(entry.target, "id", None) != target_id:
                continue
            if channel_id is not None:
                extra_channel = getattr(entry.extra, "channel", None)
                if getattr(extra_channel, "id", None) != channel_id:
                    continue
            if predicate is not None and not predicate(entry):
                continue
            if entry.user is None:
                continue
            return entry.user, "found"
        return None, "not_found"
    except discord.Forbidden:
        return None, "no_permission"
    except discord.HTTPException:
        logger.exception("فشل قراءة Audit Log في السيرفر %s.", guild.id)
        return None, "error"
