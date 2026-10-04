"""Guild-aware spam detection which records warnings in Phase 14's moderation ledger."""
from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass

import discord
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from bot.core.logger import logger
from bot.database.engine import async_session
from bot.database.models.phase14 import ModerationAction
from bot.database.models.member_xp import MemberXP
from bot.services import config_service
from bot.services.automod_service import _command_message, get_automod_config
from bot.services.logging_service import LogEvent, LogType, SendResult, send_log
from bot.services.member_xp_service import (
    apply_member_xp_delta_in_session, get_member_xp_mutation_lock, process_member_xp_change,
)

_CACHE_TTL = 20.0
_MAX_USERS = 10_000
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_CUSTOM_EMOJI_RE = re.compile(r"<a?:[A-Za-z0-9_]{2,32}:\d{15,22}>")
_UNICODE_EMOJI_RE = re.compile(
    "[\\U0001F1E6-\\U0001FAFF\\u2600-\\u27BF\\u2300-\\u23FF]"
)


@dataclass(frozen=True)
class SpamSettings:
    enabled: bool
    messages: int
    seconds: int
    duplicate_limit: int
    duplicate_seconds: int
    mention_limit: int
    emoji_limit: int
    repeat_chars: int
    links_enabled: bool
    link_limit: int
    ignore_staff: bool
    staff_role_ids: frozenset[int]
    xp_penalty: int


@dataclass
class MessageState:
    recent: deque[tuple[float, str]]


_settings_cache: dict[int, tuple[float, SpamSettings]] = {}
_activity: dict[tuple[int, int], MessageState] = {}


def clear_antispam_cache(guild_id: int) -> None:
    _settings_cache.pop(guild_id, None)


async def get_antispam_settings(guild_id: int) -> SpamSettings:
    cached = _settings_cache.get(guild_id)
    now = time.monotonic()
    if cached and cached[0] > now:
        return cached[1]
    automod = await get_automod_config(guild_id)
    settings = SpamSettings(
        enabled=bool(await config_service.get_setting(guild_id, "antispam_enabled")),
        messages=int(await config_service.get_setting(guild_id, "automod_spam_messages") or 5),
        seconds=int(await config_service.get_setting(guild_id, "automod_spam_seconds") or 5),
        duplicate_limit=int(await config_service.get_setting(guild_id, "antispam_duplicate_limit") or 3),
        duplicate_seconds=int(await config_service.get_setting(guild_id, "antispam_duplicate_seconds") or 15),
        mention_limit=int(await config_service.get_setting(guild_id, "automod_mention_limit") or 5),
        emoji_limit=int(await config_service.get_setting(guild_id, "antispam_emoji_limit") or 12),
        repeat_chars=int(await config_service.get_setting(guild_id, "antispam_repeat_chars") or 20),
        links_enabled=bool(await config_service.get_setting(guild_id, "antispam_links_enabled")),
        link_limit=int(await config_service.get_setting(guild_id, "antispam_link_limit") or 5),
        ignore_staff=automod.ignore_staff,
        staff_role_ids=automod.staff_role_ids,
        xp_penalty=int(await config_service.get_setting(guild_id, "antispam_xp_penalty") or 0),
    )
    _settings_cache[guild_id] = (now + _CACHE_TTL, settings)
    return settings


def _ignore(message: discord.Message, bot: discord.Client) -> bool:
    return (message.guild is None or not isinstance(message.author, discord.Member) or message.author.bot
            or message.webhook_id is not None or message.is_system() or _command_message(bot, message))


def _reason(message: discord.Message, settings: SpamSettings, recent: deque[tuple[float, str]], now: float) -> str | None:
    content = message.content.strip()
    windowed = [text for stamp, text in recent if now - stamp <= settings.seconds]
    if len(windowed) + 1 >= settings.messages:
        return f"Anti-Spam: {settings.messages} messages sent within {settings.seconds} seconds."
    normalized = re.sub(r"[^\w]+", "", content.casefold())
    duplicate = [text for stamp, text in recent if now - stamp <= settings.duplicate_seconds and text == normalized]
    if normalized and len(duplicate) + 1 >= settings.duplicate_limit:
        return f"Anti-Spam: repeated identical message ({settings.duplicate_limit} times)."
    mentions = len(message.mentions) + len(message.role_mentions) + int(message.mention_everyone)
    if mentions > settings.mention_limit:
        return f"Anti-Spam: excessive mentions ({mentions}; limit {settings.mention_limit})."
    emoji_count = len(_CUSTOM_EMOJI_RE.findall(content)) + len(_UNICODE_EMOJI_RE.findall(content))
    if emoji_count > settings.emoji_limit:
        return f"Anti-Spam: excessive emoji ({emoji_count}; limit {settings.emoji_limit})."
    if re.search(rf"(.)\1{{{settings.repeat_chars - 1},}}", content, re.IGNORECASE):
        return f"Anti-Spam: repeated characters exceeded {settings.repeat_chars}."
    if settings.links_enabled:
        links = len(_URL_RE.findall(content))
        if links > settings.link_limit:
            return f"Anti-Spam: excessive links ({links}; limit {settings.link_limit})."
    return None


def _append_activity(key: tuple[int, int], now: float, normalized: str, settings: SpamSettings) -> deque[tuple[float, str]]:
    if len(_activity) >= _MAX_USERS and key not in _activity:
        stale = [k for k, state in _activity.items() if not state.recent or now - state.recent[-1][0] > 300]
        for old_key in stale:
            _activity.pop(old_key, None)
        if len(_activity) >= _MAX_USERS:
            oldest = min(_activity, key=lambda k: _activity[k].recent[-1][0] if _activity[k].recent else 0)
            _activity.pop(oldest, None)
    state = _activity.setdefault(key, MessageState(deque(maxlen=30)))
    max_age = max(settings.seconds, settings.duplicate_seconds)
    while state.recent and now - state.recent[0][0] > max_age:
        state.recent.popleft()
    state.recent.append((now, normalized))
    return state.recent


async def _log(guild: discord.Guild, event: LogEvent) -> None:
    result = await send_log(guild, event)
    if result is not SendResult.SENT:
        await send_log(guild, LogEvent(LogType.MODERATION, event.title, target=event.target, staff=event.staff,
                                       reason=event.reason, xp_change=event.xp_change, fields=event.fields, ids=event.ids))


async def process_antispam_message(message: discord.Message, bot: discord.Client) -> bool:
    """Handle one spam violation; True means the shared message pipeline must stop."""
    if _ignore(message, bot):
        return False
    member = message.author
    guild = message.guild
    settings = await get_antispam_settings(guild.id)
    if not settings.enabled:
        return False
    if settings.ignore_staff and (
        member.id == guild.owner_id or member.guild_permissions.administrator
        or any(role.id in settings.staff_role_ids for role in member.roles)
    ):
        return False
    now = time.monotonic()
    key = (guild.id, member.id)
    recent = _activity.get(key, MessageState(deque(maxlen=30))).recent
    reason = _reason(message, settings, recent, now)
    normalized = re.sub(r"[^\w]+", "", message.content.casefold())
    recent = _append_activity(key, now, normalized, settings)
    if not reason:
        return False

    deleted = True
    try:
        await message.delete(reason=reason)
    except discord.NotFound:
        deleted = False
    except (discord.Forbidden, discord.HTTPException):
        deleted = False
        logger.exception("Anti-Spam could not delete message %s in guild %s", message.id, guild.id)

    await config_service.get_or_create_guild(guild.id)
    warning_count = 0
    xp_result = None
    xp_before = xp_after = xp_applied = 0
    async with get_member_xp_mutation_lock(guild.id, member.id):
        try:
            async with async_session() as session:
                async with session.begin():
                    existing = await session.scalar(select(ModerationAction.id).where(
                        ModerationAction.guild_id == guild.id, ModerationAction.message_id == message.id))
                    if existing is not None:
                        return True
                    warning_count = int(await session.scalar(select(func.count(ModerationAction.id)).where(
                        ModerationAction.guild_id == guild.id, ModerationAction.target_id == member.id,
                        ModerationAction.action_type == "warn")) or 0)
                    session.add(ModerationAction(guild_id=guild.id, target_id=member.id,
                        moderator_id=bot.user.id if bot.user else 0, action_type="warn", source="AntiSpam",
                        reason=reason, message_id=message.id))
                    xp_row = await session.get(MemberXP, (guild.id, member.id))
                    xp_before = xp_row.total_xp if xp_row else 0
                    xp_result = await apply_member_xp_delta_in_session(session, member, -settings.xp_penalty,
                        source="antispam_warn", event_id=message.id, reason=reason)
                    if xp_result is None:
                        raise RuntimeError("Member XP event was already recorded without its Anti-Spam warning.")
                    xp_after = xp_result.new_total
                    xp_applied = -xp_result.applied_amount
        except IntegrityError:
            logger.info("Ignored duplicate Anti-Spam message event %s in guild %s", message.id, guild.id)
            return True

    dm_ok = False
    cog = bot.get_cog("Phase14ModerationCog")
    if cog is not None:
        dm_ok = await cog._dm(member, title=f"⚠️ Anti-Spam warning in {guild.name}", guild_name=guild.name,
            reason=reason, moderator=bot.user.name if bot.user else "AutoMod", action="Anti-Spam Warning")
    else:
        logger.error("Phase14 moderation cog unavailable for Anti-Spam DM in guild %s", guild.id)
    if xp_result and xp_result.old_level != xp_result.new_level:
        await process_member_xp_change(member, xp_result, source_channel=message.channel)
    await _log(guild, LogEvent(LogType.AUTOMOD, "⚠️ Anti-Spam Warning", target=member, staff=bot.user,
        reason=reason, xp_change=-xp_applied,
        fields=[("Source", "Anti-Spam", True), ("Warn number", str(warning_count + 1), True),
                ("Member XP before", str(xp_before), True), ("XP penalty", f"-{xp_applied} (configured {settings.xp_penalty})", True),
                ("Member XP after", str(xp_after), True), ("Message deleted", "Yes" if deleted else "No", True),
                ("DM", "Sent" if dm_ok else "Failed / unavailable", True)],
        ids={"Guild ID": guild.id, "Message ID": message.id, "Member ID": member.id}))
    return True
