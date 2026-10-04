"""Administrator controls for the guild-scoped forbidden-word AutoMod."""
from __future__ import annotations

import json

import discord
from discord import app_commands
from discord.ext import commands

from bot.core.permissions import StaffLevel, requires_level
from bot.services import config_service
from bot.services.automod_service import (
    _WORD_LIMIT,
    get_automod_config,
    invalidate_automod_cache,
    normalize_word,
)
from bot.services.logging_service import LogEvent, LogType, send_log


class AutoModCog(commands.Cog):
    automod = app_commands.Group(name="automod", description="Manage this server's prohibited-word filter", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _get_words(self, guild_id: int) -> list[str]:
        raw = await config_service.get_setting(guild_id, "automod_words")
        try:
            words = json.loads(str(raw or "[]"))
            if isinstance(words, list):
                return list(dict.fromkeys(normalize_word(str(word)) for word in words))
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
        return []

    async def _save_words(self, interaction: discord.Interaction, words: list[str]) -> None:
        serialized = json.dumps(words, ensure_ascii=False, separators=(",", ":"))
        await config_service.set_setting(interaction.guild.id, "automod_words", serialized,
                                         updated_by=interaction.user.id)
        invalidate_automod_cache(interaction.guild.id)

    async def _log_change(self, interaction: discord.Interaction, action: str, detail: str):
        event = LogEvent(
            LogType.AUTOMOD, f"⚙️ AutoMod {action}", staff=interaction.user,
            fields=[("Guild", f"{interaction.guild.name} ({interaction.guild.id})", True),
                    ("Configuration", detail[:900], False)],
        )
        from bot.services.logging_service import SendResult
        if await send_log(interaction.guild, event) is not SendResult.SENT:
            await send_log(interaction.guild, LogEvent(
                LogType.MODERATION, event.title, staff=interaction.user, fields=event.fields,
            ))

    @automod.command(name="enable", description="Enable the prohibited-word filter")
    @requires_level(StaffLevel.ADMIN)
    async def enable(self, interaction: discord.Interaction):
        await config_service.set_setting(interaction.guild.id, "automod_enabled", True,
                                         updated_by=interaction.user.id)
        await config_service.set_setting(interaction.guild.id, "automod_block_badwords", True,
                                         updated_by=interaction.user.id)
        invalidate_automod_cache(interaction.guild.id)
        await self._log_change(interaction, "Enabled", "Forbidden word filter enabled")
        await interaction.response.send_message("✅ AutoMod is enabled for this server.", ephemeral=True)

    @automod.command(name="disable", description="Disable the prohibited-word filter")
    @requires_level(StaffLevel.ADMIN)
    async def disable(self, interaction: discord.Interaction):
        await config_service.set_setting(interaction.guild.id, "automod_enabled", False,
                                         updated_by=interaction.user.id)
        invalidate_automod_cache(interaction.guild.id)
        await self._log_change(interaction, "Disabled", "Forbidden word filter disabled")
        await interaction.response.send_message("✅ AutoMod is disabled for this server.", ephemeral=True)

    @automod.command(name="word-add", description="Add one forbidden word or short phrase")
    @app_commands.describe(word="A word or phrase, up to 80 characters")
    @requires_level(StaffLevel.ADMIN)
    async def word_add(self, interaction: discord.Interaction, word: app_commands.Range[str, 1, 80]):
        try:
            word = normalize_word(str(word))
        except ValueError as exc:
            return await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
        words = await self._get_words(interaction.guild.id)
        if any(existing.casefold() == word.casefold() for existing in words):
            return await interaction.response.send_message("That word is already configured.", ephemeral=True)
        if len(words) >= _WORD_LIMIT:
            return await interaction.response.send_message(f"The list is limited to {_WORD_LIMIT} entries.", ephemeral=True)
        words.append(word)
        await self._save_words(interaction, words)
        await self._log_change(interaction, "Word Added", word)
        await interaction.response.send_message(f"✅ Added `{word}` to this server's AutoMod filter.", ephemeral=True)

    @automod.command(name="word-remove", description="Remove one forbidden word or phrase")
    @requires_level(StaffLevel.ADMIN)
    async def word_remove(self, interaction: discord.Interaction, word: app_commands.Range[str, 1, 80]):
        word = str(word).strip()
        words = await self._get_words(interaction.guild.id)
        updated = [existing for existing in words if existing.casefold() != word.casefold()]
        if len(updated) == len(words):
            return await interaction.response.send_message("That word is not in this server's AutoMod list.", ephemeral=True)
        await self._save_words(interaction, updated)
        await self._log_change(interaction, "Word Removed", word)
        await interaction.response.send_message(f"✅ Removed `{word}` from this server's AutoMod filter.", ephemeral=True)

    @automod.command(name="word-list", description="Show this server's forbidden-word list")
    @requires_level(StaffLevel.ADMIN)
    async def word_list(self, interaction: discord.Interaction):
        words = await self._get_words(interaction.guild.id)
        if not words:
            return await interaction.response.send_message("No forbidden words are configured.", ephemeral=True)
        embed = discord.Embed(title="AutoMod Forbidden Words", description="\n".join(
            f"• `{word}`" for word in words
        )[:4000], color=discord.Color.red())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @automod.command(name="word-clear", description="Remove all forbidden words for this server")
    @requires_level(StaffLevel.ADMIN)
    async def word_clear(self, interaction: discord.Interaction):
        words = await self._get_words(interaction.guild.id)
        if not words:
            return await interaction.response.send_message("The AutoMod word list is already empty.", ephemeral=True)
        await self._save_words(interaction, [])
        await self._log_change(interaction, "Word List Cleared", f"Removed {len(words)} entries")
        await interaction.response.send_message(f"✅ Removed all {len(words)} forbidden words.", ephemeral=True)

    @automod.command(name="set-xp-penalty", description="Set the normal Member XP penalty for each warning")
    @app_commands.describe(amount="Member XP deducted per warning (0–100000)")
    @requires_level(StaffLevel.ADMIN)
    async def set_xp_penalty(self, interaction: discord.Interaction,
                             amount: app_commands.Range[int, 0, 100000]):
        await config_service.set_setting(interaction.guild.id, "automod_warn_xp_penalty", amount,
                                         updated_by=interaction.user.id)
        invalidate_automod_cache(interaction.guild.id)
        await self._log_change(interaction, "XP Penalty Changed", f"{amount} Member XP per warning")
        await interaction.response.send_message(f"✅ Each AutoMod warning will deduct up to {amount} Member XP.", ephemeral=True)

    @automod.command(name="set-log-channel", description="Set the channel for AutoMod logs")
    @requires_level(StaffLevel.ADMIN)
    async def set_log_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await config_service.set_setting(interaction.guild.id, "automod_log_channel", channel.id,
                                         updated_by=interaction.user.id)
        invalidate_automod_cache(interaction.guild.id)
        await interaction.response.send_message(f"✅ AutoMod logs will be sent to {channel.mention}.", ephemeral=True)
        await self._log_change(interaction, "Log Channel Changed", channel.mention)

    @automod.command(name="status", description="Show this server's AutoMod settings")
    @requires_level(StaffLevel.ADMIN)
    async def status(self, interaction: discord.Interaction):
        config = await get_automod_config(interaction.guild.id)
        channel = interaction.guild.get_channel(config.log_channel_id) if config.log_channel_id else None
        embed = discord.Embed(title="AutoMod Status", color=discord.Color.red())
        embed.add_field(name="Enabled", value="Yes" if config.enabled else "No", inline=True)
        embed.add_field(name="Forbidden-word filter", value="Yes" if config.badwords_enabled else "No", inline=True)
        embed.add_field(name="Words", value=str(len(config.words)), inline=True)
        embed.add_field(name="Member XP penalty", value=str(config.xp_penalty), inline=True)
        embed.add_field(name="Log channel", value=channel.mention if isinstance(channel, discord.TextChannel) else "Not configured", inline=True)
        embed.add_field(name="Ignore Staff", value="Yes" if config.ignore_staff else "No", inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoModCog(bot))
