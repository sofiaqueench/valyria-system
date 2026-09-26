"""Normal Member XP, level commands, and periodic Voice XP tracking."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.core.logger import logger
from bot.core.permissions import StaffLevel, requires_level
from bot.services import config_service
from bot.services.level_progression_service import get_current_level_role
from bot.services.logging_service import LogEvent, LogType, send_log
from bot.services.member_xp_service import (
    XP_PER_LEVEL,
    add_member_xp,
    award_message_xp,
    award_voice_xp,
    calculate_level,
    get_leaderboard,
    get_member_xp,
    set_member_xp,
)


@dataclass
class VoiceSession:
    channel_id: int
    credited_at: datetime


class MemberLevelingCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._voice_sessions: dict[tuple[int, int], VoiceSession] = {}

    async def cog_load(self):
        if not self.voice_xp_tick.is_running():
            self.voice_xp_tick.start()

    def cog_unload(self):
        self.voice_xp_tick.cancel()

    # No other message XP listener exists in this project.
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if (
            message.guild is None
            or not isinstance(message.author, discord.Member)
            or message.author.bot
            or message.webhook_id is not None
            or message.is_system()
        ):
            return
        if not (message.content.strip() or message.attachments or message.stickers):
            return
        try:
            await award_message_xp(message.author, source_channel=message.channel)
        except Exception:
            logger.exception("Message XP failed for member %s in guild %s.", message.author.id, message.guild.id)

    @commands.Cog.listener()
    async def on_ready(self):
        # After a restart, begin tracking currently connected members from now; never award downtime.
        self._voice_sessions.clear()
        now = discord.utils.utcnow()
        for guild in self.bot.guilds:
            for member in guild.members:
                state = member.voice
                if state is not None and await self._voice_eligible(member, state):
                    self._voice_sessions[(guild.id, member.id)] = VoiceSession(state.channel.id, now)

    async def process_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        key = (member.guild.id, member.id)
        if await self._voice_eligible(member, after):
            session = self._voice_sessions.get(key)
            if session is None:
                self._voice_sessions[key] = VoiceSession(after.channel.id, discord.utils.utcnow())
            else:
                # Moving between eligible channels preserves continuous eligible listening time.
                session.channel_id = after.channel.id
        else:
            self._voice_sessions.pop(key, None)

    async def _voice_eligible(self, member: discord.Member, state: discord.VoiceState) -> bool:
        channel = state.channel
        if member.bot or channel is None or not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            return False
        guild = member.guild
        if guild.afk_channel and channel.id == guild.afk_channel.id:
            return False
        excluded_ids = {
            await config_service.get_setting(guild.id, "jail_voice"),
            await config_service.get_setting(guild.id, "verification_voice"),
        }
        if channel.id in excluded_ids:
            return False
        if await config_service.get_setting(guild.id, "voice_xp_ignore_deafened") and (state.deaf or state.self_deaf):
            return False
        # No minimum-member threshold: Temp Voice and normal channels count even when the member is alone.
        return True

    @tasks.loop(seconds=10)
    async def voice_xp_tick(self):
        await self._process_voice_xp_tick()

    async def _process_voice_xp_tick(self):
        now = discord.utils.utcnow()
        for key, session in list(self._voice_sessions.items()):
            try:
                guild = self.bot.get_guild(key[0])
                member = guild.get_member(key[1]) if guild else None
                state = member.voice if member else None
                if member is None or state is None or not await self._voice_eligible(member, state):
                    self._voice_sessions.pop(key, None)
                    continue
                if state.channel.id != session.channel_id:
                    session.channel_id = state.channel.id

                interval = int(await config_service.get_setting(guild.id, "voice_xp_interval") or 60)
                amount_per_interval = int(await config_service.get_setting(guild.id, "voice_xp_amount") or 0)
                interval = max(1, interval)
                periods = int((now - session.credited_at).total_seconds() // interval)
                if periods <= 0:
                    continue
                if amount_per_interval > 0:
                    await award_voice_xp(member, amount_per_interval * periods)
                session.credited_at += timedelta(seconds=interval * periods)
            except Exception:
                # Keep the session and retry at the next tick; XP failures never stop the loop.
                logger.exception("Voice XP tick failed for guild/member %s.", key)

    @voice_xp_tick.before_loop
    async def before_voice_xp_tick(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ /level
    @app_commands.command(name="level", description="Show a member's Member XP and level")
    @app_commands.guild_only()
    @app_commands.describe(user="Member to inspect (defaults to you)")
    async def level(self, interaction: discord.Interaction, user: discord.Member | None = None):
        target = user or interaction.user
        total = await get_member_xp(interaction.guild_id, target.id)
        current_level = calculate_level(total)
        progress = total % XP_PER_LEVEL
        xp_needed = XP_PER_LEVEL - progress
        level_role = await get_current_level_role(interaction.guild, current_level)
        embed = discord.Embed(title="📈 Member Level", color=discord.Color.blurple())
        embed.set_author(name=target.display_name, icon_url=target.display_avatar.url)
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="Level", value=str(current_level), inline=True)
        embed.add_field(name="Total Member XP", value=f"{total:,}", inline=True)
        embed.add_field(name="Progress to next level", value=f"{progress}/{XP_PER_LEVEL} XP · {xp_needed} needed", inline=False)
        embed.add_field(name="Current Level Role", value=level_role.mention if level_role else "None", inline=False)
        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------- /leaderboard
    @app_commands.command(name="leaderboard", description="Top Member XP in this server")
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction):
        rows = await get_leaderboard(interaction.guild_id, limit=10)
        if not rows:
            return await interaction.response.send_message("No Member XP has been earned yet.", ephemeral=True)
        lines = []
        for rank, (member_id, total) in enumerate(rows, start=1):
            member = interaction.guild.get_member(member_id)
            name = member.display_name if member else f"Member {member_id}"
            lines.append(f"**{rank}.** {name} — Level {calculate_level(total)} · {total:,} XP")
        embed = discord.Embed(title="🏆 Member XP Leaderboard", description="\n".join(lines), color=discord.Color.gold())
        await interaction.response.send_message(embed=embed)

    # -------------------------------------------------------------- /member-xp
    member_xp = app_commands.Group(
        name="member-xp", description="Authorized management of normal Member XP", guild_only=True
    )

    async def _manual_log(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        result,
        action: str,
        reason: str | None,
    ) -> None:
        await send_log(interaction.guild, LogEvent(
            LogType.XP,
            f"✏️ Member XP {action}",
            user=member,
            staff=interaction.user,
            reason=reason,
            xp_change=result.applied_amount,
            fields=[
                ("New total", str(result.new_total), True),
                ("Current level", str(result.new_level), True),
            ],
        ))

    @member_xp.command(name="add", description="Add normal Member XP to a member")
    @app_commands.describe(member="Target member", amount="XP to add", reason="Reason for the adjustment")
    @requires_level(StaffLevel.ADMIN)
    async def member_xp_add(
        self, interaction: discord.Interaction, member: discord.Member,
        amount: app_commands.Range[int, 1, 1_000_000], reason: str | None = None,
    ):
        if member.bot:
            return await interaction.response.send_message("Bots cannot receive Member XP.", ephemeral=True)
        result = await add_member_xp(member, amount, source="admin", source_channel=interaction.channel)
        await self._manual_log(interaction, member, result, "Added", reason)
        await interaction.response.send_message(
            f"Added {result.applied_amount} Member XP to {member.mention}. Total: {result.new_total:,} (Level {result.new_level}).",
            ephemeral=True,
        )

    @member_xp.command(name="remove", description="Remove normal Member XP from a member")
    @app_commands.describe(member="Target member", amount="XP to remove", reason="Reason for the adjustment")
    @requires_level(StaffLevel.ADMIN)
    async def member_xp_remove(
        self, interaction: discord.Interaction, member: discord.Member,
        amount: app_commands.Range[int, 1, 1_000_000], reason: str | None = None,
    ):
        if member.bot:
            return await interaction.response.send_message("Bots cannot receive Member XP.", ephemeral=True)
        result = await add_member_xp(member, -amount, source="admin", source_channel=interaction.channel)
        await self._manual_log(interaction, member, result, "Removed", reason)
        await interaction.response.send_message(
            f"Removed {-result.applied_amount} Member XP from {member.mention}. Total: {result.new_total:,} (Level {result.new_level}).",
            ephemeral=True,
        )

    @member_xp.command(name="set", description="Set a member's normal Member XP total")
    @app_commands.describe(member="Target member", total="New total XP", reason="Reason for the adjustment")
    @requires_level(StaffLevel.ADMIN)
    async def member_xp_set(
        self, interaction: discord.Interaction, member: discord.Member,
        total: app_commands.Range[int, 0, 100_000_000], reason: str | None = None,
    ):
        if member.bot:
            return await interaction.response.send_message("Bots cannot receive Member XP.", ephemeral=True)
        result = await set_member_xp(member, total, source_channel=interaction.channel)
        await self._manual_log(interaction, member, result, "Set", reason)
        await interaction.response.send_message(
            f"Set {member.mention} to {result.new_total:,} Member XP (Level {result.new_level}).",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(MemberLevelingCog(bot))
