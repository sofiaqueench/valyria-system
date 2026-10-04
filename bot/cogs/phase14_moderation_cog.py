"""Phase 14 moderation and Empress commands, using central permissions and logging."""
from __future__ import annotations

import re

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from bot.core.logger import logger
from bot.core.permissions import StaffLevel, get_staff_level, is_role_hierarchy_safe, requires_level
from bot.database.engine import async_session
from bot.database.models.phase14 import ChannelLockState, ModerationAction
from bot.services import config_service
from bot.services.logging_service import LogEvent, LogType, SendResult, send_log
from bot.services.member_xp_service import get_member_xp_mutation_lock

_DURATION = re.compile(r"^(\d{1,5})(s|m|h|d)$", re.IGNORECASE)
_MAX_TIMEOUT_SECONDS = 28 * 24 * 60 * 60


def parse_duration(value: str) -> int:
    match = _DURATION.fullmatch(value.strip())
    if not match:
        raise ValueError("Use a duration such as 10m, 6h, or 7d.")
    amount = int(match.group(1))
    seconds = amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2).lower()]
    if seconds < 60 or seconds > _MAX_TIMEOUT_SECONDS:
        raise ValueError("Timeout must be between 1 minute and 28 days.")
    return seconds


class Phase14ModerationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _dm(self, user: discord.abc.User, *, title: str, guild_name: str, reason: str,
                  moderator: str, action: str, duration: str | None = None) -> bool:
        embed = discord.Embed(title=title, color=discord.Color.orange())
        if duration:
            embed.add_field(name="Duration", value=duration, inline=False)
        embed.add_field(name="Reason", value=reason[:1024], inline=False)
        embed.add_field(name="Action", value=action, inline=True)
        embed.add_field(name="Moderator", value=moderator, inline=True)
        if action != "Empress Ban":
            embed.description = f"Server: {guild_name}"
        try:
            await user.send(embed=embed)
            return True
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("Moderation DM failed for user %s (%s): %s", user.id, action, exc)
            return False

    async def _record(self, guild: discord.Guild, target_id: int, moderator: discord.abc.User,
                      action: str, reason: str, duration: int | None = None,
                      *, source: str = "Manual", message_id: int | None = None) -> None:
        async with async_session() as session:
            async with session.begin():
                session.add(ModerationAction(
                    guild_id=guild.id, target_id=target_id, moderator_id=moderator.id,
                    action_type=action, reason=reason, duration_seconds=duration,
                    source=source, message_id=message_id,
                ))

    async def automod_ban_member(
        self, member: discord.Member, *, message_id: int, detected_word: str,
        previous_warnings: int, message_deleted: bool,
    ) -> bool:
        """Apply an AutoMod threshold ban through the existing Phase 14 ban/log pathway."""
        guild = member.guild
        reason = "AutoMod: Member had 3 previous warnings and committed another prohibited-word violation."
        bot_member = guild.me
        can_ban = (
            member.id != guild.owner_id
            and bot_member is not None
            and bot_member.guild_permissions.ban_members
            and bot_member.top_role > member.top_role
        )
        dm_ok = False
        if can_ban:
            try:
                await member.send(
                    "Your message was removed because it contained a prohibited word.\n\n"
                    "You previously received 3 warnings.\nThis was your 4th violation.\n"
                    f"You have therefore been banned from {guild.name}.\n\nReason: {reason}"
                )
                dm_ok = True
            except (discord.Forbidden, discord.HTTPException):
                logger.warning("AutoMod ban DM failed for member %s in guild %s", member.id, guild.id)
        failure: str | None = None
        if not can_ban:
            failure = "The bot lacks Ban Members permission, is below the target role, or cannot ban the server owner."
        else:
            try:
                await member.ban(reason=reason, delete_message_seconds=0)
            except discord.Forbidden:
                failure = "Discord denied the AutoMod ban; check the bot's Ban Members permission and role hierarchy."
            except discord.HTTPException as exc:
                failure = f"Discord could not complete the AutoMod ban ({exc.code})."
                logger.exception("AutoMod ban failed for member %s in guild %s", member.id, guild.id)

        async with async_session() as session:
            async with session.begin():
                action = await session.scalar(select(ModerationAction).where(
                    ModerationAction.guild_id == guild.id,
                    ModerationAction.message_id == message_id,
                ))
                if action is not None:
                    action.action_type = "automod_ban_failed" if failure else "ban"

        fields = [
            ("Source", "AutoMod", True), ("Previous warning count", str(previous_warnings), True),
            ("Violation number", "4", True), ("Detected rule", detected_word, True),
            ("Message deleted", "Yes" if message_deleted else "No — permission/API failure", True),
            ("Ban DM", "Sent" if dm_ok else "Failed / unavailable", True),
        ]
        event = LogEvent(
            LogType.AUTOMOD, "🚫 AutoMod Ban Failed" if failure else "🔨 AutoMod Ban",
            target=member, staff=self.bot.user, reason=reason,
            fields=fields + ([ ("Failure", failure, False) ] if failure else []),
            ids={"Guild ID": guild.id, "Message ID": message_id},
        )
        result = await send_log(guild, event)
        if result is not SendResult.SENT:
            await send_log(guild, LogEvent(
                LogType.MODERATION, event.title, target=member, staff=self.bot.user,
                reason=reason, fields=event.fields, ids=event.ids,
            ))
        return failure is None

    async def _log(self, interaction: discord.Interaction, action: str, target: discord.abc.User | None,
                   reason: str, *, target_id: int | None = None, duration: str | None = None,
                   dm_ok: bool | None = None, empress: bool = False, extra: list[tuple[str, str, bool]] | None = None):
        fields = list(extra or [])
        fields.append(("Timestamp", discord.utils.utcnow().strftime("%Y-%m-%d %H:%M UTC"), True))
        fields.append(("Guild", f"{interaction.guild.name} ({interaction.guild.id})", True))
        if duration:
            fields.append(("Duration", duration, True))
        if dm_ok is not None:
            fields.append(("DM", "Sent" if dm_ok else "Failed / unavailable", True))
        await send_log(interaction.guild, LogEvent(
            LogType.MODERATION, f"{'👑 EMPRESS' if empress else '🔨'} {action}",
            target=target, staff=interaction.user, reason=reason,
            fields=fields, ids={"Guild ID": interaction.guild.id, **({"Target ID": target_id} if target_id else {})},
        ))

    async def _staff_target_check(self, actor: discord.Member, target: discord.Member) -> str | None:
        actor_level = await get_staff_level(actor)
        target_level = await get_staff_level(target)
        if actor.id != actor.guild.owner_id and actor_level <= target_level:
            return "You cannot moderate a member at your Staff level or above."
        if not await is_role_hierarchy_safe(actor, target):
            return "Discord role hierarchy prevents you from moderating this member."
        me = actor.guild.me
        if me is None or me.top_role <= target.top_role:
            return "The bot's highest role must be above the target's highest role."
        return None

    async def _empress_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False
        role_id = await config_service.get_setting(interaction.guild.id, "empress_role")
        if role_id and any(role.id == int(role_id) for role in interaction.user.roles):
            return True
        await interaction.response.send_message("❌ You are not authorized to use Empress commands.", ephemeral=True)
        return False

    async def _empress_reply(self, interaction: discord.Interaction, *, gif: bool = True):
        embed = discord.Embed(description="Ready to serve you, my Empress.", color=discord.Color.pink())
        url = await config_service.get_setting(interaction.guild.id, "empress_gif") if gif else None
        if url and str(url).startswith(("https://", "http://")):
            embed.set_image(url=str(url))
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _ban_member(self, interaction: discord.Interaction, member: discord.Member, reason: str,
                          *, empress: bool):
        await interaction.response.defer(ephemeral=True, thinking=True)
        if member.id == interaction.guild.owner_id:
            return await interaction.followup.send("❌ The server owner cannot be banned by the bot.", ephemeral=True)
        if interaction.guild.me is None or interaction.guild.me.top_role <= member.top_role:
            return await interaction.followup.send("❌ The bot's highest role must be above the target's highest role.", ephemeral=True)
        if not empress:
            failure = await self._staff_target_check(interaction.user, member)
            if failure:
                return await interaction.followup.send(f"❌ {failure}", ephemeral=True)
        try:
            await member.ban(reason=f"{reason} (by {interaction.user} / {interaction.user.id})", delete_message_seconds=0)
        except discord.Forbidden:
            return await interaction.followup.send("❌ The bot cannot ban this member. Check its Ban Members permission and role position.", ephemeral=True)
        except discord.HTTPException:
            logger.exception("Discord API failed to ban member %s in guild %s", member.id, interaction.guild.id)
            return await interaction.followup.send("❌ Discord could not complete the ban.", ephemeral=True)
        try:
            await member.send(embed=discord.Embed(
                title=("👑 You have been banned from " if empress else "🔨 You have been banned from ") + interaction.guild.name,
                description=f"Action: {'Empress Ban' if empress else 'Ban'}\nReason: {reason}\nModerator: {'The Empress' if empress else interaction.user.display_name}\nServer: {interaction.guild.name}",
                color=discord.Color.red(),
            ))
            dm_ok = True
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("Ban DM failed for %s: %s", member.id, exc)
            dm_ok = False
        await self._record(interaction.guild, member.id, interaction.user, "empress_ban" if empress else "ban", reason)
        await self._log(interaction, "Empress Ban" if empress else "Ban", member, reason, dm_ok=dm_ok, empress=empress)
        if empress:
            embed = discord.Embed(description="Ready to serve you, my Empress.", color=discord.Color.pink())
            gif = await config_service.get_setting(interaction.guild.id, "empress_gif")
            if gif and str(gif).startswith(("https://", "http://")):
                embed.set_image(url=str(gif))
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.followup.send(f"✅ Banned {member.mention}. DM {'sent' if dm_ok else 'could not be sent'}.", ephemeral=True)

    @app_commands.command(name="ban", description="Ban a member with a required reason")
    @app_commands.describe(user="Member to ban", reason="Required moderation reason")
    @app_commands.guild_only()
    @requires_level(StaffLevel.STAFF)
    async def ban(self, interaction: discord.Interaction, user: discord.Member, reason: app_commands.Range[str, 1, 1000]):
        await self._ban_member(interaction, user, str(reason).strip(), empress=False)

    @app_commands.command(name="warn", description="Warn a member with a required reason")
    @app_commands.describe(user="Member to warn", reason="Required warning reason")
    @app_commands.guild_only()
    @requires_level(StaffLevel.STAFF)
    async def warn(self, interaction: discord.Interaction, user: discord.Member, reason: app_commands.Range[str, 1, 1000]):
        reason = str(reason).strip()
        failure = await self._staff_target_check(interaction.user, user)
        if failure:
            return await interaction.response.send_message(f"❌ {failure}", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with get_member_xp_mutation_lock(interaction.guild.id, user.id):
            dm_ok = await self._dm(user, title=f"⚠️ You have received a warning in {interaction.guild.name}",
                                   guild_name=interaction.guild.name, reason=reason,
                                   moderator=interaction.user.display_name, action="Warning")
            await self._record(interaction.guild, user.id, interaction.user, "warn", reason)
        await self._log(interaction, "Warn", user, reason, dm_ok=dm_ok)
        await interaction.followup.send(f"✅ Warned {user.mention}. DM {'sent' if dm_ok else 'could not be sent'}.", ephemeral=True)

    @app_commands.command(name="timeout", description="Timeout a member for a specified duration")
    @app_commands.describe(user="Member to timeout", duration="Examples: 10m, 6h, 1d", reason="Required reason")
    @app_commands.guild_only()
    @requires_level(StaffLevel.STAFF)
    async def timeout_member(self, interaction: discord.Interaction, user: discord.Member,
                             duration: app_commands.Range[str, 2, 6], reason: app_commands.Range[str, 1, 1000]):
        try:
            seconds = parse_duration(duration)
        except ValueError as exc:
            return await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
        failure = await self._staff_target_check(interaction.user, user)
        if failure:
            return await interaction.response.send_message(f"❌ {failure}", ephemeral=True)
        try:
            from datetime import timedelta
            await user.timeout(discord.utils.utcnow() + timedelta(seconds=seconds), reason=str(reason))
        except discord.Forbidden:
            return await interaction.response.send_message("❌ The bot lacks permission or is below the target.", ephemeral=True)
        except discord.HTTPException:
            logger.exception("Timeout failed for %s", user.id)
            return await interaction.response.send_message("❌ Discord could not apply the timeout.", ephemeral=True)
        seconds_text = f"{seconds // 3600}h" if seconds >= 3600 and seconds % 3600 == 0 else f"{seconds // 60}m"
        dm_ok = await self._dm(user, title=f"⏳ You have been timed out in {interaction.guild.name}",
                               guild_name=interaction.guild.name, reason=str(reason),
                               moderator=interaction.user.display_name, action="Timeout", duration=seconds_text)
        await self._record(interaction.guild, user.id, interaction.user, "timeout", str(reason), seconds)
        await self._log(interaction, "Timeout", user, str(reason), duration=seconds_text, dm_ok=dm_ok)
        await interaction.response.send_message(f"✅ Timed out {user.mention} for {seconds_text}.", ephemeral=True)

    async def _set_locked(self, interaction: discord.Interaction, channel: discord.TextChannel, reason: str,
                          *, empress: bool, lock: bool):
        await interaction.response.defer(ephemeral=True, thinking=True)
        async with async_session() as session:
            async with session.begin():
                state = await session.scalar(select(ChannelLockState).where(
                    ChannelLockState.guild_id == interaction.guild.id,
                    ChannelLockState.channel_id == channel.id,
                ))
                overwrite = channel.overwrites_for(interaction.guild.default_role)
                if lock:
                    if state:
                        return await interaction.followup.send("⚠️ This channel is already locked by the bot.", ephemeral=True)
                    state = ChannelLockState(
                        guild_id=interaction.guild.id, channel_id=channel.id,
                        previous_send_messages=overwrite.send_messages,
                        previous_send_messages_in_threads=overwrite.send_messages_in_threads,
                        locked_by=interaction.user.id,
                    )
                    session.add(state)
                    overwrite.send_messages = False
                    overwrite.send_messages_in_threads = False
                else:
                    if not state:
                        return await interaction.followup.send("⚠️ This channel has no saved bot lock state.", ephemeral=True)
                    overwrite.send_messages = state.previous_send_messages
                    overwrite.send_messages_in_threads = state.previous_send_messages_in_threads
                    await session.delete(state)
                try:
                    await channel.set_permissions(interaction.guild.default_role, overwrite=overwrite,
                                                  reason=reason[:400] or ("Empress Lock" if empress else "Staff Lock"))
                except (discord.Forbidden, discord.HTTPException):
                    await session.rollback()
                    return await interaction.followup.send("❌ Could not change channel permissions; saved lock state was rolled back.", ephemeral=True)
        action = ("Empress Lock" if lock else "Empress Unlock") if empress else ("Lock" if lock else "Unlock")
        await self._record(interaction.guild, channel.id, interaction.user, "empress_lock" if empress and lock else "empress_unlock" if empress else "lock" if lock else "unlock", reason)
        await self._log(interaction, action, None, reason, target_id=channel.id, empress=empress,
                        extra=[("Channel", channel.mention, True)])
        if empress:
            await self._empress_reply(interaction)
        else:
            await interaction.followup.send(f"✅ {action} applied to {channel.mention}.", ephemeral=True)

    @app_commands.command(name="lock", description="Lock a text channel while preserving its previous state")
    @app_commands.guild_only()
    @app_commands.describe(channel="Channel to lock", reason="Why the channel is being locked")
    @requires_level(StaffLevel.STAFF)
    async def lock(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None,
                   reason: app_commands.Range[str, 1, 500] = "Staff lock"):
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            return await interaction.response.send_message("Choose a text channel to lock.", ephemeral=True)
        await self._set_locked(interaction, target, str(reason), empress=False, lock=True)

    @app_commands.command(name="unlock", description="Restore a bot-locked channel's previous send permissions")
    @app_commands.guild_only()
    @app_commands.describe(channel="Channel to unlock", reason="Why the channel is being unlocked")
    @requires_level(StaffLevel.STAFF)
    async def unlock(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None,
                     reason: app_commands.Range[str, 1, 500] = "Staff unlock"):
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            return await interaction.response.send_message("Choose a text channel to unlock.", ephemeral=True)
        await self._set_locked(interaction, target, str(reason), empress=False, lock=False)

    @app_commands.command(name="empress-ban", description="Ban a member as the configured Empress")
    @app_commands.guild_only()
    async def empress_ban(self, interaction: discord.Interaction, user: discord.Member,
                          reason: app_commands.Range[str, 1, 1000]):
        if await self._empress_check(interaction):
            await self._ban_member(interaction, user, str(reason), empress=True)

    @app_commands.command(name="empress-unban", description="Unban a user ID as the configured Empress")
    @app_commands.guild_only()
    async def empress_unban(self, interaction: discord.Interaction, user_id: app_commands.Range[str, 17, 20],
                            reason: app_commands.Range[str, 1, 1000]):
        if not await self._empress_check(interaction):
            return
        if not str(user_id).isdigit():
            return await interaction.response.send_message("❌ Enter a numeric Discord user ID.", ephemeral=True)
        target_id = int(user_id)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            entry = await interaction.guild.fetch_ban(discord.Object(id=target_id))
            await interaction.guild.unban(entry.user, reason=str(reason))
        except discord.NotFound:
            return await interaction.followup.send("❌ That user is not banned from this server.", ephemeral=True)
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Empress unban failed for %s", user_id)
            return await interaction.followup.send("❌ Discord could not complete the unban.", ephemeral=True)
        dm_ok = await self._dm(entry.user, title=f"👑 You have been unbanned from {interaction.guild.name}",
                               guild_name=interaction.guild.name, reason=str(reason), moderator="The Empress", action="Empress Unban")
        await self._record(interaction.guild, target_id, interaction.user, "empress_unban", str(reason))
        await self._log(interaction, "Empress Unban", entry.user, str(reason), dm_ok=dm_ok, empress=True)
        embed = discord.Embed(description="Ready to serve you, my Empress.", color=discord.Color.pink())
        gif = await config_service.get_setting(interaction.guild.id, "empress_gif")
        if gif and str(gif).startswith(("https://", "http://")):
            embed.set_image(url=str(gif))
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="empress-lock", description="Lock a channel as the configured Empress")
    @app_commands.guild_only()
    async def empress_lock(self, interaction: discord.Interaction, channel: discord.TextChannel,
                           reason: app_commands.Range[str, 1, 500] = "Empress lock"):
        if await self._empress_check(interaction):
            await self._set_locked(interaction, channel, str(reason), empress=True, lock=True)

    @app_commands.command(name="empress-unlock", description="Restore a channel's saved state as the configured Empress")
    @app_commands.guild_only()
    async def empress_unlock(self, interaction: discord.Interaction, channel: discord.TextChannel,
                             reason: app_commands.Range[str, 1, 500] = "Empress unlock"):
        if await self._empress_check(interaction):
            await self._set_locked(interaction, channel, str(reason), empress=True, lock=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(Phase14ModerationCog(bot))
