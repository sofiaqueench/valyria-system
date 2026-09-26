"""Keep the bot connected to the per-guild configured permanent voice channel."""
from __future__ import annotations

import asyncio

import discord
from discord.ext import commands, tasks

from bot.core.logger import logger
from bot.services import config_service
from bot.services.logging_service import LogEvent, LogType, send_log


class PermanentVoiceCog(commands.Cog):
    """One bounded, centralized reconnect loop for all configured guilds."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._retry_after: dict[int, float] = {}
        self._backoff: dict[int, int] = {}
        self._managed_guilds: set[int] = set()
        self._reported_invalid: dict[int, tuple[int | None, str]] = {}
        self._guild_locks: dict[int, asyncio.Lock] = {}

    async def cog_load(self) -> None:
        if not self.maintain_voice.is_running():
            self.maintain_voice.start()

    def cog_unload(self) -> None:
        self.maintain_voice.cancel()

    def request_reconnect(self, guild: discord.Guild) -> None:
        """Wake the next loop iteration promptly after an admin changes configuration."""
        self._retry_after[guild.id] = 0
        self._backoff[guild.id] = 0
        self._reported_invalid.pop(guild.id, None)

    @tasks.loop(seconds=20)
    async def maintain_voice(self) -> None:
        for guild in self.bot.guilds:
            try:
                await self._ensure_connected(guild)
            except Exception:
                logger.exception("Permanent Voice maintenance failed in guild %s.", guild.id)

    @maintain_voice.before_loop
    async def before_maintain_voice(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # Handles initial startup and reconnects; periodic maintenance covers later disconnects.
        for guild in self.bot.guilds:
            self.request_reconnect(guild)
            await self._ensure_connected(guild)

    async def _ensure_connected(self, guild: discord.Guild) -> None:
        lock = self._guild_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            await self._ensure_connected_locked(guild)

    async def _ensure_connected_locked(self, guild: discord.Guild) -> None:
        now = asyncio.get_running_loop().time()
        if now < self._retry_after.get(guild.id, 0):
            return

        channel_id = await config_service.get_setting(guild.id, "permanent_voice_channel")
        current = guild.voice_client
        if channel_id is None:
            # Only disconnect a connection this cog previously established.
            if guild.id in self._managed_guilds and current is not None:
                try:
                    await current.disconnect(force=True)
                finally:
                    self._managed_guilds.discard(guild.id)
            return

        channel = guild.get_channel(channel_id)
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            await self._invalid_configuration(guild, channel_id, "channel is missing or is not a voice channel")
            return

        me = guild.me
        if me is None:
            await self._invalid_configuration(guild, channel_id, "bot member is not available in the guild cache")
            return
        permissions = channel.permissions_for(me)
        if not permissions.view_channel or not permissions.connect:
            await self._invalid_configuration(guild, channel_id, "bot needs View Channel and Connect permissions")
            return

        self._reported_invalid.pop(guild.id, None)
        if current is not None and current.is_connected():
            if current.channel and current.channel.id == channel.id:
                self._retry_after.pop(guild.id, None)
                self._backoff.pop(guild.id, None)
                self._managed_guilds.add(guild.id)
                return
            try:
                await current.move_to(channel, timeout=20)
                self._connected(guild, channel, reconnected=True)
                return
            except Exception:
                logger.exception("Could not move bot voice connection in guild %s.", guild.id)
                await self._schedule_retry(guild)
                return

        if current is not None:
            try:
                await current.disconnect(force=True)
            except Exception:
                logger.debug("Discarding stale voice client in guild %s.", guild.id, exc_info=True)

        try:
            # Self-deafen keeps the bot connected without requiring Speak permission.
            await channel.connect(timeout=20, reconnect=False, self_deaf=True)
            self._connected(guild, channel, reconnected=guild.id in self._managed_guilds)
        except Exception as exc:
            logger.warning("Permanent Voice connection failed in guild %s: %s", guild.id, exc)
            await self._schedule_retry(guild)

    async def _invalid_configuration(
        self, guild: discord.Guild, channel_id: int, reason: str
    ) -> None:
        signature = (channel_id, reason)
        if self._reported_invalid.get(guild.id) != signature:
            self._reported_invalid[guild.id] = signature
            logger.warning("Invalid Permanent Voice configuration in guild %s: %s (channel %s).", guild.id, reason, channel_id)
            await send_log(guild, LogEvent(
                LogType.VOICE, "⚠️ Permanent Voice Configuration Problem",
                description=reason, ids={"Channel ID": channel_id},
            ))
        # A deleted/inaccessible channel won't improve through retries. Recheck periodically,
        # and immediately when an administrator changes/clears the setting.
        self._retry_after[guild.id] = asyncio.get_running_loop().time() + 900

    async def _schedule_retry(self, guild: discord.Guild) -> None:
        attempt = self._backoff.get(guild.id, 0)
        delay = min(30 * (2 ** attempt), 900)
        self._backoff[guild.id] = min(attempt + 1, 5)
        self._retry_after[guild.id] = asyncio.get_running_loop().time() + delay

    def _connected(
        self, guild: discord.Guild, channel: discord.VoiceChannel | discord.StageChannel,
        *, reconnected: bool,
    ) -> None:
        self._managed_guilds.add(guild.id)
        self._retry_after.pop(guild.id, None)
        self._backoff.pop(guild.id, None)
        event = "🔄 Permanent Voice Reconnected" if reconnected else "🔊 Permanent Voice Connected"
        logger.info("%s in guild %s, channel %s.", event, guild.id, channel.id)
        asyncio.create_task(send_log(guild, LogEvent(
            LogType.VOICE, event, fields=[("Channel", channel.mention, True)],
        )))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PermanentVoiceCog(bot))
