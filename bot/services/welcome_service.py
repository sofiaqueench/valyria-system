"""Build Valyria's configurable welcome card using native Discord assets."""
from __future__ import annotations

import re

import discord

from bot.core.logger import logger
from bot.services import config_service

_EMOJI_KEYS = {
    "WELCOME_EMOJI_WELCOME": ("welcome_emoji_welcome", "👑"),
    "WELCOME_EMOJI_MEMBER": ("welcome_emoji_member", "👤"),
    "WELCOME_EMOJI_SERVER": ("welcome_emoji_server", "🏰"),
    "WELCOME_EMOJI_RULES": ("welcome_emoji_rules", "📜"),
    "WELCOME_EMOJI_VERIFICATION": ("welcome_emoji_verification", "✅"),
}
_BAD_EMOJI_WARNED: set[tuple[int, str]] = set()
_PLACEHOLDER = re.compile(r"\{([A-Za-z_]+)\}")


def _format(text: str, values: dict[str, str]) -> str:
    """Replace known placeholders while leaving unknown ones readable."""
    return _PLACEHOLDER.sub(lambda match: values.get(match.group(1), match.group(0)), text)


async def _emoji(guild: discord.Guild, config_key: str, fallback: str) -> str:
    configured = await config_service.get_setting(guild.id, config_key)
    if not configured:
        return fallback
    try:
        parsed = discord.PartialEmoji.from_str(str(configured))
        if parsed.id is None:
            raise ValueError("expected Discord custom emoji syntax such as <:name:id>")
        emoji = guild.get_emoji(parsed.id)
        if emoji is None:
            raise ValueError("emoji is unavailable in this server (missing, deleted, or external)")
        return str(emoji)
    except (TypeError, ValueError) as exc:
        warning_key = (guild.id, config_key)
        if warning_key not in _BAD_EMOJI_WARNED:
            _BAD_EMOJI_WARNED.add(warning_key)
            logger.warning("Invalid %s configuration in guild %s: %s; using Unicode fallback.", config_key, guild.id, exc)
        return fallback


async def build_welcome_embed(member: discord.Member) -> discord.Embed:
    guild = member.guild
    settings = {
        token: await _emoji(guild, key, fallback)
        for token, (key, fallback) in _EMOJI_KEYS.items()
    }
    joined_at = member.joined_at
    created_at = member.created_at
    values = {
        **settings,
        "user": str(member),
        "mention": member.mention,
        "username": member.name,
        "display_name": member.display_name,
        "user_id": str(member.id),
        "server": guild.name,
        "server_id": str(guild.id),
        "member_count": str(guild.member_count or len(guild.members)),
        "created_at": discord.utils.format_dt(created_at, style="D") if created_at else "Unknown",
        "joined_at": discord.utils.format_dt(joined_at, style="D") if joined_at else "Unknown",
        "inviter": "Unknown",
    }

    title = await config_service.get_setting(guild.id, "welcome_embed_title")
    message = await config_service.get_setting(guild.id, "welcome_message")
    color = await config_service.get_setting(guild.id, "welcome_embed_color")
    embed = discord.Embed(
        title=_format(str(title or "👑 Welcome to {server}"), values)[:256],
        description=_format(str(message or "Welcome {mention} to **{server}**!"), values)[:4096],
        color=discord.Color(int(color if color is not None else 5793266)),
        timestamp=discord.utils.utcnow() if await config_service.get_setting(guild.id, "welcome_embed_timestamp") else None,
    )

    member_emoji = values["WELCOME_EMOJI_MEMBER"]
    server_emoji = values["WELCOME_EMOJI_SERVER"]
    rules_emoji = values["WELCOME_EMOJI_RULES"]
    verify_emoji = values["WELCOME_EMOJI_VERIFICATION"]
    embed.add_field(name=f"{member_emoji} Member", value=f"{member.mention}\n`{member.display_name}`", inline=True)
    embed.add_field(name=f"{server_emoji} Server", value=f"{guild.name}\n{values['member_count']} members", inline=True)
    rules_channel_id = await config_service.get_setting(guild.id, "verification_rules_channel")
    rules_channel = guild.get_channel(rules_channel_id) if rules_channel_id else None
    rules_text = f"Please read the rules in {rules_channel.mention}." if rules_channel else "Please read the server rules."
    embed.add_field(name=f"{rules_emoji} Rules", value=rules_text, inline=True)
    verify_channel_id = await config_service.get_setting(guild.id, "verification_channel")
    verify_channel = guild.get_channel(verify_channel_id) if verify_channel_id else None
    verify_text = f"Complete verification in {verify_channel.mention}." if verify_channel else "Complete the verification process to access the server."
    embed.add_field(name=f"{verify_emoji} Verification", value=verify_text, inline=False)

    use_avatar = await config_service.get_setting(guild.id, "welcome_use_member_avatar")
    legacy_avatar = await config_service.get_setting(guild.id, "welcome_show_avatar")
    if use_avatar and legacy_avatar:
        try:
            embed.set_thumbnail(url=member.display_avatar.url)
        except (AttributeError, discord.DiscordException):
            logger.warning("Could not read member avatar for %s in guild %s.", member.id, guild.id)

    if await config_service.get_setting(guild.id, "welcome_use_server_banner"):
        try:
            banner_url = guild.banner.url if guild.banner else None
        except (AttributeError, discord.DiscordException):
            banner_url = None
        if not banner_url:
            banner_url = await config_service.get_setting(guild.id, "welcome_fallback_image_url")
        if banner_url:
            try:
                embed.set_image(url=str(banner_url))
            except (TypeError, ValueError, discord.DiscordException):
                logger.warning("Invalid welcome banner/fallback image URL in guild %s; omitting image.", guild.id)

    footer = await config_service.get_setting(guild.id, "welcome_embed_footer")
    footer_text = _format(str(footer or "Valyria"), values)[:2048]
    icon_url = None
    if await config_service.get_setting(guild.id, "welcome_use_server_icon"):
        try:
            icon_url = guild.icon.url if guild.icon else None
        except (AttributeError, discord.DiscordException):
            pass
    embed.set_footer(text=footer_text, icon_url=icon_url)
    return embed
