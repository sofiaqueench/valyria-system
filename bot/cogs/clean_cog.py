"""Guild-configured message cleanup confined to the invoking channel."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from bot.core.logger import logger
from bot.core.permissions import StaffLevel, requires_level
from bot.services import config_service
from bot.services.logging_service import LogEvent, LogType, send_log

_HARD_MAX_MESSAGES = 100
_CLEAN_COOLDOWN_SECONDS = 5
_COOLDOWN_MAX_KEYS = 10_000


class CleanCog(commands.Cog):
    clean = app_commands.Group(name="clean", description="Clean recent messages in this channel", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cooldowns: dict[tuple[int, int, int], float] = {}
        self._cooldown_lock = asyncio.Lock()

    async def _roles(self, guild_id: int) -> list[int]:
        raw = await config_service.get_setting(guild_id, "clean_role_ids")
        try:
            values = json.loads(str(raw or "[]"))
            if not isinstance(values, list):
                raise ValueError("expected a list")
            role_ids = list(dict.fromkeys(int(value) for value in values if int(value) > 0))
            return role_ids
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.warning("Invalid Clean role configuration in guild %s; denying use until corrected.", guild_id)
            return []

    async def _save_roles(self, guild_id: int, role_ids: list[int], actor_id: int) -> None:
        await config_service.set_setting(guild_id, "clean_role_ids", json.dumps(role_ids), actor_id)

    async def _authorized(self, member: discord.Member) -> bool:
        configured = set(await self._roles(member.guild.id))
        return bool(configured.intersection(role.id for role in member.roles))

    async def _claim_cooldown(self, guild_id: int, user_id: int, channel_id: int) -> float:
        """Return seconds remaining, or zero after reserving this guild/user/channel scope."""
        now = time.monotonic()
        key = (guild_id, user_id, channel_id)
        async with self._cooldown_lock:
            for old_key, stamp in list(self._cooldowns.items()):
                if now - stamp >= _CLEAN_COOLDOWN_SECONDS:
                    self._cooldowns.pop(old_key, None)
            previous = self._cooldowns.get(key)
            if previous is not None and now - previous < _CLEAN_COOLDOWN_SECONDS:
                return _CLEAN_COOLDOWN_SECONDS - (now - previous)
            if len(self._cooldowns) >= _COOLDOWN_MAX_KEYS:
                oldest = min(self._cooldowns, key=self._cooldowns.get)
                self._cooldowns.pop(oldest, None)
            self._cooldowns[key] = now
            return 0

    async def _log_action(
        self, interaction: discord.Interaction, channel: discord.abc.GuildChannel,
        requested: int, deleted: int, *, failed: bool = False, detail: str | None = None,
    ) -> None:
        fields = [
            ("Channel", getattr(channel, "mention", f"#{channel.name}"), True),
            ("Requested", str(requested), True),
            ("Actually deleted", str(deleted), True),
            ("Result", "Failed / stopped" if failed else "Completed", True),
            ("Timestamp", discord.utils.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"), True),
        ]
        await send_log(interaction.guild, LogEvent(
            LogType.MODERATION,
            "⚠️ Clean Messages Failed" if failed else "🧹 Clean Messages",
            staff=interaction.user,
            reason=detail,
            fields=fields,
            ids={"Guild ID": interaction.guild_id, "User ID": interaction.user.id,
                 "Channel ID": channel.id},
        ))

    async def _delete_recent(self, channel: discord.TextChannel | discord.Thread, amount: int) -> int:
        """Delete at most amount from this channel, using singles for messages near/over 14 days."""
        messages = [message async for message in channel.history(limit=amount, oldest_first=False)]
        eligible = [message for message in messages if message.type.is_deletable()]
        cutoff = discord.utils.utcnow() - timedelta(days=14, minutes=1)
        recent = [message for message in eligible if message.created_at > cutoff]
        old = [message for message in eligible if message.created_at <= cutoff]
        deleted = 0

        # A single-item bulk request is invalid; use the message delete endpoint instead.
        if len(recent) == 1:
            await recent[0].delete(reason=f"/clean by {self.bot.user} (requested {amount})")
            deleted += 1
        elif len(recent) > 1:
            # amount is capped at 100, so this is one safe Discord bulk-delete request.
            await channel.delete_messages(recent, reason=f"/clean by {self.bot.user} (requested {amount})")
            deleted += len(recent)

        # Bulk deletion cannot include messages older than 14 days; use individual deletes.
        for message in old:
            try:
                await message.delete(reason=f"/clean by {self.bot.user} (requested {amount})")
                deleted += 1
            except discord.NotFound:
                # It was concurrently deleted; no successful deletion attributable to this action.
                continue
        return deleted

    @clean.command(name="amount", description="Delete recent messages from the current channel only")
    @app_commands.describe(amount="Number of recent messages to clean (up to the configured maximum)")
    async def amount(self, interaction: discord.Interaction, amount: int):
        guild = interaction.guild
        member = interaction.user
        channel = interaction.channel
        if guild is None or not isinstance(member, discord.Member):
            return await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)

        # Authorization is checked before channel reads, cooldown reservation, or deletion.
        if not await self._authorized(member):
            return await interaction.response.send_message("You do not have a role authorized to use Clean Messages.", ephemeral=True)
        if not await config_service.get_setting(guild.id, "clean_enabled"):
            return await interaction.response.send_message("Clean Messages is disabled in this server.", ephemeral=True)
        maximum = int(await config_service.get_setting(guild.id, "clean_max_messages") or _HARD_MAX_MESSAGES)
        if amount <= 0:
            return await interaction.response.send_message("Amount must be at least 1.", ephemeral=True)
        if amount > _HARD_MAX_MESSAGES:
            return await interaction.response.send_message(
                f"The absolute maximum clean amount is {_HARD_MAX_MESSAGES} messages.", ephemeral=True)
        if amount > maximum:
            return await interaction.response.send_message(f"Maximum clean amount is {maximum} messages.", ephemeral=True)
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return await interaction.response.send_message("Use /clean in a text channel or thread.", ephemeral=True)

        app_permissions = interaction.app_permissions
        missing = [name for name in ("view_channel", "read_message_history", "manage_messages")
                   if not getattr(app_permissions, name, False)]
        if missing:
            labels = {"view_channel": "View Channel", "read_message_history": "Read Message History",
                      "manage_messages": "Manage Messages"}
            return await interaction.response.send_message(
                "I need these permissions in this channel: " + ", ".join(labels[name] for name in missing) + ".",
                ephemeral=True)

        remaining = await self._claim_cooldown(guild.id, member.id, channel.id)
        if remaining:
            return await interaction.response.send_message(
                f"Please wait {remaining:.1f} seconds before cleaning this channel again.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        deleted = 0
        try:
            deleted = await self._delete_recent(channel, amount)
        except discord.Forbidden as exc:
            detail = "Missing channel permissions while reading or deleting messages."
            logger.warning("Clean failed in guild/channel %s/%s for user %s: %s", guild.id, channel.id, member.id, exc)
            await self._log_action(interaction, channel, amount, deleted, failed=True, detail=detail)
            return await interaction.followup.send(
                "I could not clean messages here. Confirm my View Channel, Read Message History, and Manage Messages permissions.",
                ephemeral=True)
        except discord.NotFound as exc:
            detail = "The channel or message was no longer available during cleanup."
            logger.info("Clean target disappeared in guild/channel %s/%s: %s", guild.id, channel.id, exc)
            await self._log_action(interaction, channel, amount, deleted, failed=True, detail=detail)
            return await interaction.followup.send(
                f"The channel or message disappeared during cleanup. Confirmed deleted: {deleted}.", ephemeral=True)
        except discord.HTTPException as exc:
            detail = f"Discord API stopped cleanup after {deleted} confirmed deletions ({exc.status})."
            logger.warning("Clean API failure in guild/channel %s/%s after %s deletes: %s", guild.id, channel.id, deleted, exc)
            await self._log_action(interaction, channel, amount, deleted, failed=True, detail=detail)
            return await interaction.followup.send(
                f"Discord stopped the cleanup. Confirmed deleted: {deleted}; no further deletion attempts were made.",
                ephemeral=True)
        except Exception as exc:
            logger.exception("Unexpected Clean failure in guild/channel %s/%s", guild.id, channel.id)
            await self._log_action(interaction, channel, amount, deleted, failed=True,
                                   detail=f"Unexpected cleanup error: {type(exc).__name__}.")
            return await interaction.followup.send(
                f"Cleanup failed. Confirmed deleted: {deleted}; no further deletion attempts were made.", ephemeral=True)

        await self._log_action(interaction, channel, amount, deleted)
        await interaction.followup.send(f"🧹 Successfully cleaned {deleted} messages in {channel.mention}.", ephemeral=True)

    @clean.command(name="add-role", description="Authorize a role to use /clean")
    @requires_level(StaffLevel.ADMIN)
    async def add_role(self, interaction: discord.Interaction, role: discord.Role):
        if role.is_default() or role.managed:
            return await interaction.response.send_message("Choose a regular server role; @everyone and managed roles are not allowed.", ephemeral=True)
        role_ids = await self._roles(interaction.guild_id)
        if role.id in role_ids:
            return await interaction.response.send_message(f"{role.mention} is already authorized.", ephemeral=True)
        role_ids.append(role.id)
        await self._save_roles(interaction.guild_id, role_ids, interaction.user.id)
        await interaction.response.send_message(f"{role.mention} can now use /clean.", ephemeral=True)

    @clean.command(name="remove-role", description="Remove a role's permission to use /clean")
    @requires_level(StaffLevel.ADMIN)
    async def remove_role(self, interaction: discord.Interaction, role: discord.Role):
        role_ids = await self._roles(interaction.guild_id)
        if role.id not in role_ids:
            return await interaction.response.send_message(f"{role.mention} is not an authorized Clean role.", ephemeral=True)
        role_ids.remove(role.id)
        await self._save_roles(interaction.guild_id, role_ids, interaction.user.id)
        await interaction.response.send_message(f"{role.mention} can no longer use /clean.", ephemeral=True)

    @clean.command(name="roles", description="List roles authorized to use /clean")
    @requires_level(StaffLevel.ADMIN)
    async def roles(self, interaction: discord.Interaction):
        role_ids = await self._roles(interaction.guild_id)
        lines = [f"• {interaction.guild.get_role(role_id).mention}" if interaction.guild.get_role(role_id)
                 else "• (a configured role was deleted)" for role_id in role_ids]
        text = "\n".join(lines) if lines else "No Clean roles are configured."
        await interaction.response.send_message("🧹 **Clean Messages Roles**\n" + text, ephemeral=True)

    @clean.command(name="set-max", description="Set the maximum number of messages removable per command")
    @requires_level(StaffLevel.ADMIN)
    async def set_max(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, _HARD_MAX_MESSAGES]):
        await config_service.set_setting(interaction.guild_id, "clean_max_messages", amount, interaction.user.id)
        await interaction.response.send_message(f"Maximum Clean amount set to {amount} messages.", ephemeral=True)

    @clean.command(name="enable", description="Enable Clean Messages in this server")
    @requires_level(StaffLevel.ADMIN)
    async def enable(self, interaction: discord.Interaction):
        await config_service.set_setting(interaction.guild_id, "clean_enabled", True, interaction.user.id)
        await interaction.response.send_message("Clean Messages is enabled.", ephemeral=True)

    @clean.command(name="disable", description="Disable Clean Messages in this server")
    @requires_level(StaffLevel.ADMIN)
    async def disable(self, interaction: discord.Interaction):
        await config_service.set_setting(interaction.guild_id, "clean_enabled", False, interaction.user.id)
        await interaction.response.send_message("Clean Messages is disabled.", ephemeral=True)

    @clean.command(name="status", description="Show Clean Messages configuration")
    @requires_level(StaffLevel.ADMIN)
    async def status(self, interaction: discord.Interaction):
        enabled = await config_service.get_setting(interaction.guild_id, "clean_enabled")
        maximum = await config_service.get_setting(interaction.guild_id, "clean_max_messages")
        role_ids = await self._roles(interaction.guild_id)
        lines = [f"• {interaction.guild.get_role(role_id).mention}" if interaction.guild.get_role(role_id)
                 else "• (a configured role was deleted)" for role_id in role_ids]
        roles = "\n".join(lines) if lines else "None configured"
        await interaction.response.send_message(
            f"🧹 **Clean Messages Status**\nEnabled: {'Yes' if enabled else 'No'}\n"
            f"Maximum Messages: {maximum}\nCooldown: {_CLEAN_COOLDOWN_SECONDS} seconds\nAuthorized Roles:\n{roles}",
            ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(CleanCog(bot))
