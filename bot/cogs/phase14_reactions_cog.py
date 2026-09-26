"""Configurable rules, reaction roles, and role-based automatic reactions."""
from __future__ import annotations

import re

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import delete, select

from bot.core.logger import logger
from bot.core.permissions import StaffLevel, requires_level
from bot.database.engine import async_session
from bot.database.models.phase14 import ReactionRoleMapping, RoleReactionRule
from bot.services import config_service
from bot.services.logging_service import LogEvent, LogType, send_log

_CUSTOM = re.compile(r"^<a?:(\w{2,32}):(\d{15,22})>$")


def emoji_key(emoji: discord.PartialEmoji | discord.Emoji | str) -> str:
    if isinstance(emoji, str):
        match = _CUSTOM.fullmatch(emoji.strip())
        if match:
            return f"id:{match.group(2)}"
        return f"u:{emoji.strip()}"
    if emoji.id:
        return f"id:{emoji.id}"
    return f"u:{emoji.name}"


def resolve_emoji(guild: discord.Guild, value: str) -> tuple[str, str, discord.PartialEmoji | str]:
    value = value.strip()
    match = _CUSTOM.fullmatch(value)
    if match:
        emoji = guild.get_emoji(int(match.group(2)))
        if emoji is None:
            raise ValueError("That custom emoji is unavailable in this server.")
        return emoji_key(value), str(emoji), emoji
    if not value or len(value) > 64 or value in {"<", ">"}:
        raise ValueError("Enter a valid Unicode emoji or a custom emoji from this server.")
    return emoji_key(value), value, value


async def configured_role(guild: discord.Guild, role_id: int) -> discord.Role | None:
    return guild.get_role(role_id)


class Phase14ReactionsCog(commands.Cog):
    rules = app_commands.Group(name="rules", description="Configure and publish the server rules", guild_only=True)
    reaction_role = app_commands.Group(name="reaction-role", description="Configure message reaction roles", guild_only=True)
    role_reaction = app_commands.Group(name="role-reaction", description="Configure role-based message reactions", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @rules.command(name="configure", description="Set rules channel, exact message, and comma-separated reactions")
    @requires_level(StaffLevel.ADMIN)
    async def configure_rules(self, interaction: discord.Interaction, channel: discord.TextChannel,
                              message: app_commands.Range[str, 1, 4000],
                              emojis: app_commands.Range[str, 1, 500]):
        parsed = [part.strip() for part in emojis.split(",") if part.strip()]
        if len(parsed) > 20:
            return await interaction.response.send_message("Use at most 20 rules emojis.", ephemeral=True)
        for emoji in parsed:
            try:
                resolve_emoji(interaction.guild, emoji)
            except ValueError:
                continue  # Invalid/deleted custom emoji is skipped at publish time; fallback is used if none work.
        await config_service.set_setting(interaction.guild.id, "rules_channel", channel.id, updated_by=interaction.user.id)
        await config_service.set_setting(interaction.guild.id, "rules_message", str(message), updated_by=interaction.user.id)
        await config_service.set_setting(interaction.guild.id, "rules_emojis", ",".join(parsed), updated_by=interaction.user.id)
        await interaction.response.send_message("✅ Rules configuration saved for this server.", ephemeral=True)

    @rules.command(name="publish", description="Send the configured rules and add configured reactions")
    @requires_level(StaffLevel.ADMIN)
    async def publish_rules(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        channel_id = await config_service.get_setting(interaction.guild.id, "rules_channel")
        message = await config_service.get_setting(interaction.guild.id, "rules_message")
        emojis = await config_service.get_setting(interaction.guild.id, "rules_emojis")
        channel = interaction.guild.get_channel(int(channel_id)) if channel_id else None
        if not isinstance(channel, discord.TextChannel) or not message:
            return await interaction.followup.send("Configure rules first with `/rules configure`.", ephemeral=True)
        try:
            posted = await channel.send(embed=discord.Embed(description=str(message)[:4000]),
                                        allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Could not publish rules to channel %s", channel.id)
            return await interaction.followup.send("❌ Could not send the rules in the configured channel.", ephemeral=True)
        added = 0
        for raw in str(emojis or "").split(","):
            if not raw.strip():
                continue
            try:
                _, _, emoji = resolve_emoji(interaction.guild, raw)
                await posted.add_reaction(emoji)
                added += 1
            except (ValueError, discord.Forbidden, discord.HTTPException):
                logger.warning("Rules emoji %r could not be added in guild %s", raw, interaction.guild.id)
        if added == 0:
            try:
                await posted.add_reaction("✅")
                added = 1
            except discord.HTTPException:
                logger.exception("Could not add fallback reaction to rules message %s", posted.id)
        await interaction.followup.send(f"✅ Published rules in {channel.mention} with {added} reaction(s). Message ID: `{posted.id}`", ephemeral=True)
        await send_log(interaction.guild, LogEvent(LogType.MESSAGE, "📜 Rules Message Published", staff=interaction.user,
                                                   fields=[("Channel", channel.mention, True), ("Reactions", str(added), True)],
                                                   ids={"Message ID": posted.id}))

    @reaction_role.command(name="add", description="Map one reaction on a server message to a role")
    @app_commands.describe(channel="Channel containing the target message", message_id="Discord message ID",
                           emoji="Unicode emoji or this server's custom emoji", role="Role to give/remove", persistent="Keep role when reaction is removed")
    @requires_level(StaffLevel.ADMIN)
    async def add_reaction_role(self, interaction: discord.Interaction, channel: discord.TextChannel,
                                message_id: app_commands.Range[str, 1, 22], emoji: app_commands.Range[str, 1, 100],
                                role: discord.Role, persistent: bool = False):
        try:
            message = await channel.fetch_message(int(message_id))
            key, display, reaction = resolve_emoji(interaction.guild, emoji)
        except (ValueError, discord.NotFound, discord.Forbidden, discord.HTTPException):
            return await interaction.response.send_message("❌ Message or emoji is invalid or inaccessible.", ephemeral=True)
        me = interaction.guild.me
        if role.is_default() or role.managed or me.top_role <= role:
            return await interaction.response.send_message("❌ The bot cannot assign this role because it is protected or above its role.", ephemeral=True)
        try:
            await message.add_reaction(reaction)
        except (discord.Forbidden, discord.HTTPException):
            return await interaction.response.send_message("❌ I could not add that reaction; the mapping was not changed.", ephemeral=True)
        async with async_session() as session:
            async with session.begin():
                current = await session.scalar(select(ReactionRoleMapping).where(
                    ReactionRoleMapping.guild_id == interaction.guild.id,
                    ReactionRoleMapping.message_id == message.id,
                    ReactionRoleMapping.emoji_key == key,
                ))
                if current:
                    current.channel_id = channel.id
                    current.role_id = role.id
                    current.emoji_display = display
                    current.persistent = persistent
                else:
                    session.add(ReactionRoleMapping(guild_id=interaction.guild.id, channel_id=channel.id,
                                                    message_id=message.id, emoji_key=key, emoji_display=display,
                                                    role_id=role.id, persistent=persistent))
        await interaction.response.send_message(f"✅ {display} now maps to {role.mention} on message `{message.id}`.", ephemeral=True)

    @reaction_role.command(name="remove", description="Remove a reaction-role mapping")
    @requires_level(StaffLevel.ADMIN)
    async def remove_reaction_role(self, interaction: discord.Interaction, message_id: app_commands.Range[str, 1, 22],
                                   emoji: app_commands.Range[str, 1, 100]):
        try:
            key, _, _ = resolve_emoji(interaction.guild, emoji)
        except ValueError as exc:
            return await interaction.response.send_message(str(exc), ephemeral=True)
        async with async_session() as session:
            async with session.begin():
                result = await session.execute(delete(ReactionRoleMapping).where(
                    ReactionRoleMapping.guild_id == interaction.guild.id,
                    ReactionRoleMapping.message_id == int(message_id), ReactionRoleMapping.emoji_key == key,
                ))
        await interaction.response.send_message("✅ Mapping removed." if result.rowcount else "No matching mapping was found.", ephemeral=True)

    @role_reaction.command(name="add", description="React to messages from members holding a role")
    @requires_level(StaffLevel.ADMIN)
    async def add_role_reaction(self, interaction: discord.Interaction, role: discord.Role,
                                emoji: app_commands.Range[str, 1, 100]):
        try:
            _, display, _ = resolve_emoji(interaction.guild, emoji)
        except ValueError as exc:
            return await interaction.response.send_message(str(exc), ephemeral=True)
        async with async_session() as session:
            async with session.begin():
                current = await session.scalar(select(RoleReactionRule).where(
                    RoleReactionRule.guild_id == interaction.guild.id, RoleReactionRule.role_id == role.id,
                ))
                if current:
                    current.emoji_display = display
                    current.enabled = True
                else:
                    session.add(RoleReactionRule(guild_id=interaction.guild.id, role_id=role.id, emoji_display=display))
        await interaction.response.send_message(f"✅ Messages from members with {role.mention} will receive {display}.", ephemeral=True)

    @role_reaction.command(name="remove", description="Disable automatic reaction for a role")
    @requires_level(StaffLevel.ADMIN)
    async def remove_role_reaction(self, interaction: discord.Interaction, role: discord.Role):
        async with async_session() as session:
            async with session.begin():
                result = await session.execute(delete(RoleReactionRule).where(
                    RoleReactionRule.guild_id == interaction.guild.id, RoleReactionRule.role_id == role.id,
                ))
        await interaction.response.send_message("✅ Rule removed." if result.rowcount else "No rule was configured for this role.", ephemeral=True)

    async def _reaction_role_event(self, payload: discord.RawReactionActionEvent, *, add: bool):
        if payload.guild_id is None or payload.user_id == self.bot.user.id:
            return
        async with async_session() as session:
            mapping = await session.scalar(select(ReactionRoleMapping).where(
                ReactionRoleMapping.guild_id == payload.guild_id,
                ReactionRoleMapping.message_id == payload.message_id,
                ReactionRoleMapping.emoji_key == emoji_key(payload.emoji),
            ))
        if mapping is None or (not add and mapping.persistent):
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        role = guild.get_role(mapping.role_id)
        member = guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return
        if role is None or member is None or member.bot or role.managed or guild.me.top_role <= role:
            return
        try:
            if add:
                if role not in member.roles:
                    await member.add_roles(role, reason="Configured reaction role")
            elif role in member.roles:
                await member.remove_roles(role, reason="Configured reaction role removed")
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Could not apply reaction role in guild %s", guild.id)
            return
        action = "Role Added" if add else "Role Removed"
        await send_log(guild, LogEvent(LogType.ROLE, f"🎭 Reaction Role {action}", user=member,
                                       fields=[("Role", role.mention, True), ("Action", action, True),
                                               ("Reaction", mapping.emoji_display, True)],
                                       ids={"Message ID": payload.message_id}))
        log_id = await config_service.get_setting(guild.id, "reaction_role_logs")
        channel = guild.get_channel(int(log_id)) if log_id else None
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(embed=discord.Embed(
                    title="🎭 Reaction Role", description=f"Member: {member.mention}\nRole: {role.mention}\nAction: {action}\nReaction: {mapping.emoji_display}",
                    color=discord.Color.green() if add else discord.Color.orange(),
                ), allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                logger.exception("Could not send reaction-role result log in %s", guild.id)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        await self._reaction_role_event(payload, add=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        await self._reaction_role_event(payload, add=False)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot or message.webhook_id:
            return
        async with async_session() as session:
            rules = list((await session.scalars(select(RoleReactionRule).where(
                RoleReactionRule.guild_id == message.guild.id, RoleReactionRule.enabled.is_(True),
            ))).all())
        role_ids = {role.id for role in message.author.roles}
        matched = [rule for rule in rules if rule.role_id in role_ids]
        for rule in matched:
            try:
                _, _, emoji = resolve_emoji(message.guild, rule.emoji_display)
                await message.add_reaction(emoji)
            except (ValueError, discord.Forbidden, discord.HTTPException):
                logger.warning("Role-based reaction %r failed in guild %s", rule.emoji_display, message.guild.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(Phase14ReactionsCog(bot))
