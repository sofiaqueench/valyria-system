"""Anti-Raid and passive security monitoring, using the central config/log systems."""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from datetime import timezone

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from bot.core.logger import logger
from bot.core.permissions import StaffLevel, requires_level
from bot.database.engine import async_session
from bot.database.models.security import RaidState
from bot.services import config_service
from bot.services.logging_service import LogEvent, LogType, send_log

_BURST_COUNT = 5
_BURST_WINDOW = 30
_MASS_ALERT_COOLDOWN = 60
_AUDIT_ACTIONS = {
    "member_kick": discord.AuditLogAction.kick,
}


class SecurityCog(commands.Cog):
    antiraid = app_commands.Group(name="antiraid", description="Configure join-raid detection", guild_only=True)
    antispam = app_commands.Group(name="antispam", description="Configure message spam detection", guild_only=True)
    security = app_commands.Group(name="security", description="Configure security monitoring", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._joins: dict[int, deque[tuple[float, discord.Member]]] = defaultdict(lambda: deque(maxlen=100))
        self._join_locks: dict[int, asyncio.Lock] = {}
        self._recent_join_events: dict[tuple[int, int], float] = {}
        self._mass_events: dict[tuple[int, str], deque[float]] = defaultdict(lambda: deque(maxlen=30))
        self._mass_alert_at: dict[tuple[int, str], float] = {}
        self._audit_cache: dict[tuple[int, int, discord.AuditLogAction], tuple[float, discord.abc.User | None]] = {}

    async def _log(self, guild: discord.Guild, title: str, description: str, *, actor=None, target=None, fields=None):
        data = list(fields or [])
        data.append(("Guild", f"{guild.name} ({guild.id})", True))
        data.append(("Timestamp", discord.utils.utcnow().strftime("%Y-%m-%d %H:%M UTC"), True))
        await send_log(guild, LogEvent(LogType.SECURITY, title, description=description, staff=actor,
            target=target, fields=data, ids={"Guild ID": guild.id, **({"Target ID": target.id} if target else {})}))

    async def _actor(self, guild: discord.Guild, target_id: int, action: discord.AuditLogAction):
        key = (guild.id, target_id, action)
        cached = self._audit_cache.get(key)
        now = time.monotonic()
        if cached and now - cached[0] <= 15:
            return cached[1]
        me = guild.me
        if me is None or not me.guild_permissions.view_audit_log:
            return None
        try:
            async for entry in guild.audit_logs(limit=5, action=action):
                target = getattr(entry.target, "id", None)
                age = abs((discord.utils.utcnow() - entry.created_at).total_seconds())
                if target == target_id and age <= 12:
                    self._audit_cache[key] = (now, entry.user)
                    return entry.user
        except (discord.Forbidden, discord.HTTPException):
            logger.warning("Could not inspect audit log for %s in guild %s", action, guild.id)
        return None

    async def _mass_action(self, guild: discord.Guild, kind: str, target, actor=None):
        now = time.monotonic()
        key = (guild.id, kind)
        bucket = self._mass_events[key]
        bucket.append(now)
        while bucket and now - bucket[0] > _BURST_WINDOW:
            bucket.popleft()
        last = self._mass_alert_at.get(key, 0)
        if len(bucket) >= _BURST_COUNT and now - last >= _MASS_ALERT_COOLDOWN:
            self._mass_alert_at[key] = now
            await self._log(guild, "🚨 Possible mass security action",
                f"Detected {len(bucket)} {kind.replace('_', ' ')} events within {_BURST_WINDOW} seconds. No automatic punishment was applied.",
                actor=actor, target=target,
                fields=[("Action type", kind, True), ("Events", str(len(bucket)), True), ("Window", f"{_BURST_WINDOW}s", True)])

    async def _audit_event(self, guild_id: int, action: discord.AuditLogAction, entry: discord.AuditLogEntry):
        target_id = getattr(entry.target, "id", None)
        if target_id is None:
            return
        self._audit_cache[(guild_id, target_id, action)] = (time.monotonic(), entry.user)
        if action == discord.AuditLogAction.kick:
            guild = self.bot.get_guild(guild_id)
            if guild:
                await self._mass_action(guild, "member_kick", entry.target, entry.user)
        # Keep the audit cache bounded and short lived.
        now = time.monotonic()
        for key, value in list(self._audit_cache.items()):
            if now - value[0] > 30:
                self._audit_cache.pop(key, None)

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        guild = entry.guild
        if guild is None:
            return
        action = entry.action
        if action in _AUDIT_ACTIONS.values():
            await self._audit_event(guild.id, action, entry)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        await self._mass_action(role.guild, "role_delete", role)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        await self._mass_action(channel.guild, "channel_delete", channel)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        actor = await self._actor(guild, user.id, discord.AuditLogAction.ban)
        await self._mass_action(guild, "member_ban", user, actor)

    async def _raid_state(self, guild_id: int):
        async with async_session() as session:
            return await session.get(RaidState, guild_id)

    async def _set_raid_state(self, guild: discord.Guild, active: bool, actor_id: int | None, reason: str, join_count=0):
        await config_service.get_or_create_guild(guild.id)
        async with async_session() as session:
            async with session.begin():
                row = await session.scalar(select(RaidState).where(RaidState.guild_id == guild.id).with_for_update())
                if row is None:
                    if not active:
                        return False
                    row = RaidState(guild_id=guild.id, active=True, activated_by=actor_id,
                                    reason=reason[:300], join_count=join_count, activated_at=discord.utils.utcnow())
                    session.add(row)
                elif row.active == active:
                    return False
                else:
                    row.active = active
                    row.activated_by = actor_id if active else None
                    row.reason = reason[:300] if active else None
                    row.join_count = join_count if active else 0
                    row.activated_at = discord.utils.utcnow() if active else None
        await self._log(guild, "🚨 Raid Mode Activated" if active else "✅ Raid Mode Deactivated",
            reason, actor=guild.get_member(actor_id) if actor_id else None,
            fields=[("Raid Mode", "Active" if active else "Inactive", True), ("Joins", str(join_count), True)])
        return True

    async def _apply_raid_restriction(self, member: discord.Member):
        role_id = await config_service.get_setting(member.guild.id, "raid_restricted_role")
        role = member.guild.get_role(role_id) if role_id else None
        bot_member = member.guild.me
        if role is None:
            return
        if role.is_default() or role.managed or bot_member is None or bot_member.top_role <= role:
            logger.warning("Raid restriction role %s is invalid or above bot hierarchy in guild %s", role_id, member.guild.id)
            return
        if role not in member.roles:
            try:
                await member.add_roles(role, reason="Anti-Raid strict mode: new join during active raid")
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("Could not restrict new raid-mode member %s in guild %s", member.id, member.guild.id)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        guild = member.guild
        now = time.monotonic()
        event_key = (guild.id, member.id)
        if now - self._recent_join_events.get(event_key, -100) < 2:
            return
        self._recent_join_events[event_key] = now
        for key, stamp in list(self._recent_join_events.items()):
            if now - stamp > 60:
                self._recent_join_events.pop(key, None)

        try:
            active = await self._raid_state(guild.id)
            mode = str(await config_service.get_setting(guild.id, "antiraid_mode") or "normal")
            include_bots = bool(await config_service.get_setting(guild.id, "antiraid_include_bots"))
            if active and active.active and mode == "strict" and (not member.bot or include_bots):
                await self._apply_raid_restriction(member)
            if member.bot and not include_bots:
                return
            if not await config_service.get_setting(guild.id, "antiraid_enabled"):
                return
            threshold = int(await config_service.get_setting(guild.id, "antiraid_join_threshold") or 10)
            window = int(await config_service.get_setting(guild.id, "antiraid_join_seconds") or 30)
            lock = self._join_locks.setdefault(guild.id, asyncio.Lock())
            async with lock:
                bucket = self._joins[guild.id]
                bucket.append((now, member))
                while bucket and now - bucket[0][0] > window:
                    bucket.popleft()
                current_state = await self._raid_state(guild.id)
                if len(bucket) >= threshold and not (current_state and current_state.active):
                    activated = await self._set_raid_state(guild, True, None,
                        f"Detected {len(bucket)} human joins within {window} seconds.", len(bucket))
                    if activated:
                        if mode == "strict":
                            for _, joined_member in list(bucket):
                                if not joined_member.bot or include_bots:
                                    await self._apply_raid_restriction(joined_member)
                        await self._log(guild, "🚨 Possible raid detected",
                            f"Detected {len(bucket)} member joins within {window} seconds. Raid Mode is active.",
                            fields=[("Joins", str(len(bucket)), True), ("Window", f"{window}s", True),
                                    ("Raid Mode", mode.title(), True), ("Automatic", "Yes", True)])
        except Exception:
            logger.exception("Anti-Raid join processing failed for member %s in guild %s", member.id, guild.id)

    @antiraid.command(name="enable", description="Enable join-burst detection")
    @requires_level(StaffLevel.ADMIN)
    async def antiraid_enable(self, interaction: discord.Interaction):
        await config_service.set_setting(interaction.guild_id, "antiraid_enabled", True, interaction.user.id)
        await self._log(interaction.guild, "Anti-Raid Enabled", "Join-burst detection was enabled.", actor=interaction.user)
        await interaction.response.send_message("Anti-Raid detection enabled.", ephemeral=True)

    @antiraid.command(name="disable", description="Disable join-burst detection")
    @requires_level(StaffLevel.ADMIN)
    async def antiraid_disable(self, interaction: discord.Interaction):
        await config_service.set_setting(interaction.guild_id, "antiraid_enabled", False, interaction.user.id)
        await self._log(interaction.guild, "Anti-Raid Detection Disabled", "Automatic join detection is disabled; current Raid Mode state is unchanged.", actor=interaction.user)
        await interaction.response.send_message("Anti-Raid detection disabled. Use `/antiraid deactivate` to clear an active Raid Mode.", ephemeral=True)

    @antiraid.command(name="set-threshold", description="Set the number of joins that trigger detection")
    @requires_level(StaffLevel.ADMIN)
    async def antiraid_threshold(self, interaction: discord.Interaction, amount: app_commands.Range[int, 3, 100]):
        await config_service.set_setting(interaction.guild_id, "antiraid_join_threshold", amount, interaction.user.id)
        await interaction.response.send_message(f"Raid threshold set to {amount} joins.", ephemeral=True)

    @antiraid.command(name="set-window", description="Set the join detection window")
    @requires_level(StaffLevel.ADMIN)
    async def antiraid_window(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 3, 300]):
        await config_service.set_setting(interaction.guild_id, "antiraid_join_seconds", seconds, interaction.user.id)
        await interaction.response.send_message(f"Raid window set to {seconds} seconds.", ephemeral=True)

    @antiraid.command(name="set-mode", description="Set normal alert-only or strict restricted-join mode")
    @app_commands.choices(mode=[app_commands.Choice(name="Normal", value="normal"), app_commands.Choice(name="Strict", value="strict")])
    @requires_level(StaffLevel.ADMIN)
    async def antiraid_mode(self, interaction: discord.Interaction, mode: app_commands.Choice[str]):
        await config_service.set_setting(interaction.guild_id, "antiraid_mode", mode.value, interaction.user.id)
        await interaction.response.send_message(f"Raid mode set to {mode.name}.", ephemeral=True)

    @antiraid.command(name="set-restricted-role", description="Set the role applied to new joins during Strict Raid Mode")
    @requires_level(StaffLevel.ADMIN)
    async def antiraid_role(self, interaction: discord.Interaction, role: discord.Role):
        if role.is_default() or role.managed or interaction.guild.me.top_role <= role:
            return await interaction.response.send_message("The restriction role must be assignable by the bot.", ephemeral=True)
        await config_service.set_setting(interaction.guild_id, "raid_restricted_role", role.id, interaction.user.id)
        await interaction.response.send_message(f"Strict Raid Mode will add {role.mention} to new joins.", ephemeral=True)

    @antiraid.command(name="set-log-channel", description="Set the centralized security log channel")
    @requires_level(StaffLevel.ADMIN)
    async def antiraid_log_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await config_service.set_setting(interaction.guild_id, "log_security", channel.id, interaction.user.id)
        await interaction.response.send_message(f"Security alerts will be sent to {channel.mention}.", ephemeral=True)

    @antiraid.command(name="activate", description="Manually activate Raid Mode")
    @requires_level(StaffLevel.ADMIN)
    async def antiraid_activate(self, interaction: discord.Interaction, reason: app_commands.Range[str, 1, 300] = "Manually activated"):
        changed = await self._set_raid_state(interaction.guild, True, interaction.user.id, reason)
        await interaction.response.send_message("Raid Mode activated." if changed else "Raid Mode is already active.", ephemeral=True)

    @antiraid.command(name="deactivate", description="Manually deactivate Raid Mode")
    @requires_level(StaffLevel.ADMIN)
    async def antiraid_deactivate(self, interaction: discord.Interaction, reason: app_commands.Range[str, 1, 300] = "Manually deactivated"):
        changed = await self._set_raid_state(interaction.guild, False, interaction.user.id, reason)
        await interaction.response.send_message("Raid Mode deactivated." if changed else "Raid Mode is already inactive.", ephemeral=True)

    @antiraid.command(name="status", description="Show Anti-Raid settings and Raid Mode state")
    @requires_level(StaffLevel.ADMIN)
    async def antiraid_status(self, interaction: discord.Interaction):
        state = await self._raid_state(interaction.guild_id)
        enabled = await config_service.get_setting(interaction.guild_id, "antiraid_enabled")
        threshold = await config_service.get_setting(interaction.guild_id, "antiraid_join_threshold")
        window = await config_service.get_setting(interaction.guild_id, "antiraid_join_seconds")
        mode = await config_service.get_setting(interaction.guild_id, "antiraid_mode")
        await interaction.response.send_message(f"Detection: {'enabled' if enabled else 'disabled'} · {threshold} joins / {window}s\nMode: {mode}\nRaid Mode: {'ACTIVE' if state and state.active else 'inactive'}" + (f"\nReason: {state.reason}" if state and state.active else ""), ephemeral=True)

    @security.command(name="set-log-channel", description="Configure centralized security monitoring logs")
    @requires_level(StaffLevel.ADMIN)
    async def security_log_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await config_service.set_setting(interaction.guild_id, "log_security", channel.id, interaction.user.id)
        await interaction.response.send_message(f"Security monitoring logs will be sent to {channel.mention}.", ephemeral=True)

    @antispam.command(name="enable", description="Enable message spam detection")
    @requires_level(StaffLevel.ADMIN)
    async def antispam_enable(self, interaction: discord.Interaction):
        await config_service.set_setting(interaction.guild_id, "antispam_enabled", True, interaction.user.id)
        from bot.services.antispam_service import clear_antispam_cache
        clear_antispam_cache(interaction.guild_id)
        await interaction.response.send_message("Anti-Spam is enabled.", ephemeral=True)

    @antispam.command(name="disable", description="Disable message spam detection")
    @requires_level(StaffLevel.ADMIN)
    async def antispam_disable(self, interaction: discord.Interaction):
        await config_service.set_setting(interaction.guild_id, "antispam_enabled", False, interaction.user.id)
        from bot.services.antispam_service import clear_antispam_cache
        clear_antispam_cache(interaction.guild_id)
        await interaction.response.send_message("Anti-Spam is disabled.", ephemeral=True)

    @antispam.command(name="set-message-limit", description="Set messages that trigger burst detection")
    @requires_level(StaffLevel.ADMIN)
    async def antispam_message_limit(self, interaction: discord.Interaction, amount: app_commands.Range[int, 2, 30]):
        await config_service.set_setting(interaction.guild_id, "automod_spam_messages", amount, interaction.user.id)
        from bot.services.antispam_service import clear_antispam_cache
        clear_antispam_cache(interaction.guild_id)
        await interaction.response.send_message(f"Burst threshold set to {amount} messages.", ephemeral=True)

    @antispam.command(name="set-window", description="Set the message burst window in seconds")
    @requires_level(StaffLevel.ADMIN)
    async def antispam_window(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 1, 60]):
        await config_service.set_setting(interaction.guild_id, "automod_spam_seconds", seconds, interaction.user.id)
        from bot.services.antispam_service import clear_antispam_cache
        clear_antispam_cache(interaction.guild_id)
        await interaction.response.send_message(f"Burst window set to {seconds} seconds.", ephemeral=True)

    @antispam.command(name="set-max-mentions", description="Set maximum mentions per message")
    @requires_level(StaffLevel.ADMIN)
    async def antispam_mentions(self, interaction: discord.Interaction, amount: app_commands.Range[int, 2, 50]):
        await config_service.set_setting(interaction.guild_id, "automod_mention_limit", amount, interaction.user.id)
        from bot.services.antispam_service import clear_antispam_cache
        clear_antispam_cache(interaction.guild_id)
        await interaction.response.send_message(f"Mention limit set to {amount}.", ephemeral=True)

    @antispam.command(name="set-duplicate-limit", description="Set repeated identical messages before warning")
    @requires_level(StaffLevel.ADMIN)
    async def antispam_duplicate_limit(self, interaction: discord.Interaction, amount: app_commands.Range[int, 2, 10]):
        await config_service.set_setting(interaction.guild_id, "antispam_duplicate_limit", amount, interaction.user.id)
        from bot.services.antispam_service import clear_antispam_cache
        clear_antispam_cache(interaction.guild_id)
        await interaction.response.send_message(f"Duplicate message threshold set to {amount}.", ephemeral=True)

    @antispam.command(name="set-duplicate-window", description="Set the duplicate message detection window")
    @requires_level(StaffLevel.ADMIN)
    async def antispam_duplicate_window(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 3, 120]):
        await config_service.set_setting(interaction.guild_id, "antispam_duplicate_seconds", seconds, interaction.user.id)
        from bot.services.antispam_service import clear_antispam_cache
        clear_antispam_cache(interaction.guild_id)
        await interaction.response.send_message(f"Duplicate detection window set to {seconds} seconds.", ephemeral=True)

    @antispam.command(name="set-emoji-limit", description="Set maximum emoji count per message")
    @requires_level(StaffLevel.ADMIN)
    async def antispam_emoji_limit(self, interaction: discord.Interaction, amount: app_commands.Range[int, 4, 80]):
        await config_service.set_setting(interaction.guild_id, "antispam_emoji_limit", amount, interaction.user.id)
        from bot.services.antispam_service import clear_antispam_cache
        clear_antispam_cache(interaction.guild_id)
        await interaction.response.send_message(f"Emoji threshold set to {amount}.", ephemeral=True)

    @antispam.command(name="set-repeat-limit", description="Set repeated characters that trigger detection")
    @requires_level(StaffLevel.ADMIN)
    async def antispam_repeat_limit(self, interaction: discord.Interaction, amount: app_commands.Range[int, 10, 100]):
        await config_service.set_setting(interaction.guild_id, "antispam_repeat_chars", amount, interaction.user.id)
        from bot.services.antispam_service import clear_antispam_cache
        clear_antispam_cache(interaction.guild_id)
        await interaction.response.send_message(f"Repeated character threshold set to {amount}.", ephemeral=True)

    @antispam.command(name="links", description="Configure link spam detection")
    @app_commands.describe(enabled="Whether excessive links should trigger Anti-Spam", amount="Links allowed per message")
    @requires_level(StaffLevel.ADMIN)
    async def antispam_links(self, interaction: discord.Interaction, enabled: bool,
                             amount: app_commands.Range[int, 2, 30] = 5):
        await config_service.set_setting(interaction.guild_id, "antispam_links_enabled", enabled, interaction.user.id)
        await config_service.set_setting(interaction.guild_id, "antispam_link_limit", amount, interaction.user.id)
        from bot.services.antispam_service import clear_antispam_cache
        clear_antispam_cache(interaction.guild_id)
        await interaction.response.send_message(
            f"Link spam detection {'enabled' if enabled else 'disabled'}; limit: {amount} per message.", ephemeral=True)

    @antispam.command(name="set-xp-penalty", description="Set Member XP removed per spam warning")
    @requires_level(StaffLevel.ADMIN)
    async def antispam_penalty(self, interaction: discord.Interaction, amount: app_commands.Range[int, 0, 100000]):
        await config_service.set_setting(interaction.guild_id, "antispam_xp_penalty", amount, interaction.user.id)
        from bot.services.antispam_service import clear_antispam_cache
        clear_antispam_cache(interaction.guild_id)
        await interaction.response.send_message(f"Anti-Spam Member XP penalty set to {amount}.", ephemeral=True)

    @antispam.command(name="set-log-channel", description="Set the AutoMod/Anti-Spam log channel")
    @requires_level(StaffLevel.ADMIN)
    async def antispam_log_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await config_service.set_setting(interaction.guild_id, "automod_log_channel", channel.id, interaction.user.id)
        from bot.services.antispam_service import clear_antispam_cache
        clear_antispam_cache(interaction.guild_id)
        await interaction.response.send_message(f"AutoMod and Anti-Spam logs will use {channel.mention}.", ephemeral=True)

    @antispam.command(name="status", description="Show Anti-Spam settings")
    @requires_level(StaffLevel.ADMIN)
    async def antispam_status(self, interaction: discord.Interaction):
        values = [await config_service.get_setting(interaction.guild_id, key) for key in (
            "antispam_enabled", "automod_spam_messages", "automod_spam_seconds", "automod_mention_limit",
            "antispam_duplicate_limit", "antispam_duplicate_seconds", "antispam_emoji_limit",
            "antispam_repeat_chars", "antispam_xp_penalty", "automod_log_channel")]
        await interaction.response.send_message(
            f"Enabled: {values[0]} · Burst: {values[1]} / {values[2]}s · Mentions: {values[3]}\n"
            f"Duplicate: {values[4]} / {values[5]}s · Emoji limit: {values[6]} · Repeated-char limit: {values[7]}\n"
            f"Member XP penalty: {values[8]} · Log channel: {values[9] or 'not configured'}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SecurityCog(bot))
