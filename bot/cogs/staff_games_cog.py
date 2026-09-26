"""Staff-only panels, persistent game sessions, results, and host XP."""
from __future__ import annotations

import asyncio
from datetime import timezone

import discord
from discord import app_commands
from discord.ext import commands

from bot.config.game_registry import GAMES
from bot.core.permissions import StaffLevel, get_staff_level, level_label, requires_level
from bot.core.logger import logger
from bot.services import config_service
from bot.services.logging_service import LogEvent, LogType, send_log
from bot.services.staff_games_service import (
    cancel_game, finish_game, get_active_game, get_pending_result,
    save_result_message, start_game,
)
from bot.services.staff_promotion_service import get_rank_config
from bot.services.staff_xp_service import get_staff_xp_mutation_lock
from bot.views.staff_games import (
    StaffGameHostPicker, StaffGamePanelView, StaffGameResultModal,
    panel_embed,
)

_GAME_CHOICES = [app_commands.Choice(name=game.label, value=game.key) for game in GAMES.values()]
_STAFF_ROLE_KEYS = (
    "role_owner", "role_admin", "role_head_moderator", "role_moderator",
    "role_trial_moderator", "role_support", "role_staff",
)
_RESULT_PUBLISH_LOCKS: dict[tuple[int, int], asyncio.Lock] = {}


def _aware(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _duration(seconds: int) -> str:
    seconds = max(0, seconds)
    minutes = seconds // 60
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes or not parts:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    return " ".join(parts)


class StaffGamesCog(commands.Cog):
    game_panel = app_commands.Group(
        name="game-panel", description="Publish Staff-only panels for configured games", guild_only=True,
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        for game_key in GAMES:
            self.bot.add_view(StaffGamePanelView(self, game_key))

    @game_panel.command(name="publish", description="Publish or refresh one game's private Staff panel")
    @app_commands.choices(game=_GAME_CHOICES)
    @app_commands.describe(game="The game panel to publish")
    @requires_level(StaffLevel.ADMIN)
    async def publish_panel(self, interaction: discord.Interaction, game: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True, thinking=True)
        definition = GAMES[game.value]
        panel_id = await config_service.get_setting(interaction.guild_id, definition.panel_channel_key)
        results_id = await config_service.get_setting(interaction.guild_id, definition.results_channel_key)
        channel = interaction.guild.get_channel(panel_id) if panel_id else None
        if not isinstance(channel, discord.TextChannel):
            return await interaction.followup.send(
                f"Configure `{definition.panel_channel_key}` first with `/config set-channel`.", ephemeral=True,
            )
        if results_id and int(results_id) == channel.id:
            return await interaction.followup.send("The Staff panel channel and results channel must be different.", ephemeral=True)
        try:
            await self._ensure_private_panel_channel(interaction.guild, channel)
        except ValueError as exc:
            return await interaction.followup.send(f"🔒 {exc}", ephemeral=True)
        permissions = channel.permissions_for(interaction.guild.me)
        if not permissions.send_messages or not permissions.embed_links:
            return await interaction.followup.send("I need Send Messages and Embed Links in this panel channel.", ephemeral=True)
        message = await channel.send(embed=panel_embed(game.value), view=StaffGamePanelView(self, game.value))
        await interaction.followup.send(
            f"✅ {definition.emoji} {definition.label} Staff panel published in {channel.mention} (message `{message.id}`).",
            ephemeral=True,
        )

    async def _ensure_private_panel_channel(self, guild: discord.Guild, channel: discord.TextChannel) -> None:
        if channel.permissions_for(guild.default_role).view_channel:
            raise ValueError("@everyone can view this channel. Deny View Channel for @everyone before publishing the Staff panel.")

        allowed_role_ids: set[int] = set()
        for key in _STAFF_ROLE_KEYS:
            role_id = await config_service.get_setting(guild.id, key)
            if role_id:
                allowed_role_ids.add(int(role_id))
        ranks = await get_rank_config(guild.id)
        allowed_role_ids.update(rank.role_id for rank in ranks.values())
        for role in guild.roles:
            if role.id == guild.default_role.id:
                continue
            if channel.permissions_for(role).view_channel and role.id not in allowed_role_ids and not role.permissions.administrator:
                raise ValueError(f"Role {role.mention} can view this channel but is not an authorized Staff role.")

        overwrite_sources = [channel.overwrites]
        if channel.category is not None:
            overwrite_sources.append(channel.category.overwrites)
        seen_member_ids: set[int] = set()
        for overwrites in overwrite_sources:
            for target, overwrite in overwrites.items():
                if not isinstance(target, discord.Member) or target.id in seen_member_ids:
                    continue
                seen_member_ids.add(target.id)
                if overwrite.view_channel is True and await get_staff_level(target) < StaffLevel.STAFF:
                    raise ValueError(f"Non-Staff member {target.mention} has direct access to this channel.")

    async def _is_game_staff(self, member: discord.Member) -> bool:
        return await get_staff_level(member) >= StaffLevel.STAFF

    async def is_panel_authorized(self, interaction: discord.Interaction, game_key: str) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False
        definition = GAMES.get(game_key)
        if definition is None or not await self._is_game_staff(interaction.user):
            return False
        configured_channel = await config_service.get_setting(interaction.guild.id, definition.panel_channel_key)
        return configured_channel is not None and int(configured_channel) == interaction.channel_id

    async def _deny_or_continue(self, interaction: discord.Interaction, game_key: str) -> bool:
        if await self.is_panel_authorized(interaction, game_key):
            return True
        await interaction.response.send_message("❌ You do not have permission to use this Game Panel.", ephemeral=True)
        return False

    async def _staff_host_options(self, guild: discord.Guild, preferred_id: int | None = None):
        configured_role_ids = set()
        for key in _STAFF_ROLE_KEYS:
            role_id = await config_service.get_setting(guild.id, key)
            if role_id:
                configured_role_ids.add(int(role_id))
        ranks = await get_rank_config(guild.id)
        configured_role_ids.update(rank.role_id for rank in ranks.values())
        members = list(guild.members)
        if not guild.chunked:
            try:
                members = [member async for member in guild.fetch_members(limit=None)]
            except discord.HTTPException:
                if not members:
                    raise ValueError("Could not load this server's member list. Check the Members intent and try again.")
        eligible: list[tuple[discord.Member, str]] = []
        for member in members:
            if member.bot or not (
                member.id == guild.owner_id
                or member.guild_permissions.administrator
                or any(role.id in configured_role_ids for role in member.roles)
            ):
                continue
            level = await get_staff_level(member)
            if level >= StaffLevel.STAFF:
                held_rank = next((rank for rank in ranks.values() if rank.role_id in {role.id for role in member.roles}), None)
                rank_label = (
                    f"High Staff · Rank {held_rank.rank}" if held_rank and held_rank.rank_type == "high_staff"
                    else level_label(level)
                )
                eligible.append((member, rank_label))
        eligible.sort(key=lambda entry: (entry[0].id != preferred_id, entry[0].display_name.casefold(), entry[0].id))
        return eligible

    async def panel_start(self, interaction: discord.Interaction, game_key: str):
        if not await self._deny_or_continue(interaction, game_key):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        definition = GAMES[game_key]
        if await get_active_game(interaction.guild.id, game_key):
            return await interaction.followup.send(f"⚠️ There is already an active {definition.label} game.", ephemeral=True)
        try:
            members = await self._staff_host_options(interaction.guild, interaction.user.id)
        except ValueError as exc:
            return await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
        if not members:
            return await interaction.followup.send("No Staff or High Staff members are available to host.", ephemeral=True)
        await interaction.followup.send(
            f"Select the Staff member hosting {definition.emoji} **{definition.label}**.",
            view=StaffGameHostPicker(self, game_key, "start", members), ephemeral=True,
        )

    async def panel_submit(self, interaction: discord.Interaction, game_key: str):
        if not await self._deny_or_continue(interaction, game_key):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        active = await get_active_game(interaction.guild.id, game_key)
        if active is None:
            pending = await get_pending_result(interaction.guild.id, game_key)
            if pending is not None:
                async with _RESULT_PUBLISH_LOCKS.setdefault((interaction.guild.id, pending.id), asyncio.Lock()):
                    pending = await get_pending_result(interaction.guild.id, game_key)
                    if pending is None:
                        return await interaction.followup.send("✅ This game's result has already been published.", ephemeral=True)
                    channel, message = await self._post_result(interaction.guild, pending)
                    if message is not None:
                        return await interaction.followup.send(
                            f"✅ Saved result for {GAMES[game_key].label} was published in {channel.mention}. No additional XP was awarded.", ephemeral=True,
                        )
                    return await interaction.followup.send(
                        "⚠️ This game is completed and its XP is saved, but the result could not be published. Check the configured results channel and try again.", ephemeral=True,
                    )
            return await interaction.followup.send("⚠️ There is no active game to submit.", ephemeral=True)
        try:
            members = await self._staff_host_options(interaction.guild, interaction.user.id)
        except ValueError as exc:
            return await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
        if not members:
            return await interaction.followup.send("No Staff or High Staff members are available to host.", ephemeral=True)
        await interaction.followup.send(
            f"Select the host for the active {GAMES[game_key].label} game (ID `{active.id}`), then enter its winner.",
            view=StaffGameHostPicker(self, game_key, "submit", members, session_id=active.id),
            ephemeral=True,
        )

    async def panel_view(self, interaction: discord.Interaction, game_key: str):
        if not await self._deny_or_continue(interaction, game_key):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        active = await get_active_game(interaction.guild.id, game_key)
        if active is None:
            return await interaction.followup.send(f"There is no active {GAMES[game_key].label} game.", ephemeral=True)
        host = interaction.guild.get_member(active.host_id)
        host_text = host.mention if host else f"<@{active.host_id}>"
        started = _aware(active.started_at)
        embed = discord.Embed(title=f"{GAMES[game_key].emoji} Active {GAMES[game_key].label} Game", color=GAMES[game_key].color)
        embed.add_field(name="Game ID", value=str(active.id), inline=True)
        embed.add_field(name="Host", value=host_text, inline=True)
        embed.add_field(name="Started", value=f"<t:{int(started.timestamp())}:F>", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def panel_cancel(self, interaction: discord.Interaction, game_key: str):
        if not await self._deny_or_continue(interaction, game_key):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            game = await cancel_game(
                guild_id=interaction.guild.id, game_type=game_key,
                cancelled_by=interaction.user.id,
            )
        except ValueError as exc:
            return await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
        host = interaction.guild.get_member(game.host_id)
        await send_log(interaction.guild, LogEvent(
            LogType.XP, "❌ STAFF GAME CANCELLED", user=host, staff=interaction.user,
            fields=[("Game", GAMES[game_key].label, True), ("Game ID", str(game.id), True)],
            ids={"Guild ID": interaction.guild.id, "Game ID": game.id, "Cancelled By": interaction.user.id},
        ))
        await interaction.followup.send(f"❌ {GAMES[game_key].label} game `{game.id}` cancelled. No Staff XP was awarded.", ephemeral=True)

    async def selected_game_host(
        self, interaction: discord.Interaction, game_key: str, action: str,
        host: discord.Member, session_id: int | None,
    ):
        if not await self._deny_or_continue(interaction, game_key):
            return
        if not await self._is_game_staff(host):
            return await interaction.response.send_message("❌ Only authorized Staff or High Staff can host a game.", ephemeral=True)
        if action == "start":
            await interaction.response.defer(ephemeral=True, thinking=True)
            live_host = interaction.guild.get_member(host.id)
            if live_host is None:
                try:
                    live_host = await interaction.guild.fetch_member(host.id)
                except discord.HTTPException:
                    return await interaction.followup.send("That Staff member is no longer in this server.", ephemeral=True)
            if not await self._is_game_staff(live_host):
                return await interaction.followup.send("❌ Only authorized Staff or High Staff can host a game.", ephemeral=True)
            host = live_host
            definition = GAMES[game_key]
            try:
                game = await start_game(
                    guild_id=interaction.guild.id, game_type=game_key,
                    host_id=host.id, created_by=interaction.user.id,
                    panel_channel_id=interaction.channel_id,
                )
            except ValueError as exc:
                return await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
            await send_log(interaction.guild, LogEvent(
                LogType.XP, "🎮 STAFF GAME STARTED", user=host, staff=interaction.user,
                fields=[("Game", definition.label, True), ("Game ID", str(game.id), True)],
                ids={"Guild ID": interaction.guild.id, "Game ID": game.id},
            ))
            return await interaction.followup.send(
                f"🎮 {definition.label} game `{game.id}` started. Host: {host.mention}.", ephemeral=True,
            )
        if action == "submit" and session_id is not None:
            await interaction.response.send_modal(StaffGameResultModal(self, game_key, session_id, host.id))
            return
        await interaction.response.send_message("That Game Panel action is invalid.", ephemeral=True)

    async def submit_result(
        self, interaction: discord.Interaction, game_key: str, session_id: int,
        host_id: int, winner: str,
    ):
        if not await self._deny_or_continue(interaction, game_key):
            return
        if not winner:
            return await interaction.response.send_message("Enter a winner, team, or side.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        host = interaction.guild.get_member(host_id)
        if host is None:
            try:
                host = await interaction.guild.fetch_member(host_id)
            except discord.HTTPException:
                return await interaction.followup.send("The selected host is no longer in this server.", ephemeral=True)
        if not await self._is_game_staff(host):
            return await interaction.followup.send("❌ Only authorized Staff or High Staff can host a game.", ephemeral=True)
        definition = GAMES[game_key]
        results_channel_id = await config_service.get_setting(interaction.guild.id, definition.results_channel_key)
        results_channel = interaction.guild.get_channel(results_channel_id) if results_channel_id else None
        if not isinstance(results_channel, discord.TextChannel):
            return await interaction.followup.send(
                f"Configure `{definition.results_channel_key}` as a text channel before submitting this result.", ephemeral=True,
            )
        panel_channel_id = await config_service.get_setting(interaction.guild.id, definition.panel_channel_key)
        if results_channel.id == panel_channel_id:
            return await interaction.followup.send("The results channel must be different from the Staff panel channel.", ephemeral=True)
        if not results_channel.permissions_for(interaction.guild.default_role).view_channel:
            return await interaction.followup.send(
                "The results channel must be visible to @everyone so members can read the published result.", ephemeral=True,
            )
        if not results_channel.permissions_for(interaction.guild.me).send_messages or not results_channel.permissions_for(interaction.guild.me).embed_links:
            return await interaction.followup.send("I need Send Messages and Embed Links in the results channel.", ephemeral=True)
        xp_amount = int(await config_service.get_setting(interaction.guild.id, definition.xp_key) or 0)
        if xp_amount < 0:
            return await interaction.followup.send("The configured Game Host XP is invalid.", ephemeral=True)
        try:
            game = await finish_game(
                guild_id=interaction.guild.id, game_type=game_key,
                session_id=session_id, host=host, winner=winner, xp_amount=xp_amount,
            )
        except ValueError as exc:
            return await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
        except Exception:
            logger.exception("Could not complete Staff game %s in guild %s.", session_id, interaction.guild.id)
            return await interaction.followup.send("⚠️ The result could not be saved; no result was published.", ephemeral=True)

        embed, duration_seconds = self._result_embed(game, host)
        try:
            result_message = await results_channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            await save_result_message(game.id, interaction.guild.id, result_message.id)
        except Exception:
            logger.exception("Game result %s was saved but could not be posted to channel %s.", game.id, results_channel.id)
            result_message = None

        started = _aware(game.started_at)
        ended = _aware(game.ended_at)
        await send_log(interaction.guild, LogEvent(
            LogType.XP, "🏆 STAFF GAME COMPLETED", user=host, staff=interaction.user,
            reason=f"Winner: {winner}", xp_change=xp_amount,
            fields=[
                ("Game", definition.label, True), ("Winner", winner, True),
                ("Started", started.strftime("%Y-%m-%d %H:%M UTC"), True),
                ("Ended", ended.strftime("%Y-%m-%d %H:%M UTC"), True),
                ("Duration", _duration(duration_seconds), True),
                ("Staff XP", f"+{xp_amount:,}", True),
            ], ids={"Guild ID": interaction.guild.id, "Game ID": game.id, "Host ID": host.id},
        ))
        if result_message is None:
            return await interaction.followup.send(
                f"✅ Result saved and {xp_amount:,} Staff XP recorded for the host, but I could not post it in {results_channel.mention}.",
                ephemeral=True,
            )
        await interaction.followup.send(
            f"✅ {definition.label} result saved and published in {results_channel.mention}. "
            f"The host received {xp_amount:,} Staff XP.", ephemeral=True,
        )

    async def _post_result(self, guild: discord.Guild, game):
        definition = GAMES[game.game_type]
        results_channel_id = await config_service.get_setting(guild.id, definition.results_channel_key)
        channel = guild.get_channel(results_channel_id) if results_channel_id else None
        if not isinstance(channel, discord.TextChannel):
            return None, None
        panel_channel_id = await config_service.get_setting(guild.id, definition.panel_channel_key)
        if channel.id == panel_channel_id:
            return None, None
        if not channel.permissions_for(guild.default_role).view_channel:
            return None, None
        permissions = channel.permissions_for(guild.me)
        if not permissions.send_messages or not permissions.embed_links:
            return None, None
        host = guild.get_member(game.host_id)
        if host is None:
            try:
                host = await guild.fetch_member(game.host_id)
            except discord.HTTPException:
                host = discord.Object(id=game.host_id)
        embed, _ = self._result_embed(game, host)
        try:
            message = await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            await save_result_message(game.id, guild.id, message.id)
            return channel, message
        except Exception:
            logger.exception("Could not publish completed game result %s in guild %s.", game.id, guild.id)
            return channel, None

    def _result_embed(self, game, host: discord.Member):
        definition = GAMES[game.game_type]
        started = _aware(game.started_at)
        ended = _aware(game.ended_at)
        seconds = max(0, int((ended - started).total_seconds()))
        embed = discord.Embed(
            title=f"{definition.emoji} {definition.label.upper()} — RESULT",
            color=definition.color, timestamp=ended,
        )
        embed.add_field(name="🎮 Host", value=host.mention, inline=True)
        embed.add_field(
            name="🕐 Time",
            value=f"<t:{int(started.timestamp())}:f> – <t:{int(ended.timestamp())}:t>", inline=False,
        )
        embed.add_field(name="⏱ Duration", value=_duration(seconds), inline=True)
        embed.add_field(name="🏆 Winner", value=game.winner or "Not recorded", inline=True)
        return embed, seconds


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StaffGamesCog(bot))
