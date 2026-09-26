"""Game cleanup panel and reversible nickname snapshots, integrated with existing game roles/settings."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import delete, select

from bot.core.logger import logger
from bot.core.permissions import StaffLevel, get_staff_level, is_role_hierarchy_safe, requires_level
from bot.database.engine import async_session
from bot.database.models.phase14 import NicknameSnapshot, ReactionRoleMapping
from bot.services import config_service
from bot.services.logging_service import LogEvent, LogType, send_log
from bot.services.staff_promotion_service import get_rank_config
from bot.views.game_cleanup import GameCleanupView

_PROTECTED_ROLE_KEYS = (
    "role_owner", "role_admin", "role_head_moderator", "role_moderator", "role_trial_moderator",
    "role_support", "role_staff", "role_verification_team", "role_verified", "role_unverified",
    "role_male", "role_female", "role_jail",
)
_PROTECTED_ROLE_PERMISSIONS = (
    "administrator", "manage_guild", "manage_roles", "manage_channels", "manage_messages",
    "ban_members", "kick_members", "moderate_members",
)


async def _protected_role_ids(guild: discord.Guild) -> set[int]:
    result: set[int] = set()
    for key in _PROTECTED_ROLE_KEYS:
        role_id = await config_service.get_setting(guild.id, key)
        if role_id:
            result.add(int(role_id))
    empress_role_id = await config_service.get_setting(guild.id, "empress_role")
    if empress_role_id:
        result.add(int(empress_role_id))
    try:
        ranks = await get_rank_config(guild.id)
        result.update(rank.role_id for rank in ranks.values())
    except Exception:
        logger.exception("Could not load Staff promotion roles for guild %s", guild.id)
    return result


class GameCleanupCog(commands.Cog):
    cleanup = app_commands.Group(name="game-cleanup", description="Publish and configure the game self-service panel", guild_only=True)
    nickname = app_commands.Group(name="game-nickname", description="Safely set and restore a player's game nickname", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(GameCleanupView(self))

    @cleanup.command(name="publish", description="Publish the member self-service cleanup panel")
    @requires_level(StaffLevel.ADMIN)
    async def publish(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await interaction.response.defer(ephemeral=True, thinking=True)
        embed = discord.Embed(
            title="🎮 Game Cleanup",
            description="Use these controls on your own account to restore your saved nickname or remove configured game/self roles.",
            color=discord.Color.blurple(),
        )
        try:
            await channel.send(embed=embed, view=GameCleanupView(self))
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Could not publish game cleanup panel in %s", channel.id)
            return await interaction.followup.send("❌ I could not publish the panel in that channel.", ephemeral=True)
        await interaction.followup.send(f"✅ Game cleanup panel published in {channel.mention}.", ephemeral=True)

    @cleanup.command(name="set-game-role", description="Choose the one role managed by Remove Game Role")
    @requires_level(StaffLevel.ADMIN)
    async def set_game_role(self, interaction: discord.Interaction, role: discord.Role):
        if role.is_default() or role.managed or any(getattr(role.permissions, name) for name in _PROTECTED_ROLE_PERMISSIONS):
            return await interaction.response.send_message("❌ Choose a normal, unmanaged game role.", ephemeral=True)
        if role.id in await _protected_role_ids(interaction.guild):
            return await interaction.response.send_message("❌ Staff, moderation, and verification roles cannot be configured as a removable Game Role.", ephemeral=True)
        if interaction.guild.me.top_role <= role:
            return await interaction.response.send_message("❌ The bot's highest role must be above the Game Role.", ephemeral=True)
        await config_service.set_setting(interaction.guild.id, "game_cleanup_role", role.id, updated_by=interaction.user.id)
        await interaction.response.send_message(f"✅ Cleanup will remove only {role.mention}.", ephemeral=True)

    @nickname.command(name="set", description="Save a member's exact current nickname, then set their game nickname")
    @requires_level(StaffLevel.STAFF)
    async def set_game_nickname(self, interaction: discord.Interaction, member: discord.Member,
                                nickname: app_commands.Range[str, 1, 32], game_id: app_commands.Range[str, 1, 80]):
        if not await is_role_hierarchy_safe(interaction.user, member):
            return await interaction.response.send_message("❌ You cannot change this member's nickname due to Staff or Discord hierarchy.", ephemeral=True)
        if interaction.user.id != interaction.guild.owner_id and await get_staff_level(interaction.user) <= await get_staff_level(member):
            return await interaction.response.send_message("❌ You cannot change the nickname of a member at your Staff level or above.", ephemeral=True)
        if interaction.guild.me.top_role <= member.top_role:
            return await interaction.response.send_message("❌ The bot role must be above this member.", ephemeral=True)
        async with async_session() as session:
            async with session.begin():
                existing = await session.scalar(select(NicknameSnapshot).where(
                    NicknameSnapshot.guild_id == interaction.guild.id, NicknameSnapshot.user_id == member.id,
                ))
                if existing:
                    return await interaction.response.send_message("⚠️ This member already has a saved game nickname. Use Reset Nickname first.", ephemeral=True)
                snapshot = NicknameSnapshot(guild_id=interaction.guild.id, user_id=member.id,
                                            previous_nickname=member.nick, game_id=str(game_id))
                session.add(snapshot)
        try:
            await member.edit(nick=str(nickname), reason=f"Game {game_id}; previous nickname saved")
        except (discord.Forbidden, discord.HTTPException):
            async with async_session() as session:
                async with session.begin():
                    await session.execute(delete(NicknameSnapshot).where(
                        NicknameSnapshot.guild_id == interaction.guild.id, NicknameSnapshot.user_id == member.id,
                    ))
            logger.exception("Could not set game nickname for %s", member.id)
            return await interaction.response.send_message("❌ The nickname could not be changed; saved nickname data was cleared.", ephemeral=True)
        await send_log(interaction.guild, LogEvent(LogType.MODERATION, "🎮 Game Nickname Changed", user=member,
                                                   staff=interaction.user, reason=f"Game: {game_id}",
                                                   fields=[("Previous nickname", snapshot.previous_nickname or member.name, True), ("New nickname", nickname, True)]))
        await interaction.response.send_message(f"✅ Saved the previous nickname and set {member.mention}'s game nickname.", ephemeral=True)

    async def _member(self, interaction: discord.Interaction) -> discord.Member | None:
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This control is available inside a server only.", ephemeral=True)
            return None
        return interaction.user

    async def reset_nickname(self, interaction: discord.Interaction):
        member = await self._member(interaction)
        if member is None:
            return
        async with async_session() as session:
            snapshot = await session.scalar(select(NicknameSnapshot).where(
                NicknameSnapshot.guild_id == interaction.guild.id, NicknameSnapshot.user_id == member.id,
            ))
        if snapshot is None:
            return await interaction.response.send_message("ℹ️ No saved game nickname was found for you.", ephemeral=True)
        if interaction.guild.me.top_role <= member.top_role:
            return await interaction.response.send_message("❌ The bot cannot restore your nickname because of role hierarchy.", ephemeral=True)
        try:
            await member.edit(nick=snapshot.previous_nickname, reason=f"Game nickname reset ({snapshot.game_id})")
        except (discord.Forbidden, discord.HTTPException):
            return await interaction.response.send_message("❌ The saved nickname could not be restored. Check the bot's Manage Nicknames permission and role position.", ephemeral=True)
        async with async_session() as session:
            async with session.begin():
                await session.execute(delete(NicknameSnapshot).where(
                    NicknameSnapshot.guild_id == interaction.guild.id, NicknameSnapshot.user_id == member.id,
                ))
        await send_log(interaction.guild, LogEvent(LogType.ROLE, "🔄 Game Nickname Restored", user=member,
                                                   reason=f"Game: {snapshot.game_id}"))
        await interaction.response.send_message("✅ Your original nickname has been restored.", ephemeral=True)

    async def remove_game_role(self, interaction: discord.Interaction):
        member = await self._member(interaction)
        if member is None:
            return
        role_id = await config_service.get_setting(interaction.guild.id, "game_cleanup_role")
        role = interaction.guild.get_role(int(role_id)) if role_id else None
        if role is None:
            return await interaction.response.send_message("ℹ️ No Game Role is configured. Ask an Admin to use `/game-cleanup set-game-role`.", ephemeral=True)
        if role not in member.roles:
            return await interaction.response.send_message("ℹ️ You do not have the configured Game Role.", ephemeral=True)
        if role.managed or interaction.guild.me.top_role <= role:
            return await interaction.response.send_message("❌ The bot cannot remove this role because of its hierarchy.", ephemeral=True)
        try:
            await member.remove_roles(role, reason="Member used Game Cleanup panel")
        except (discord.Forbidden, discord.HTTPException):
            return await interaction.response.send_message("❌ The Game Role could not be removed.", ephemeral=True)
        await interaction.response.send_message(f"✅ Removed {role.mention} from your account.", ephemeral=True)
        await send_log(interaction.guild, LogEvent(LogType.ROLE, "🎮 Game Role Removed", user=member,
                                                   fields=[("Role", role.mention, True)]))

    async def remove_self_role(self, interaction: discord.Interaction):
        member = await self._member(interaction)
        if member is None:
            return
        async with async_session() as session:
            mappings = list((await session.scalars(select(ReactionRoleMapping).where(
                ReactionRoleMapping.guild_id == interaction.guild.id,
            ))).all())
        allowed_ids = {mapping.role_id for mapping in mappings}
        allowed_ids.difference_update(await _protected_role_ids(interaction.guild))
        roles = [role for role in member.roles if role.id in allowed_ids and not role.managed
                 and not any(getattr(role.permissions, name) for name in _PROTECTED_ROLE_PERMISSIONS)]
        if not roles:
            return await interaction.response.send_message("ℹ️ You do not have a removable configured self-role.", ephemeral=True)
        roles = [role for role in roles if interaction.guild.me.top_role > role]
        if not roles:
            return await interaction.response.send_message("❌ The bot cannot remove your configured self-role because of role hierarchy.", ephemeral=True)
        try:
            await member.remove_roles(*roles, reason="Member used Game Cleanup panel")
        except (discord.Forbidden, discord.HTTPException):
            return await interaction.response.send_message("❌ The configured self-role could not be removed.", ephemeral=True)
        await interaction.response.send_message("✅ Removed your configured self-role(s): " + ", ".join(role.mention for role in roles), ephemeral=True)
        for role in roles:
            await send_log(interaction.guild, LogEvent(LogType.ROLE, "🎭 Self Role Removed", user=member,
                                                       fields=[("Role", role.mention, True)]))


async def setup(bot: commands.Bot):
    await bot.add_cog(GameCleanupCog(bot))
