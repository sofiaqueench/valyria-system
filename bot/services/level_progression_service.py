"""Level milestone role synchronization, announcements, and XP logging."""
from __future__ import annotations

import json

import discord

from bot.core.logger import logger
from bot.services import config_service
from bot.services.logging_service import LogEvent, LogType, send_log

_BAD_EMOJI_WARNED: set[tuple[int, str]] = set()
_BAD_ROLE_WARNED: set[tuple[int, int, int]] = set()


async def get_level_role_mapping(guild_id: int) -> dict[int, int]:
    raw = await config_service.get_setting(guild_id, "level_roles")
    try:
        decoded = json.loads(str(raw or "{}"))
        if not isinstance(decoded, dict):
            raise ValueError("expected JSON object")
        mapping: dict[int, int] = {}
        for level_text, role_id in decoded.items():
            level, role = int(level_text), int(role_id)
            if level > 0 and role > 0:
                mapping[level] = role
        return mapping
    except (TypeError, ValueError, json.JSONDecodeError):
        logger.warning("Invalid level_roles configuration for guild %s; skipping level roles.", guild_id)
        return {}


async def get_current_level_role(guild: discord.Guild, level: int) -> discord.Role | None:
    mapping = await get_level_role_mapping(guild.id)
    choices = sorted(((milestone, guild.get_role(role_id)) for milestone, role_id in mapping.items() if milestone <= level), reverse=True)
    for _, role in choices:
        if role is not None:
            return role
    return None


async def _level_emoji(guild: discord.Guild) -> str:
    configured = await config_service.get_setting(guild.id, "level_up_emoji")
    if not configured:
        return "🎉"
    configured = str(configured).strip()
    try:
        emoji = discord.PartialEmoji.from_str(configured)
        if emoji.id is None:
            if configured.startswith("<"):
                raise ValueError("malformed custom emoji")
            return configured
        guild_emoji = guild.get_emoji(emoji.id)
        if guild_emoji is None:
            raise ValueError("custom emoji is not available in this server")
        return str(guild_emoji)
    except (TypeError, ValueError) as exc:
        warning_key = (guild.id, configured)
        if warning_key not in _BAD_EMOJI_WARNED:
            _BAD_EMOJI_WARNED.add(warning_key)
            logger.warning("Invalid level_up_emoji in guild %s: %s; using fallback.", guild.id, exc)
        return "🎉"


async def _sync_roles(
    member: discord.Member,
    mapping: dict[int, int],
    current_level: int,
) -> tuple[discord.Role | None, bool, list[discord.Role]]:
    guild = member.guild
    configured_role_ids = set(mapping.values())
    current = [role for role in member.roles if role.id in configured_role_ids]
    valid_choices = sorted(
        ((milestone, guild.get_role(role_id)) for milestone, role_id in mapping.items() if milestone <= current_level),
        reverse=True,
    )
    desired_level: int | None = None
    desired_role: discord.Role | None = None
    for milestone, role in valid_choices:
        if role is not None:
            desired_level, desired_role = milestone, role
            break
        bad_key = (guild.id, milestone, mapping[milestone])
        if bad_key not in _BAD_ROLE_WARNED:
            _BAD_ROLE_WARNED.add(bad_key)
            logger.warning("Configured level role %s for level %s is missing in guild %s.", mapping[milestone], milestone, guild.id)

    to_remove = [role for role in current if desired_role is None or role.id != desired_role.id]
    needs_add = desired_role is not None and all(role.id != desired_role.id for role in current)
    if not to_remove and not needs_add:
        return desired_role, True, current

    bot_member = guild.me
    changing_roles = [*to_remove, *([desired_role] if needs_add and desired_role else [])]
    if (
        bot_member is None
        or not bot_member.guild_permissions.manage_roles
        or any(bot_member.top_role <= role for role in changing_roles)
    ):
        logger.warning("Cannot manage configured Level Roles in guild %s; check Manage Roles and role hierarchy.", guild.id)
        return desired_role, False, current

    try:
        if to_remove:
            await member.remove_roles(*to_remove, reason=f"Level role sync at level {current_level}")
        if needs_add and desired_role is not None:
            await member.add_roles(desired_role, reason=f"Reached member level {desired_level}")
    except discord.HTTPException:
        logger.exception("Discord rejected Level Role update for member %s in guild %s.", member.id, guild.id)
        return desired_role, False, current
    return desired_role, True, current


async def _send_level_announcement(
    member: discord.Member,
    *,
    level: int,
    role: discord.Role | None,
    source_channel: discord.abc.Messageable | None,
) -> None:
    guild = member.guild
    if not await config_service.get_setting(guild.id, "level_up_enabled"):
        return
    channel_id = await config_service.get_setting(guild.id, "level_up_channel")
    channel = guild.get_channel(channel_id) if channel_id else None
    if not isinstance(channel, discord.TextChannel) and isinstance(source_channel, discord.TextChannel):
        channel = source_channel
    if not isinstance(channel, discord.TextChannel):
        return

    emoji = await _level_emoji(guild)
    embed = discord.Embed(
        title=f"{emoji} Congratulations!",
        description=f"{member.mention}\n\nYou reached **Level {level}**!",
        color=discord.Color.gold(),
    )
    embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
    embed.set_thumbnail(url=member.display_avatar.url)
    if role is not None:
        embed.add_field(name="New Level Role", value=role.mention, inline=False)

    banner_url = await config_service.get_setting(guild.id, "level_up_banner")
    if banner_url:
        try:
            embed.set_image(url=str(banner_url))
        except (TypeError, ValueError, discord.DiscordException):
            logger.warning("Invalid level_up_banner URL in guild %s; sending without banner.", guild.id)

    try:
        await channel.send(
            content=member.mention,
            embed=embed,
            allowed_mentions=discord.AllowedMentions(users=[member]),
        )
    except discord.HTTPException:
        logger.exception("Could not send level-up announcement for member %s.", member.id)


async def process_level_progression(
    member: discord.Member,
    result,
    *,
    source_channel: discord.abc.Messageable | None = None,
) -> None:
    """Apply the highest configured role and announce each level increase once."""
    guild = member.guild
    mapping = await get_level_role_mapping(guild.id)
    role, role_sync_ok, previous_roles = await _sync_roles(member, mapping, result.new_level)

    if result.new_level > result.old_level:
        await _send_level_announcement(
            member,
            level=result.new_level,
            role=role if role_sync_ok else None,
            source_channel=source_channel,
        )

    if result.new_level != result.old_level:
        fields = [
            ("Previous level", str(result.old_level), True),
            ("Current level", str(result.new_level), True),
            ("Member XP", str(result.new_total), True),
        ]
        if role is not None and role_sync_ok:
            fields.append(("Level role", role.mention, False))
        elif mapping and not role_sync_ok:
            fields.append(("Level role", "Not changed — check bot permissions and role hierarchy", False))
        await send_log(guild, LogEvent(
            LogType.XP,
            "🎚️ Member Level Increased" if result.new_level > result.old_level else "🎚️ Member Level Adjusted",
            user=member,
            xp_change=result.applied_amount,
            fields=fields,
        ))
