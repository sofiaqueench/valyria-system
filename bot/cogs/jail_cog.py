"""Persistent member jail using the shared moderation, permission and logging services."""
from __future__ import annotations

import json

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from bot.core.logger import logger
from bot.core.permissions import StaffLevel, is_role_hierarchy_safe, requires_level
from bot.database.engine import async_session
from bot.database.models.security import JailChannelPermission, JailRecord
from bot.services import config_service
from bot.services.logging_service import LogEvent, LogType, send_log


def _active_query(guild_id: int, user_id: int):
    return select(JailRecord).where(JailRecord.guild_id == guild_id,
        JailRecord.user_id == user_id, JailRecord.status == "ACTIVE")


class JailCog(commands.Cog):
    jail = app_commands.Group(name="jail", description="Manage member jail", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _log(self, guild: discord.Guild, title: str, member: discord.Member, actor, reason: str):
        await send_log(guild, LogEvent(LogType.MODERATION, title, target=member, staff=actor,
            reason=reason, ids={"Guild ID": guild.id, "Member ID": member.id}))

    async def _restore_role_overwrites(self, guild: discord.Guild, role_id: int):
        async with async_session() as session:
            rows = (await session.scalars(select(JailChannelPermission).where(
                JailChannelPermission.guild_id == guild.id, JailChannelPermission.role_id == role_id))).all()
            snapshots = [(row.channel_id, row.had_overwrite, row.allow_bits, row.deny_bits) for row in rows]
        for channel_id, had, allow, deny in snapshots:
            channel = guild.get_channel(channel_id)
            role = guild.get_role(role_id)
            if channel is None or role is None:
                continue
            overwrite = (discord.PermissionOverwrite.from_pair(discord.Permissions(allow), discord.Permissions(deny))
                         if had else None)
            try:
                await channel.set_permissions(role, overwrite=overwrite, reason="Restore permissions after Jail role change")
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("Could not restore Jail overwrite for channel %s in guild %s", channel_id, guild.id)
        async with async_session() as session:
            async with session.begin():
                await session.execute(select(JailChannelPermission).where(
                    JailChannelPermission.guild_id == guild.id, JailChannelPermission.role_id == role_id))
                rows = (await session.scalars(select(JailChannelPermission).where(
                    JailChannelPermission.guild_id == guild.id, JailChannelPermission.role_id == role_id))).all()
                for row in rows:
                    await session.delete(row)

    async def _secure_jail_role(self, guild: discord.Guild, role: discord.Role):
        chat_id = await config_service.get_setting(guild.id, "jail_chat")
        voice_id = await config_service.get_setting(guild.id, "jail_voice")
        allow_chat = bool(await config_service.get_setting(guild.id, "jail_allow_chat"))
        async with async_session() as session:
            async with session.begin():
                existing_rows = (await session.scalars(select(JailChannelPermission).where(
                    JailChannelPermission.guild_id == guild.id, JailChannelPermission.role_id == role.id))).all()
                existing_ids = {row.channel_id for row in existing_rows}
                for channel in guild.channels:
                    if channel.id in existing_ids:
                        continue
                    old = channel.overwrites_for(role)
                    allow, deny = old.pair()
                    session.add(JailChannelPermission(guild_id=guild.id, role_id=role.id,
                        channel_id=channel.id, had_overwrite=role in channel.overwrites,
                        allow_bits=allow.value, deny_bits=deny.value))
        for channel in guild.channels:
            if channel.id == chat_id:
                overwrite = discord.PermissionOverwrite(view_channel=True, send_messages=allow_chat, read_message_history=True)
            elif channel.id == voice_id:
                overwrite = discord.PermissionOverwrite(view_channel=True, connect=True, speak=False)
            else:
                overwrite = discord.PermissionOverwrite(view_channel=False, send_messages=False,
                    read_message_history=False, connect=False, speak=False)
            try:
                await channel.set_permissions(role, overwrite=overwrite, reason="Configure private Jail role access")
            except (discord.Forbidden, discord.HTTPException) as exc:
                raise RuntimeError(f"Could not configure Jail access for #{channel.name}: {exc}") from exc

    @jail.command(name="config-role", description="Set and secure the Jail role")
    @requires_level(StaffLevel.ADMIN)
    async def config_role(self, interaction: discord.Interaction, role: discord.Role):
        guild = interaction.guild
        if role.is_default() or role.managed or role.permissions.administrator or guild.me.top_role <= role:
            return await interaction.response.send_message(
                "Choose a non-Administrator role below the bot's highest role.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        old_id = await config_service.get_setting(guild.id, "role_jail")
        if old_id and old_id != role.id:
            async with async_session() as session:
                active_record_id = await session.scalar(select(JailRecord.id).where(
                    JailRecord.guild_id == guild.id, JailRecord.status == "ACTIVE").limit(1))
            if active_record_id is not None:
                return await interaction.followup.send(
                    "Cannot change the Jail role while members have active Jail records. Unjail them first so they are not exposed.",
                    ephemeral=True)
        if old_id and old_id != role.id and guild.get_role(old_id):
            await self._restore_role_overwrites(guild, old_id)
        try:
            await self._secure_jail_role(guild, role)
            await config_service.set_setting(guild.id, "role_jail", role.id, interaction.user.id)
        except Exception as exc:
            logger.exception("Could not configure Jail role in guild %s", guild.id)
            return await interaction.followup.send(f"Jail role setup was incomplete: {exc}", ephemeral=True)
        await interaction.followup.send(f"Jail role set to {role.mention}. Its access is restricted to configured Jail rooms.", ephemeral=True)

    @jail.command(name="set-chat", description="Set the text channel jailed members can view")
    @requires_level(StaffLevel.ADMIN)
    async def set_chat(self, interaction: discord.Interaction, channel: discord.TextChannel | None):
        if channel:
            await config_service.set_setting(interaction.guild_id, "jail_chat", channel.id, interaction.user.id)
        else:
            await config_service.reset_setting(interaction.guild_id, "jail_chat")
        role_id = await config_service.get_setting(interaction.guild_id, "role_jail")
        role = interaction.guild.get_role(role_id) if role_id else None
        if role:
            await interaction.response.defer(ephemeral=True)
            try:
                await self._secure_jail_role(interaction.guild, role)
            except Exception as exc:
                logger.exception("Could not apply updated Jail chat permissions")
                return await interaction.followup.send(f"Jail chat setting saved, but permissions could not be fully updated: {exc}", ephemeral=True)
            return await interaction.followup.send(f"Jail text access updated to {channel.mention if channel else 'none'}.", ephemeral=True)
        await interaction.response.send_message("Jail text room saved. Configure a Jail role to apply channel restrictions.", ephemeral=True)

    @jail.command(name="set-voice", description="Set the voice channel jailed members can access")
    @requires_level(StaffLevel.ADMIN)
    async def set_voice(self, interaction: discord.Interaction, channel: discord.VoiceChannel | None):
        if channel:
            await config_service.set_setting(interaction.guild_id, "jail_voice", channel.id, interaction.user.id)
        else:
            await config_service.reset_setting(interaction.guild_id, "jail_voice")
        role_id = await config_service.get_setting(interaction.guild_id, "role_jail")
        role = interaction.guild.get_role(role_id) if role_id else None
        if role:
            await interaction.response.defer(ephemeral=True)
            try:
                await self._secure_jail_role(interaction.guild, role)
            except Exception as exc:
                logger.exception("Could not apply updated Jail voice permissions")
                return await interaction.followup.send(f"Jail voice setting saved, but permissions could not be fully updated: {exc}", ephemeral=True)
            return await interaction.followup.send(f"Jail voice access updated to {channel.mention if channel else 'none'}.", ephemeral=True)
        await interaction.response.send_message("Jail voice room saved. Configure a Jail role to apply channel restrictions.", ephemeral=True)

    @jail.command(name="config-log-channel", description="Set the centralized moderation log channel")
    @requires_level(StaffLevel.ADMIN)
    async def config_log_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await config_service.set_setting(interaction.guild_id, "log_moderation", channel.id, interaction.user.id)
        await interaction.response.send_message(f"Jail moderation logs will be sent to {channel.mention}.", ephemeral=True)

    @jail.command(name="apply", description="Jail a member")
    @app_commands.describe(user="Member to jail", reason="Reason for the jail")
    @requires_level(StaffLevel.STAFF)
    async def apply(self, interaction: discord.Interaction, user: discord.Member, reason: app_commands.Range[str, 1, 500]):
        guild = interaction.guild
        role_id = await config_service.get_setting(guild.id, "role_jail")
        role = guild.get_role(role_id) if role_id else None
        if role is None:
            return await interaction.response.send_message("Configure a Jail role first with `/jail config-role`.", ephemeral=True)
        async with async_session() as session:
            active = await session.scalar(_active_query(guild.id, user.id))
        if active is not None:
            if role not in user.roles:
                try:
                    await user.add_roles(role, reason="Restore the configured role for an existing active Jail")
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception("Could not restore active Jail role to member %s", user.id)
                    return await interaction.response.send_message("An active Jail record exists, but I could not restore its role.", ephemeral=True)
            return await interaction.response.send_message(f"{user.mention} already has an active Jail record.", ephemeral=True)
        bot_member = guild.me
        if (user.bot or user.guild_permissions.administrator
                or not await is_role_hierarchy_safe(interaction.user, user)
                or bot_member is None or bot_member.top_role <= user.top_role or bot_member.top_role <= role):
            return await interaction.response.send_message("That member cannot be jailed because of role hierarchy, Administrator bypass permissions, or target type.", ephemeral=True)
        if role in user.roles:
            return await interaction.response.send_message("That member already has the Jail role.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        original = [r.id for r in user.roles if not r.is_default() and not r.managed]
        try:
            await user.add_roles(role, reason=f"Jailed by {interaction.user}: {reason}")
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Failed to apply Jail role to member %s", user.id)
            return await interaction.followup.send("I could not apply the Jail role. Check my role position and permissions.", ephemeral=True)
        async with async_session() as session:
            async with session.begin():
                session.add(JailRecord(guild_id=guild.id, user_id=user.id, moderator_id=interaction.user.id,
                    reason=reason, original_role_ids=json.dumps(original), jail_role_id=role.id))
        cog = self.bot.get_cog("Phase14ModerationCog")
        if cog:
            await cog._dm(user, title=f"🔒 You were jailed in {guild.name}", guild_name=guild.name,
                reason=reason, moderator=interaction.user.name, action="Jail")
        await self._log(guild, "🔒 Member Jailed", user, interaction.user, reason)
        await interaction.followup.send(f"{user.mention} has been jailed. Their other roles were preserved.", ephemeral=True)

    @jail.command(name="status", description="Show a member's current Jail state")
    @requires_level(StaffLevel.STAFF)
    async def status(self, interaction: discord.Interaction, user: discord.Member):
        async with async_session() as session:
            row = await session.scalar(_active_query(interaction.guild_id, user.id).order_by(JailRecord.created_at.desc()))
        if row is None:
            return await interaction.response.send_message(f"{user.mention} is not recorded as jailed.", ephemeral=True)
        await interaction.response.send_message(f"{user.mention} is jailed since <t:{int(row.created_at.timestamp())}:R>. Reason: {row.reason}", ephemeral=True)

    @app_commands.command(name="unjail", description="Release a member from Jail")
    @app_commands.guild_only()
    @app_commands.describe(user="Member to release", reason="Reason for release")
    @requires_level(StaffLevel.STAFF)
    async def unjail(self, interaction: discord.Interaction, user: discord.Member, reason: str = "Jail completed"):
        guild = interaction.guild
        async with async_session() as session:
            row = await session.scalar(_active_query(guild.id, user.id).order_by(JailRecord.created_at.desc()))
            if row is None:
                return await interaction.response.send_message("No active Jail record was found.", ephemeral=True)
            role = guild.get_role(row.jail_role_id)
            try:
                if role and role in user.roles:
                    await user.remove_roles(role, reason=f"Unjailed by {interaction.user}: {reason}")
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("Could not remove Jail role from member %s", user.id)
                return await interaction.response.send_message("I could not remove the Jail role; no database changes were made.", ephemeral=True)
            available = {role.id: role for role in guild.roles if not role.managed and not role.is_default()
                         and guild.me.top_role > role}
            try:
                previous = set(json.loads(row.original_role_ids))
                restore = [available[rid] for rid in previous if rid in available and available[rid] not in user.roles]
                if restore:
                    await user.add_roles(*restore, reason="Restore roles after Jail")
            except (ValueError, TypeError, discord.Forbidden, discord.HTTPException):
                logger.exception("Could not fully restore applicable roles to member %s", user.id)
            row.status = "RELEASED"
            row.unjailed_by = interaction.user.id
            row.unjailed_at = discord.utils.utcnow()
            await session.commit()
        await self._log(guild, "🔓 Member Unjailed", user, interaction.user, reason)
        await interaction.response.send_message(f"{user.mention} has been released. Existing roles were preserved and available original roles restored.", ephemeral=True)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        async with async_session() as session:
            row = await session.scalar(_active_query(member.guild.id, member.id).order_by(JailRecord.created_at.desc()))
            if row is None:
                return
            role = member.guild.get_role(row.jail_role_id)
            if role is None:
                logger.error("Active Jail record has missing role %s in guild %s", row.jail_role_id, member.guild.id)
                return
            try:
                await member.add_roles(role, reason="Restore active Jail after member rejoin")
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("Could not restore Jail for rejoining member %s", member.id)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        role_id = await config_service.get_setting(channel.guild.id, "role_jail")
        role = channel.guild.get_role(role_id) if role_id else None
        if role is None:
            return
        async with async_session() as session:
            async with session.begin():
                existing = await session.scalar(select(JailChannelPermission.id).where(
                    JailChannelPermission.guild_id == channel.guild.id,
                    JailChannelPermission.role_id == role.id,
                    JailChannelPermission.channel_id == channel.id))
                if existing is None:
                    allow, deny = channel.overwrites_for(role).pair()
                    session.add(JailChannelPermission(guild_id=channel.guild.id, role_id=role.id,
                        channel_id=channel.id, had_overwrite=role in channel.overwrites,
                        allow_bits=allow.value, deny_bits=deny.value))
        try:
            await channel.set_permissions(role, overwrite=discord.PermissionOverwrite(
                view_channel=False, send_messages=False, read_message_history=False, connect=False, speak=False),
                reason="Keep new channel inaccessible to jailed members")
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Could not apply Jail restriction to new channel %s in guild %s", channel.id, channel.guild.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(JailCog(bot))
