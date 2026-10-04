"""Configurable rules, reaction roles, and role-based automatic reactions."""
from __future__ import annotations

import asyncio
import re
from weakref import WeakValueDictionary

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


def role_assignment_error(guild: discord.Guild, role: discord.Role) -> str | None:
    bot_member = guild.me
    if bot_member is None:
        return "The bot's guild member is unavailable."
    if not bot_member.guild_permissions.manage_roles:
        return "The bot needs Manage Roles to assign reaction roles."
    if role.is_default() or role.managed or role.permissions.administrator:
        return "@everyone, managed, and Administrator roles cannot be used as reaction roles."
    if bot_member.top_role <= role:
        return "The bot's highest role must be above the selected role."
    return None


class Phase14ReactionsCog(commands.Cog):
    rules = app_commands.Group(name="rules", description="Configure and publish the server rules", guild_only=True)
    reaction_role = app_commands.Group(name="reaction-role", description="Configure message reaction roles", guild_only=True)
    role_reaction = app_commands.Group(name="role-reaction", description="Configure role-based message reactions", guild_only=True)
    reactionrole = app_commands.Group(name="reactionrole", description="Manage reaction-to-role mappings", guild_only=True)
    autoreaction = app_commands.Group(name="autoreaction", description="Manage automatic channel reactions", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._role_event_locks: WeakValueDictionary[tuple[int, int, int], asyncio.Lock] = WeakValueDictionary()

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
        role_error = role_assignment_error(interaction.guild, role)
        if role_error:
            return await interaction.response.send_message(f"❌ {role_error}", ephemeral=True)
        try:
            await message.add_reaction(reaction)
        except (discord.Forbidden, discord.HTTPException):
            return await interaction.response.send_message("❌ I could not add that reaction; the mapping was not changed.", ephemeral=True)
        await config_service.get_or_create_guild(interaction.guild.id)
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
        await send_log(interaction.guild, LogEvent(LogType.ROLE, "🎭 Reaction role mapping configured", staff=interaction.user,
            fields=[("Role", role.mention, True), ("Emoji", display, True)], ids={"Message": message.id}))

    @reactionrole.command(name="add", description="Add a reaction role mapping to a message")
    @requires_level(StaffLevel.STAFF)
    async def reactionrole_add(self, interaction: discord.Interaction, channel: discord.TextChannel,
                               message_id: app_commands.Range[str, 1, 22], emoji: app_commands.Range[str, 1, 100],
                               role: discord.Role, persistent: bool = False):
        await Phase14ReactionsCog.add_reaction_role.callback(self, interaction, channel, message_id, emoji, role, persistent)

    @reactionrole.command(name="remove", description="Remove a reaction role mapping")
    @requires_level(StaffLevel.STAFF)
    async def reactionrole_remove(self, interaction: discord.Interaction, message_id: app_commands.Range[str, 1, 22], emoji: app_commands.Range[str, 1, 100]):
        await Phase14ReactionsCog.remove_reaction_role.callback(self, interaction, message_id, emoji)

    @reactionrole.command(name="list", description="List reaction role mappings")
    @requires_level(StaffLevel.STAFF)
    async def reactionrole_list(self, interaction: discord.Interaction):
        async with async_session() as session:
            rows = list((await session.scalars(select(ReactionRoleMapping).where(ReactionRoleMapping.guild_id == interaction.guild_id).order_by(ReactionRoleMapping.id).limit(40))).all())
        text = "\n".join(f"<#{r.channel_id}> · `{r.message_id}` · {r.emoji_display} → <@&{r.role_id}>" for r in rows) or "No reaction role mappings."
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @reactionrole.command(name="clear", description="Clear mappings for one message")
    @requires_level(StaffLevel.ADMIN)
    async def reactionrole_clear(self, interaction: discord.Interaction,
                                 message_id: app_commands.Range[str, 1, 22]):
        if not message_id.isdecimal() or int(message_id) <= 0:
            return await interaction.response.send_message("Enter a valid Discord message ID.", ephemeral=True)
        target_message_id = int(message_id)
        async with async_session() as session:
            result = await session.execute(delete(ReactionRoleMapping).where(
                ReactionRoleMapping.guild_id == interaction.guild_id,
                ReactionRoleMapping.message_id == target_message_id,
            )); await session.commit()
        await interaction.response.send_message(f"Cleared {result.rowcount or 0} mapping(s).", ephemeral=True)
        await send_log(interaction.guild, LogEvent(LogType.ROLE, "🎭 Reaction role mappings cleared", staff=interaction.user,
            fields=[("Mappings", str(result.rowcount or 0), True)], ids={"Message ID": target_message_id}))

    @reaction_role.command(name="remove", description="Remove a reaction-role mapping")
    @requires_level(StaffLevel.ADMIN)
    async def remove_reaction_role(self, interaction: discord.Interaction, message_id: app_commands.Range[str, 1, 22],
                                   emoji: app_commands.Range[str, 1, 100]):
        if not message_id.isdecimal() or int(message_id) <= 0:
            return await interaction.response.send_message("Enter a valid Discord message ID.", ephemeral=True)
        target_message_id = int(message_id)
        try:
            key, _, _ = resolve_emoji(interaction.guild, emoji)
        except ValueError as exc:
            return await interaction.response.send_message(str(exc), ephemeral=True)
        async with async_session() as session:
            async with session.begin():
                result = await session.execute(delete(ReactionRoleMapping).where(
                    ReactionRoleMapping.guild_id == interaction.guild.id,
                    ReactionRoleMapping.message_id == target_message_id, ReactionRoleMapping.emoji_key == key,
                ))
        await interaction.response.send_message("✅ Mapping removed." if result.rowcount else "No matching mapping was found.", ephemeral=True)
        if result.rowcount:
            await send_log(interaction.guild, LogEvent(LogType.ROLE, "🎭 Reaction role mapping removed", staff=interaction.user,
                fields=[("Emoji", emoji, True)], ids={"Message": target_message_id}))

    @role_reaction.command(name="add", description="React to messages from members holding a role")
    @requires_level(StaffLevel.ADMIN)
    async def add_role_reaction(self, interaction: discord.Interaction, role: discord.Role,
                                emoji: app_commands.Range[str, 1, 100]):
        try:
            _, display, _ = resolve_emoji(interaction.guild, emoji)
        except ValueError as exc:
            return await interaction.response.send_message(str(exc), ephemeral=True)
        await config_service.get_or_create_guild(interaction.guild.id)
        async with async_session() as session:
            async with session.begin():
                current = await session.scalar(select(RoleReactionRule).where(
                    RoleReactionRule.guild_id == interaction.guild.id, RoleReactionRule.role_id == role.id,
                    RoleReactionRule.channel_id.is_(None),
                ))
                if current:
                    current.emoji_display = display
                    current.enabled = True
                else:
                    session.add(RoleReactionRule(guild_id=interaction.guild.id, role_id=role.id, channel_id=None, emoji_display=display))
        await interaction.response.send_message(f"✅ Messages from members with {role.mention} will receive {display}.", ephemeral=True)
        await send_log(interaction.guild, LogEvent(LogType.MESSAGE, "⚡ Automatic reaction configured", staff=interaction.user,
            fields=[("Role", role.mention, True), ("Emoji", display, True)]))

    @autoreaction.command(name="add", description="Add a role-gated reaction rule for a channel")
    @requires_level(StaffLevel.STAFF)
    async def autoreaction_add(self, interaction: discord.Interaction, channel: discord.TextChannel,
                               role: discord.Role, emoji: app_commands.Range[str, 1, 100]):
        try:
            _, display, _ = resolve_emoji(interaction.guild, emoji)
        except ValueError as exc:
            return await interaction.response.send_message(str(exc), ephemeral=True)
        await config_service.get_or_create_guild(interaction.guild.id)
        async with async_session() as session:
            async with session.begin():
                row = await session.scalar(select(RoleReactionRule).where(RoleReactionRule.guild_id == interaction.guild_id,
                    RoleReactionRule.channel_id == channel.id, RoleReactionRule.role_id == role.id))
                if row:
                    row.emoji_display, row.enabled = display, True
                else:
                    session.add(RoleReactionRule(guild_id=interaction.guild_id, channel_id=channel.id, role_id=role.id,
                                                 emoji_display=display, enabled=True))
        await send_log(interaction.guild, LogEvent(LogType.MESSAGE, "⚡ Automatic reaction configured", staff=interaction.user,
            fields=[("Channel", channel.mention, True), ("Role", role.mention, True), ("Emoji", display, True)]))
        await interaction.response.send_message(f"{channel.mention} + {role.mention} → {display}", ephemeral=True)

    @autoreaction.command(name="remove", description="Remove a channel/role automatic reaction")
    @requires_level(StaffLevel.STAFF)
    async def autoreaction_remove(self, interaction: discord.Interaction, channel: discord.TextChannel, role: discord.Role):
        async with async_session() as session:
            result = await session.execute(delete(RoleReactionRule).where(RoleReactionRule.guild_id == interaction.guild_id,
                RoleReactionRule.channel_id == channel.id, RoleReactionRule.role_id == role.id)); await session.commit()
        await send_log(interaction.guild, LogEvent(LogType.MESSAGE, "⚡ Automatic reaction rule removed", staff=interaction.user,
            fields=[("Channel", channel.mention, True), ("Role", role.mention, True)]))
        await interaction.response.send_message("Rule removed." if result.rowcount else "No matching rule.", ephemeral=True)

    @autoreaction.command(name="list", description="List automatic reaction rules")
    @requires_level(StaffLevel.STAFF)
    async def autoreaction_list(self, interaction: discord.Interaction):
        async with async_session() as session:
            rows = list((await session.scalars(select(RoleReactionRule).where(RoleReactionRule.guild_id == interaction.guild_id).order_by(RoleReactionRule.id).limit(40))).all())
        text = "\n".join(f"<#{r.channel_id or 0}> + <@&{r.role_id}> → {r.emoji_display}" for r in rows) or "No automatic reaction rules."
        await interaction.response.send_message(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @autoreaction.command(name="clear", description="Clear automatic reaction rules")
    @requires_level(StaffLevel.ADMIN)
    async def autoreaction_clear(self, interaction: discord.Interaction):
        async with async_session() as session:
            result = await session.execute(delete(RoleReactionRule).where(RoleReactionRule.guild_id == interaction.guild_id)); await session.commit()
        await send_log(interaction.guild, LogEvent(LogType.MESSAGE, "⚡ Automatic reaction rules cleared", staff=interaction.user))
        await interaction.response.send_message(f"Cleared {result.rowcount or 0} rule(s).", ephemeral=True)

    @role_reaction.command(name="remove", description="Disable automatic reaction for a role")
    @requires_level(StaffLevel.ADMIN)
    async def remove_role_reaction(self, interaction: discord.Interaction, role: discord.Role):
        async with async_session() as session:
            async with session.begin():
                result = await session.execute(delete(RoleReactionRule).where(
                    RoleReactionRule.guild_id == interaction.guild.id, RoleReactionRule.role_id == role.id,
                    RoleReactionRule.channel_id.is_(None),
                ))
        await interaction.response.send_message("✅ Rule removed." if result.rowcount else "No rule was configured for this role.", ephemeral=True)
        if result.rowcount:
            await send_log(interaction.guild, LogEvent(LogType.MESSAGE, "⚡ Automatic reaction rule removed", staff=interaction.user,
                fields=[("Role", role.mention, True)]))

    async def _reaction_role_event(self, payload: discord.RawReactionActionEvent, *, add: bool):
        if payload.guild_id is None or self.bot.user is None or payload.user_id == self.bot.user.id:
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
        if payload.channel_id != mapping.channel_id:
            logger.warning("Reaction-role mapping channel mismatch for message %s in guild %s.", payload.message_id, guild.id)
            return
        if role is None:
            logger.warning("Reaction-role mapping references deleted role %s in guild %s; mapping retained for staff review.", mapping.role_id, guild.id)
            await send_log(guild, LogEvent(LogType.ROLE, "⚠️ Reaction Role references a missing role",
                fields=[("Role ID", str(mapping.role_id), True), ("Reaction", mapping.emoji_display, True)],
                ids={"Message ID": payload.message_id, "Channel ID": payload.channel_id}))
            return
        member = guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return
        if member is None or member.bot:
            return
        role_error = role_assignment_error(guild, role)
        if role_error:
            logger.warning("Reaction role %s skipped in guild %s: %s", role.id, guild.id, role_error)
            return
        lock_key = (guild.id, member.id, role.id)
        lock = self._role_event_locks.setdefault(lock_key, asyncio.Lock())
        async with lock:
            try:
                # Use the idempotent API action instead of the possibly stale Member.roles cache.
                if add:
                    await member.add_roles(role, reason="Configured reaction role")
                else:
                    await member.remove_roles(role, reason="Configured reaction role removed")
            except (discord.Forbidden, discord.HTTPException):
                logger.exception("Could not apply reaction role in guild %s", guild.id)
                return
        action = "Role Added" if add else "Role Removed"
        await send_log(guild, LogEvent(LogType.ROLE, f"🎭 Reaction Role {action}", user=member,
                                       fields=[("Role", role.mention, True), ("Action", action, True),
                                               ("Reaction", mapping.emoji_display, True)],
                                       ids={"Message ID": payload.message_id, "Channel ID": payload.channel_id}))
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

    async def _report_deleted_reaction_message(self, guild_id: int | None, channel_id: int, message_id: int) -> None:
        if guild_id is None:
            return
        async with async_session() as session:
            rows = list((await session.scalars(select(ReactionRoleMapping).where(
                ReactionRoleMapping.guild_id == guild_id,
                ReactionRoleMapping.message_id == message_id,
            ))).all())
        if not rows:
            return
        guild = self.bot.get_guild(guild_id)
        logger.warning(
            "Deleted message %s in channel %s has %s reaction-role mapping(s); retaining them for staff review.",
            message_id, channel_id, len(rows),
        )
        if guild:
            await send_log(guild, LogEvent(LogType.ROLE, "⚠️ Reaction-role message was deleted; mappings retained",
                fields=[("Mappings", str(len(rows)), True)],
                ids={"Message ID": message_id, "Channel ID": channel_id}))

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        await self._report_deleted_reaction_message(payload.guild_id, payload.channel_id, payload.message_id)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        for message_id in payload.message_ids:
            await self._report_deleted_reaction_message(payload.guild_id, payload.channel_id, message_id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot or message.webhook_id:
            return
        async with async_session() as session:
            rules = list((await session.scalars(select(RoleReactionRule).where(
                RoleReactionRule.guild_id == message.guild.id, RoleReactionRule.enabled.is_(True),
            ))).all())
        role_ids = {role.id for role in message.author.roles}
        matched = [rule for rule in rules if rule.role_id in role_ids and (rule.channel_id is None or rule.channel_id == message.channel.id)]
        for rule in matched:
            try:
                _, _, emoji = resolve_emoji(message.guild, rule.emoji_display)
                await message.add_reaction(emoji)
            except (ValueError, discord.Forbidden, discord.HTTPException):
                logger.warning("Role-based reaction %r failed in guild %s", rule.emoji_display, message.guild.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(Phase14ReactionsCog(bot))
