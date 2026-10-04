"""Guild-scoped forbidden-word detection and idempotent moderation/Member XP changes."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import timedelta

import discord
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from bot.core.logger import logger
from bot.database.engine import async_session
from bot.database.models.member_xp import MemberXP
from bot.database.models.phase14 import ModerationAction
from bot.services import config_service
from bot.services.logging_service import LogEvent, LogType, SendResult, send_log
from bot.services.member_xp_service import (
    apply_member_xp_delta_in_session,
    get_member_xp_mutation_lock,
    process_member_xp_change,
)

_CACHE_TTL_SECONDS = 30
_WORD_LIMIT = 40
_WORD_LENGTH_LIMIT = 80
_cache: dict[int, tuple[float, "AutoModConfig"]] = {}


@dataclass(frozen=True)
class AutoModConfig:
    enabled: bool
    badwords_enabled: bool
    ignore_staff: bool
    words: tuple[str, ...]
    patterns: tuple[tuple[str, re.Pattern[str]], ...]
    xp_penalty: int
    log_channel_id: int | None
    staff_role_ids: frozenset[int]


def invalidate_automod_cache(guild_id: int) -> None:
    _cache.pop(guild_id, None)


def normalize_word(value: str) -> str:
    word = value.strip()
    if not word or len(word) > _WORD_LENGTH_LIMIT or "\n" in word or "\r" in word:
        raise ValueError(f"Enter one word or short phrase, up to {_WORD_LENGTH_LIMIT} characters.")
    return word


def word_pattern(word: str) -> re.Pattern[str]:
    escaped = re.escape(word)
    left = r"(?<!\w)" if word[0].isalnum() or word[0] == "_" else ""
    right = r"(?!\w)" if word[-1].isalnum() or word[-1] == "_" else ""
    return re.compile(left + escaped + right, re.IGNORECASE)


async def get_automod_config(guild_id: int) -> AutoModConfig:
    cached = _cache.get(guild_id)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    raw = await config_service.get_setting(guild_id, "automod_words")
    try:
        values = json.loads(str(raw or "[]"))
        if not isinstance(values, list):
            raise ValueError("word list must be a JSON list")
        words = tuple(dict.fromkeys(normalize_word(str(word)) for word in values))
    except (ValueError, TypeError, json.JSONDecodeError):
        logger.warning("Invalid AutoMod word list in guild %s; using an empty list.", guild_id)
        words = ()
    staff_role_ids: set[int] = set()
    for key in ("role_owner", "role_admin", "role_head_moderator", "role_moderator",
                "role_trial_moderator", "role_support", "role_staff"):
        role_id = await config_service.get_setting(guild_id, key)
        if role_id:
            staff_role_ids.add(int(role_id))
    try:
        from bot.services.staff_promotion_service import get_rank_config
        ranks = await get_rank_config(guild_id)
        staff_role_ids.update(rank.role_id for rank in ranks.values())
    except Exception:
        logger.exception("Could not load Staff ranks for AutoMod ignore setting in guild %s", guild_id)
    config = AutoModConfig(
        enabled=bool(await config_service.get_setting(guild_id, "automod_enabled")),
        badwords_enabled=bool(await config_service.get_setting(guild_id, "automod_block_badwords")),
        ignore_staff=bool(await config_service.get_setting(guild_id, "automod_ignore_staff")),
        words=words,
        patterns=tuple((word, word_pattern(word)) for word in words),
        xp_penalty=int(await config_service.get_setting(guild_id, "automod_warn_xp_penalty") or 0),
        log_channel_id=await config_service.get_setting(guild_id, "automod_log_channel"),
        staff_role_ids=frozenset(staff_role_ids),
    )
    _cache[guild_id] = (time.monotonic() + _CACHE_TTL_SECONDS, config)
    return config


async def _log(guild: discord.Guild, event: LogEvent) -> None:
    result = await send_log(guild, event)
    if result is not SendResult.SENT:
        await send_log(guild, LogEvent(
            LogType.MODERATION,
            event.title,
            description=event.description,
            user=event.user,
            target=event.target,
            staff=event.staff,
            reason=event.reason,
            xp_change=event.xp_change,
            fields=event.fields,
            ids=event.ids,
            color=event.color,
        ))


def _command_message(bot: discord.Client, message: discord.Message) -> bool:
    prefixes = bot.command_prefix if isinstance(bot.command_prefix, (str, tuple, list)) else ()
    if isinstance(prefixes, str):
        prefixes = (prefixes,)
    return any(prefix and message.content.startswith(prefix) for prefix in prefixes)


async def process_automod_message(message: discord.Message, bot: discord.Client) -> bool:
    """Return True for a detected rule so the existing Member XP listener skips the message."""
    if (
        message.guild is None
        or not isinstance(message.author, discord.Member)
        or message.author.bot
        or message.webhook_id is not None
        or message.is_system()
        or _command_message(bot, message)
    ):
        return False
    config = await get_automod_config(message.guild.id)
    if not config.enabled or not config.badwords_enabled or not config.patterns:
        return False
    if config.ignore_staff:
        if (message.author.id == message.guild.owner_id or message.author.guild_permissions.administrator
                or any(role.id in config.staff_role_ids for role in message.author.roles)):
            return False
    match = next(((word, found) for word, pattern in config.patterns
                  if (found := pattern.search(message.content)) is not None), None)
    if match is None:
        return False
    detected_word, _ = match
    reason = f"AutoMod: Use of a prohibited word ({detected_word})."
    deletion_ok = True
    try:
        await message.delete(reason="AutoMod: prohibited-word filter")
    except discord.NotFound:
        deletion_ok = False
    except (discord.Forbidden, discord.HTTPException):
        deletion_ok = False
        logger.exception("AutoMod could not delete message %s in guild %s", message.id, message.guild.id)

    member = message.author
    warning_number = 0
    previous_warnings = 0
    xp_before = xp_after = xp_applied = 0
    xp_result = None
    should_ban = False
    lock = get_member_xp_mutation_lock(message.guild.id, member.id)
    await config_service.get_or_create_guild(message.guild.id)
    try:
        async with lock:
            async with async_session() as session:
                async with session.begin():
                    existing = await session.scalar(select(ModerationAction.id).where(
                        ModerationAction.guild_id == message.guild.id,
                        ModerationAction.message_id == message.id,
                    ))
                    if existing is not None:
                        return True
                    warning_query = select(func.count(ModerationAction.id)).where(
                        ModerationAction.guild_id == message.guild.id,
                        ModerationAction.target_id == member.id,
                        ModerationAction.action_type == "warn",
                    )
                    expiry_days = int(await config_service.get_setting(message.guild.id, "warn_expire_days") or 0)
                    if expiry_days > 0:
                        warning_query = warning_query.where(
                            ModerationAction.created_at >= discord.utils.utcnow() - timedelta(days=expiry_days)
                        )
                    previous_warnings = int(await session.scalar(warning_query) or 0)
                    if previous_warnings >= 3:
                        should_ban = True
                        session.add(ModerationAction(
                            guild_id=message.guild.id, target_id=member.id,
                            moderator_id=bot.user.id if bot.user else 0,
                            action_type="automod_ban_pending", source="AutoMod", reason=reason,
                            message_id=message.id,
                        ))
                    else:
                        warning_number = previous_warnings + 1
                        session.add(ModerationAction(
                            guild_id=message.guild.id, target_id=member.id,
                            moderator_id=bot.user.id if bot.user else 0,
                            action_type="warn", source="AutoMod", reason=reason,
                            message_id=message.id,
                        ))
                        xp_row = await session.get(MemberXP, (message.guild.id, member.id))
                        xp_before = xp_row.total_xp if xp_row else 0
                        xp_result = await apply_member_xp_delta_in_session(
                            session, member, -config.xp_penalty,
                            source="automod_warn", event_id=message.id, reason=reason,
                        )
                        if xp_result is None:
                            raise RuntimeError("Member XP event was already recorded without its AutoMod warning.")
                        xp_after = xp_result.new_total
                        xp_applied = -xp_result.applied_amount
    except IntegrityError:
        # Unique message claims prevent a duplicate dispatch or concurrent gateway event from repeating effects.
        logger.info("Ignored duplicate AutoMod message event %s in guild %s.", message.id, message.guild.id)
        return True

    if not deletion_ok:
        await _log(message.guild, LogEvent(
            LogType.AUTOMOD, "⚠️ AutoMod Message Deletion Failed", target=member,
            reason=reason, fields=[("Detected rule", detected_word, True)],
            ids={"Message ID": message.id, "Guild ID": message.guild.id},
        ))
    if should_ban:
        cog = bot.get_cog("Phase14ModerationCog")
        if cog is None:
            logger.error("AutoMod reached the ban threshold but Phase14ModerationCog is unavailable.")
            return True
        await cog.automod_ban_member(
            member, message_id=message.id, detected_word=detected_word,
            previous_warnings=previous_warnings, message_deleted=deletion_ok,
        )
        return True

    if xp_result is not None and xp_result.old_level != xp_result.new_level:
        await process_member_xp_change(member, xp_result, source_channel=message.channel)
    dm_ok = True
    try:
        await member.send(
            f"Your message was removed because it contained a prohibited word.\n\n"
            f"You have received warning {warning_number} of 3.\n"
            f"Reason: {reason}\n"
            f"Member XP penalty: -{xp_applied} XP (configured penalty: {config.xp_penalty}).\n"
            f"Current warning count: {warning_number}/3. Your 4th violation will result in a ban."
        )
    except (discord.Forbidden, discord.HTTPException):
        dm_ok = False
        logger.warning("AutoMod warning DM failed for member %s in guild %s", member.id, message.guild.id)
    await _log(message.guild, LogEvent(
        LogType.AUTOMOD, "⚠️ AutoMod Warning", target=member, staff=bot.user,
        reason=reason, xp_change=-xp_applied,
        fields=[
            ("Source", "AutoMod", True), ("Warning", f"{warning_number}/3", True),
            ("Detected rule", detected_word, True), ("XP before", f"{xp_before}", True),
            ("Configured penalty", f"-{config.xp_penalty}", True), ("XP after", f"{xp_after}", True),
            ("Message deleted", "Yes" if deletion_ok else "No — permission/API failure", True),
            ("DM", "Sent" if dm_ok else "Failed / unavailable", True),
        ],
        ids={"Message ID": message.id, "Guild ID": message.guild.id},
    ))
    return True
