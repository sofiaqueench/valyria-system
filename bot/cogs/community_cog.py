"""Phase 18 guild-scoped community tools."""
from __future__ import annotations

import json
import asyncio
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from bot.core.logger import logger
from bot.core.permissions import StaffLevel, has_level, requires_level
from bot.database.engine import async_session
from bot.database.models.community import CommunitySequence, Confession, Poll, PollVote, ServerStat, Suggestion
from bot.database.models.guild import Guild
from bot.services import config_service
from bot.services.logging_service import LogEvent, LogType, send_log

STAT_NAMES = {"all_members": "👥・All Members", "bots": "🤖・Bots", "humans": "👤・Humans",
              "online": "🟢・Online", "voice": "🔊・In Voice"}
STAT_CHOICES = [app_commands.Choice(name=k.replace("_", " ").title(), value=k) for k in STAT_NAMES]


def utc_datetime(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


async def next_number(guild_id: int, kind: str) -> int:
    await config_service.get_or_create_guild(guild_id)
    async with async_session() as session:
        async with session.begin():
            stmt = sqlite_insert(CommunitySequence).values(guild_id=guild_id, kind=kind, last_number=1)
            stmt = stmt.on_conflict_do_update(index_elements=["guild_id", "kind"],
                set_={"last_number": CommunitySequence.last_number + 1}).returning(CommunitySequence.last_number)
            return int(await session.scalar(stmt))


async def secure_review_channel(guild: discord.Guild, channel: discord.TextChannel) -> bool:
    """Apply an allow-list view policy to the configured staff review channel."""
    staff_keys = ("role_staff", "role_support", "role_trial_moderator", "role_moderator",
                  "role_head_moderator", "role_admin", "role_owner", "role_ticket_staff",
                  "role_ticket_manager", "role_application_reviewer")
    staff_ids = {int(value) for key in staff_keys if (value := await config_service.get_setting(guild.id, key))}
    allow = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    overwrites = dict(channel.overwrites)
    everyone = guild.default_role
    overwrites[everyone] = discord.PermissionOverwrite(view_channel=False)
    bot_member = guild.me
    if bot_member:
        overwrites[bot_member] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
            read_message_history=True, manage_messages=True)
    for role in guild.roles:
        if role.is_default() or role.id in staff_ids or role.permissions.administrator:
            if role.id in staff_ids:
                overwrites[role] = allow
            continue
        if channel.permissions_for(role).view_channel:
            overwrites[role] = discord.PermissionOverwrite(view_channel=False)
    for target, current in channel.overwrites.items():
        if isinstance(target, discord.Member) and target != bot_member and current.view_channel is True:
            overwrites[target] = discord.PermissionOverwrite(view_channel=False)
    if len(overwrites) > 100:
        logger.error("Cannot secure suggestion review channel %s: Discord overwrite limit exceeded", channel.id)
        return False
    try:
        await channel.edit(overwrites=overwrites, reason="Restrict suggestion reviews to configured staff roles")
    except (discord.Forbidden, discord.HTTPException):
        logger.exception("Could not secure suggestion review channel %s", channel.id)
        return False
    return True


def poll_embed(poll: Poll, counts: list[int] | None = None, closed: bool = False) -> discord.Embed:
    options = json.loads(poll.options_json)
    labels = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    lines = [f"{labels[i]} {discord.utils.escape_mentions(option)}" + (f" — **{counts[i]}** vote(s)" if counts else "") for i, option in enumerate(options)]
    title = f"📊 COMMUNITY POLL · #{poll.poll_number:04d}"
    embed = discord.Embed(title=title, description=f"**{discord.utils.escape_mentions(poll.question)}**\n\n" + "\n".join(lines), color=discord.Color.blurple())
    embed.set_footer(text=("Closed" if closed else "Multiple choices allowed" if poll.multiple else "Choose one option") + f" · Ends <t:{int(poll.ends_at.replace(tzinfo=timezone.utc).timestamp())}:R>")
    return embed


class SuggestionModal(discord.ui.Modal, title="Submit a suggestion"):
    title_field = discord.ui.TextInput(label="Suggestion Title", max_length=200)
    description = discord.ui.TextInput(label="Describe your idea", style=discord.TextStyle.paragraph, required=True, max_length=3500)

    async def on_submit(self, interaction: discord.Interaction):
        cog: CommunityCog = interaction.client.get_cog("CommunityCog")
        await cog.create_suggestion(interaction, str(self.title_field), str(self.description))


class RejectModal(discord.ui.Modal, title="Reject suggestion"):
    reason = discord.ui.TextInput(label="Why was this suggestion rejected?", style=discord.TextStyle.paragraph, required=True, max_length=1500)

    def __init__(self, suggestion_id: int, required: bool = True):
        super().__init__(timeout=300)
        self.suggestion_id = suggestion_id
        self.reason.required = required

    async def on_submit(self, interaction: discord.Interaction):
        cog: CommunityCog = interaction.client.get_cog("CommunityCog")
        await cog.decide_suggestion(interaction, self.suggestion_id, "REJECTED", str(self.reason))


class ConfessionModal(discord.ui.Modal, title="Anonymous confession"):
    content = discord.ui.TextInput(label="Write your confession", style=discord.TextStyle.paragraph, required=True, max_length=3000)

    async def on_submit(self, interaction: discord.Interaction):
        cog: CommunityCog = interaction.client.get_cog("CommunityCog")
        await cog.create_confession(interaction, str(self.content))


class SuggestionPanelView(discord.ui.View):
    def __init__(self, label="Submit Suggestion", emoji="💡"):
        super().__init__(timeout=None)
        button = self.children[0]
        button.label = str(label)[:80] or "Submit Suggestion"
        try:
            button.emoji = emoji or "💡"
        except (TypeError, ValueError):
            button.emoji = "💡"

    @discord.ui.button(label="Submit Suggestion", emoji="💡", style=discord.ButtonStyle.primary, custom_id="community:suggestion:submit")
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SuggestionModal())


class SuggestionReviewView(discord.ui.View):
    def __init__(self, suggestion_id: int):
        super().__init__(timeout=None)
        self.suggestion_id = suggestion_id
        for child, action in zip(self.children, ("APPROVED", "REJECTED", "UNDER_REVIEW")):
            child.custom_id = f"community:suggestion:{action.lower()}:{suggestion_id}"

    @discord.ui.button(label="Approve", emoji="✅", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: CommunityCog = interaction.client.get_cog("CommunityCog")
        await cog.decide_suggestion(interaction, self.suggestion_id, "APPROVED")

    @discord.ui.button(label="Reject", emoji="❌", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: CommunityCog = interaction.client.get_cog("CommunityCog")
        if not await cog.is_suggestion_staff(interaction):
            return await interaction.response.send_message("You are not authorized to review suggestions.", ephemeral=True)
        required = await config_service.get_setting(interaction.guild_id, "suggestion_reject_reason_required")
        await interaction.response.send_modal(RejectModal(self.suggestion_id, bool(required)))

    @discord.ui.button(label="Under Review", emoji="🟡", style=discord.ButtonStyle.secondary)
    async def review(self, interaction: discord.Interaction, button: discord.ui.Button):
        cog: CommunityCog = interaction.client.get_cog("CommunityCog")
        await cog.decide_suggestion(interaction, self.suggestion_id, "UNDER_REVIEW")


class PollVoteView(discord.ui.View):
    def __init__(self, poll_id: int, options: list[str], multiple: bool):
        super().__init__(timeout=None)
        self.poll_id = poll_id
        select = discord.ui.Select(placeholder="Vote in this poll", min_values=1, max_values=len(options) if multiple else 1,
                                   options=[discord.SelectOption(label=o[:100], value=str(i)) for i, o in enumerate(options)],
                                   custom_id=f"community:poll:vote:{poll_id}")
        select.callback = self._vote
        self.add_item(select)

    async def _vote(self, interaction: discord.Interaction):
        cog: CommunityCog = interaction.client.get_cog("CommunityCog")
        await cog.cast_vote(interaction, self.poll_id, [int(v) for v in interaction.data.get("values", [])])


class CommunityCog(commands.Cog):
    stats = app_commands.Group(name="stats", description="Configure server statistic voice channels", guild_only=True)
    suggestion = app_commands.Group(name="suggestion", description="Suggestion panel configuration", guild_only=True)
    poll = app_commands.Group(name="poll", description="Community polls", guild_only=True)
    confession = app_commands.Group(name="confession", description="Configure anonymous confessions", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._stat_tasks: dict[int, asyncio.Task] = {}
        self._last_confession: dict[tuple[int, int], datetime] = {}
        self._confession_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._views_restored = False

    async def cog_load(self):
        self.bot.add_view(SuggestionPanelView())
        self.close_expired_polls.start()

    async def cog_unload(self):
        self.close_expired_polls.cancel()
        for task in self._stat_tasks.values():
            task.cancel()

    async def on_ready(self):
        if not self._views_restored:
            async with async_session() as session:
                suggestions = list((await session.scalars(select(Suggestion).where(Suggestion.status.in_(["PENDING", "UNDER_REVIEW"]), Suggestion.review_message_id.is_not(None)))).all())
                polls = list((await session.scalars(select(Poll).where(Poll.status == "OPEN", Poll.message_id.is_not(None)))).all())
            for row in suggestions:
                self.bot.add_view(SuggestionReviewView(row.id), message_id=row.review_message_id)
            for row in polls:
                self.bot.add_view(PollVoteView(row.id, json.loads(row.options_json), row.multiple), message_id=row.message_id)
            self._views_restored = True
        for guild in self.bot.guilds:
            await self.refresh_stats(guild)

    @tasks.loop(seconds=20)
    async def close_expired_polls(self):
        now = datetime.now(timezone.utc)
        async with async_session() as session:
            rows = list((await session.scalars(select(Poll).where(Poll.status == "OPEN", Poll.ends_at <= now))).all())
            ids = [r.id for r in rows]
            if ids:
                await session.execute(Poll.__table__.update().where(Poll.id.in_(ids), Poll.status == "OPEN").values(status="CLOSED"))
                await session.commit()
        for row in rows:
            await self.finish_poll(row)

    @close_expired_polls.before_loop
    async def before_poll_loop(self):
        await self.bot.wait_until_ready()

    @stats.command(name="setup", description="Create five server statistic voice channels")
    @requires_level(StaffLevel.ADMIN)
    async def stats_setup(self, interaction: discord.Interaction, category: discord.CategoryChannel):
        await interaction.response.defer(ephemeral=True, thinking=True)
        await config_service.get_or_create_guild(interaction.guild_id)
        created = []
        try:
            for kind, title in STAT_NAMES.items():
                channel = await interaction.guild.create_voice_channel(f"{title}: 0", category=category,
                    reason=f"Server stats configured by {interaction.user}")
                async with async_session() as session:
                    async with session.begin():
                        row = await session.scalar(select(ServerStat).where(ServerStat.guild_id == interaction.guild_id,
                            ServerStat.stat_type == kind))
                        if row:
                            row.channel_id, row.enabled = channel.id, True
                        else:
                            session.add(ServerStat(guild_id=interaction.guild_id, stat_type=kind, channel_id=channel.id))
                created.append(channel)
        except (discord.Forbidden, discord.HTTPException, SQLAlchemyError):
            logger.exception("Could not finish server stats setup in guild %s", interaction.guild_id)
            await interaction.followup.send(f"Setup stopped after creating {len(created)} channel(s). Configure or retry remaining stats with `/stats set`.", ephemeral=True)
            return
        await self.refresh_stats(interaction.guild)
        await send_log(interaction.guild, LogEvent(LogType.CHANNEL, "📊 Server stats channels set up", staff=interaction.user,
            fields=[("Category", category.name, True), ("Channels", str(len(created)), True)]))
        await interaction.followup.send(f"Created and configured {len(created)} statistic channels in {category.mention}.", ephemeral=True)

    @commands.Cog.listener()
    async def on_member_join(self, member):
        await self.debounce_stats(member.guild)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        await self.debounce_stats(member.guild)

    @commands.Cog.listener()
    async def on_presence_update(self, before, after):
        await self.debounce_stats(after.guild)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if before.channel != after.channel:
            await self.debounce_stats(member.guild)

    async def debounce_stats(self, guild):
        old = self._stat_tasks.get(guild.id)
        if old and not old.done():
            old.cancel()
        async def later():
            try:
                await asyncio.sleep(10)
                await self.refresh_stats(guild)
            except asyncio.CancelledError:
                pass
        self._stat_tasks[guild.id] = asyncio.create_task(later())

    async def refresh_stats(self, guild):
        async with async_session() as session:
            rows = list((await session.scalars(select(ServerStat).where(ServerStat.guild_id == guild.id, ServerStat.enabled.is_(True)))).all())
        if not rows:
            return
        members = guild.members
        values = {"all_members": guild.member_count or len(members), "bots": sum(m.bot for m in members),
                  "humans": sum(not m.bot for m in members), "online": sum(not m.bot and m.status != discord.Status.offline for m in members),
                  "voice": sum(len(v.members) for v in guild.voice_channels)}
        for row in rows:
            channel = guild.get_channel(row.channel_id)
            if not isinstance(channel, discord.VoiceChannel):
                logger.warning("Stats channel %s for %s is missing/invalid in guild %s", row.channel_id, row.stat_type, guild.id)
                async with async_session() as session:
                    await session.execute(delete(ServerStat).where(ServerStat.id == row.id)); await session.commit()
                await send_log(guild, LogEvent(LogType.CHANNEL, "⚠️ Server stats channel is missing", reason=f"{row.stat_type}: {row.channel_id}"))
                continue
            name = f"{STAT_NAMES[row.stat_type]}: {values[row.stat_type]}"[:100]
            if channel.name != name:
                try:
                    await channel.edit(name=name, reason="Refresh configured server statistics")
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception("Could not refresh server stats channel %s", channel.id)
                else:
                    async with async_session() as session:
                        await session.execute(ServerStat.__table__.update().where(ServerStat.id == row.id).values(last_name=name)); await session.commit()

    @stats.command(name="set", description="Set a voice channel for one statistic")
    @requires_level(StaffLevel.ADMIN)
    async def stats_set(self, interaction: discord.Interaction, type: str, channel: discord.VoiceChannel):
        if type not in STAT_NAMES:
            return await interaction.response.send_message("Unknown statistic type.", ephemeral=True)
        await config_service.get_or_create_guild(interaction.guild_id)
        async with async_session() as session:
            async with session.begin():
                row = await session.scalar(select(ServerStat).where(ServerStat.guild_id == interaction.guild_id, ServerStat.stat_type == type.value))
                if row:
                    row.channel_id, row.enabled = channel.id, True
                else:
                    session.add(ServerStat(guild_id=interaction.guild_id, stat_type=type.value, channel_id=channel.id))
        await self.refresh_stats(interaction.guild)
        await send_log(interaction.guild, LogEvent(LogType.CHANNEL, "📊 Server stats configured", staff=interaction.user, fields=[("Type", type, True), ("Channel", channel.mention, True)]))
        await interaction.response.send_message(f"Configured {type.replace('_', ' ').title()} stats in {channel.mention}.", ephemeral=True)

    @stats_set.autocomplete("type")
    async def stats_type_autocomplete(self, interaction, current):
        return [c for c in STAT_CHOICES if current.lower() in c.name.lower()]

    @stats.command(name="enable", description="Enable a configured statistic")
    @requires_level(StaffLevel.ADMIN)
    async def stats_enable(self, interaction: discord.Interaction, type: str):
        await self._stats_toggle(interaction, type, True)

    @stats_enable.autocomplete("type")
    async def stats_enable_types(self, interaction, current):
        return [c for c in STAT_CHOICES if current.lower() in c.name.lower()]

    @stats.command(name="disable", description="Disable a configured statistic")
    @requires_level(StaffLevel.ADMIN)
    async def stats_disable(self, interaction: discord.Interaction, type: str):
        await self._stats_toggle(interaction, type, False)

    @stats_disable.autocomplete("type")
    async def stats_disable_types(self, interaction, current):
        return [c for c in STAT_CHOICES if current.lower() in c.name.lower()]

    @stats.command(name="refresh", description="Refresh all configured statistics")
    @requires_level(StaffLevel.ADMIN)
    async def stats_refresh(self, interaction: discord.Interaction):
        await self.refresh_stats(interaction.guild)
        await interaction.response.send_message("Statistics refreshed.", ephemeral=True)

    @stats.command(name="status", description="Show configured statistics")
    @requires_level(StaffLevel.ADMIN)
    async def stats_status(self, interaction: discord.Interaction):
        async with async_session() as session:
            rows = list((await session.scalars(select(ServerStat).where(ServerStat.guild_id == interaction.guild_id))).all())
        text = "\n".join(f"{r.stat_type}: <#{r.channel_id}> · {'enabled' if r.enabled else 'disabled'}" for r in rows) or "No stats configured."
        await interaction.response.send_message(text, ephemeral=True)

    async def _stats_toggle(self, interaction, key, enabled):
        if key not in STAT_NAMES:
            return await interaction.response.send_message("Unknown statistic type.", ephemeral=True)
        async with async_session() as session:
            row = await session.scalar(select(ServerStat).where(ServerStat.guild_id == interaction.guild_id, ServerStat.stat_type == key))
            if row:
                row.enabled = enabled; await session.commit()
        await interaction.response.send_message("Updated." if row else "Set a channel first with `/stats set`.", ephemeral=True)
        if row:
            await send_log(interaction.guild, LogEvent(LogType.CHANNEL, "📊 Server stats setting changed", staff=interaction.user,
                fields=[("Type", key, True), ("Enabled", str(enabled), True)]))
        if row and enabled:
            await self.refresh_stats(interaction.guild)

    @suggestion.command(name="panel", description="Send the suggestion panel")
    @requires_level(StaffLevel.ADMIN)
    async def suggestion_panel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await config_service.set_setting(interaction.guild_id, "suggestion_panel_channel", channel.id, interaction.user.id)
        title = await config_service.get_setting(interaction.guild_id, "suggestion_panel_title")
        text = await config_service.get_setting(interaction.guild_id, "suggestion_panel_text")
        label = await config_service.get_setting(interaction.guild_id, "suggestion_button_label")
        emoji = await config_service.get_setting(interaction.guild_id, "suggestion_button_emoji")
        embed = discord.Embed(title=str(title), description=str(text), color=discord.Color.gold())
        banner = await config_service.get_setting(interaction.guild_id, "suggestion_banner")
        if banner:
            embed.set_image(url=str(banner))
        await channel.send(embed=embed, view=SuggestionPanelView(label, emoji))
        await interaction.response.send_message(f"Suggestion panel sent to {channel.mention}.", ephemeral=True)

    @suggestion.command(name="set-channel", description="Configure suggestion workflow channels")
    @requires_level(StaffLevel.ADMIN)
    async def suggestion_channel(self, interaction: discord.Interaction, type: str, channel: discord.TextChannel):
        keys = {"panel": "suggestion_panel_channel", "review": "suggestion_review_channel", "approved": "suggestion_approved_channel"}
        if type not in keys:
            return await interaction.response.send_message("Unknown channel type.", ephemeral=True)
        if type == "review" and not await secure_review_channel(interaction.guild, channel):
            return await interaction.response.send_message("I could not secure this review channel. Check my Manage Channels permission and channel overwrite capacity.", ephemeral=True)
        key = keys[type]
        await config_service.set_setting(interaction.guild_id, key, channel.id, interaction.user.id)
        await send_log(interaction.guild, LogEvent(LogType.CHANNEL, "💡 Suggestion channel configured", staff=interaction.user, fields=[("Type", type, True), ("Channel", channel.mention, True)]))
        await interaction.response.send_message(f"Configured {type.title()} channel: {channel.mention}.", ephemeral=True)

    @suggestion_channel.autocomplete("type")
    async def suggestion_channel_types(self, interaction, current):
        return [app_commands.Choice(name=n.title(), value=n) for n in ("panel", "review", "approved") if current.lower() in n]

    @suggestion.command(name="set-banner", description="Set or clear the suggestion panel banner URL")
    @requires_level(StaffLevel.ADMIN)
    async def suggestion_banner(self, interaction: discord.Interaction, url: str | None = None):
        if url and not url.startswith(("https://", "http://")):
            return await interaction.response.send_message("Enter a valid HTTP(S) image URL.", ephemeral=True)
        await config_service.set_setting(interaction.guild_id, "suggestion_banner", url or "", interaction.user.id)
        await interaction.response.send_message("Suggestion banner updated.", ephemeral=True)

    @suggestion.command(name="set-text", description="Customize suggestion panel text and button")
    @requires_level(StaffLevel.ADMIN)
    async def suggestion_text(self, interaction: discord.Interaction, type: str, value: app_commands.Range[str, 1, 1500]):
        keys = {"title": "suggestion_panel_title", "description": "suggestion_panel_text",
                "button_label": "suggestion_button_label", "button_emoji": "suggestion_button_emoji"}
        if type not in keys or len(value) > (80 if type in {"title", "button_label"} else 1500):
            return await interaction.response.send_message("Type must be title, description, button_label, or button_emoji; keep text within Discord limits.", ephemeral=True)
        await config_service.set_setting(interaction.guild_id, keys[type], value, interaction.user.id)
        await interaction.response.send_message("Suggestion panel text saved. Run `/suggestion panel` to publish the updated panel.", ephemeral=True)

    @suggestion_text.autocomplete("type")
    async def suggestion_text_types(self, interaction, current):
        values = ("title", "description", "button_label", "button_emoji")
        return [app_commands.Choice(name=v.replace("_", " ").title(), value=v) for v in values if current.lower() in v]

    @confession.command(name="set-channel", description="Set the anonymous confession channel")
    @requires_level(StaffLevel.ADMIN)
    async def confession_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await config_service.set_setting(interaction.guild_id, "confession_channel", channel.id, interaction.user.id)
        await send_log(interaction.guild, LogEvent(LogType.CHANNEL, "🤫 Confession channel configured", staff=interaction.user, fields=[("Channel", channel.mention, True)]))
        await interaction.response.send_message(f"Confessions will be published in {channel.mention}.", ephemeral=True)

    async def create_suggestion(self, interaction, title, description):
        title, description = title.strip(), description.strip()
        if not title or not description:
            return await interaction.response.send_message("Suggestion title and description cannot be blank.", ephemeral=True)
        review_id = await config_service.get_setting(interaction.guild_id, "suggestion_review_channel")
        channel = interaction.guild.get_channel(review_id) if review_id else None
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("Suggestions are not configured yet.", ephemeral=True)
        number = await next_number(interaction.guild_id, "suggestion")
        row = Suggestion(guild_id=interaction.guild_id, suggestion_number=number, author_id=interaction.user.id, title=title, description=description, review_channel_id=channel.id)
        async with async_session() as session:
            async with session.begin():
                session.add(row); await session.flush(); row_id = row.id
        embed = discord.Embed(title=f"💡 SUGGESTION #{number:04d}", description=f"**Title:** {discord.utils.escape_markdown(title)}\n\n**Suggestion:**\n{discord.utils.escape_mentions(description)}\n\n**Submitted by:** {interaction.user.mention}\n**Status:** 🟡 Pending Review", color=discord.Color.gold())
        try:
            message = await channel.send(embed=embed, view=SuggestionReviewView(row_id), allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False))
        except (discord.Forbidden, discord.HTTPException):
            logger.exception("Could not send suggestion to review channel %s", channel.id)
            async with async_session() as session:
                await session.execute(Suggestion.__table__.update().where(Suggestion.id == row_id).values(status="FAILED")); await session.commit()
            return await interaction.response.send_message("The staff review channel is unavailable; please try again later.", ephemeral=True)
        async with async_session() as session:
            await session.execute(Suggestion.__table__.update().where(Suggestion.id == row_id).values(review_message_id=message.id)); await session.commit()
        await send_log(interaction.guild, LogEvent(LogType.MESSAGE, "💡 Suggestion Created", user=interaction.user, ids={"Suggestion": number}))
        await interaction.response.send_message(f"Your suggestion #{number:04d} was sent to staff for review.", ephemeral=True)

    async def is_suggestion_staff(self, interaction):
        if not isinstance(interaction.user, discord.Member):
            return False
        manager_role = await config_service.get_setting(interaction.guild_id, "role_ticket_manager")
        if manager_role and any(r.id == manager_role for r in interaction.user.roles):
            return True
        return await has_level(interaction.user, StaffLevel.STAFF)

    async def decide_suggestion(self, interaction, suggestion_id, status, reason=None):
        if not await self.is_suggestion_staff(interaction):
            return await interaction.response.send_message("You are not authorized to review suggestions.", ephemeral=True)
        async with async_session() as session:
            async with session.begin():
                row = await session.scalar(select(Suggestion).where(Suggestion.id == suggestion_id, Suggestion.guild_id == interaction.guild_id).with_for_update())
                if not row or row.status not in ("PENDING", "UNDER_REVIEW") or row.status == status:
                    return await interaction.response.send_message("This suggestion has already been decided or is unavailable.", ephemeral=True)
                row.status, row.reviewed_by, row.review_reason, row.reviewed_at = status, interaction.user.id, reason, datetime.now(timezone.utc)
                data = (row.suggestion_number, row.author_id, row.title, row.description, row.review_channel_id, row.review_message_id)
        number, author_id, title, description, review_channel_id, review_message_id = data
        channel = interaction.guild.get_channel(review_channel_id)
        if channel and review_message_id:
            try:
                message = await channel.fetch_message(review_message_id)
                label = {"APPROVED": "✅ Approved", "REJECTED": "❌ Rejected", "UNDER_REVIEW": "🟡 Under Review"}[status]
                embed = discord.Embed(title=f"💡 SUGGESTION #{number:04d}", description=f"**Title:** {discord.utils.escape_markdown(title)}\n\n**Suggestion:**\n{discord.utils.escape_mentions(description)}\n\n**Submitted by:** <@{author_id}>\n**Status:** {label}" + (f"\n**Reason:** {discord.utils.escape_mentions(reason)}" if reason else ""), color=discord.Color.green() if status == "APPROVED" else discord.Color.red() if status == "REJECTED" else discord.Color.gold())
                await message.edit(embed=embed, view=SuggestionReviewView(suggestion_id) if status == "UNDER_REVIEW" else None, allowed_mentions=discord.AllowedMentions.none())
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.exception("Could not update suggestion review message %s", review_message_id)
        if status == "APPROVED":
            approved_id = await config_service.get_setting(interaction.guild_id, "suggestion_approved_channel")
            approved = interaction.guild.get_channel(approved_id) if approved_id else None
            if isinstance(approved, discord.TextChannel):
                try:
                    pub = await approved.send(embed=discord.Embed(title=f"💡 APPROVED SUGGESTION #{number:04d}", description=f"“{discord.utils.escape_mentions(title)}”\n\n{discord.utils.escape_mentions(description)}\n\nSuggested by: <@{author_id}>\nStatus: ✅ Approved", color=discord.Color.green()), allowed_mentions=discord.AllowedMentions(users=False, roles=False, everyone=False))
                    async with async_session() as session:
                        await session.execute(Suggestion.__table__.update().where(Suggestion.id == suggestion_id).values(approved_channel_id=approved.id, approved_message_id=pub.id)); await session.commit()
                except (discord.Forbidden, discord.HTTPException):
                    logger.exception("Could not publish approved suggestion %s", number)
        await send_log(interaction.guild, LogEvent(LogType.MESSAGE, f"Suggestion {status.title()}", staff=interaction.user, reason=reason, ids={"Suggestion": number}))
        if not interaction.response.is_done():
            await interaction.response.send_message(f"Suggestion #{number:04d}: {status.replace('_', ' ').title()}.", ephemeral=True)
        else:
            await interaction.followup.send(f"Suggestion #{number:04d}: {status.replace('_', ' ').title()}.", ephemeral=True)

    @poll.command(name="create", description="Create a poll with 2–10 options")
    @requires_level(StaffLevel.STAFF)
    async def create_poll(self, interaction: discord.Interaction, question: app_commands.Range[str, 1, 300], options: app_commands.Range[str, 3, 1000], duration_hours: int | None = None, channel: discord.TextChannel | None = None, multiple_choices: bool | None = None):
        if duration_hours is None:
            duration_hours = int(await config_service.get_setting(interaction.guild_id, "poll_default_duration_hours") or 24)
        if not 1 <= duration_hours <= 720:
            return await interaction.response.send_message("Poll duration must be between 1 and 720 hours.", ephemeral=True)
        if channel is None:
            channel_id = await config_service.get_setting(interaction.guild_id, "poll_default_channel")
            channel = interaction.guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("Choose a poll channel or configure one with `/poll defaults`.", ephemeral=True)
        if multiple_choices is None:
            multiple_choices = bool(await config_service.get_setting(interaction.guild_id, "poll_default_multiple_choices"))
        choices = [x.strip() for x in options.split("|") if x.strip()]
        if not 2 <= len(choices) <= 10 or any(len(x) > 100 for x in choices):
            return await interaction.response.send_message("Provide 2–10 options separated by `|`; each option must be at most 100 characters.", ephemeral=True)
        number = await next_number(interaction.guild_id, "poll")
        ends = datetime.now(timezone.utc) + timedelta(hours=duration_hours)
        row = Poll(guild_id=interaction.guild_id, poll_number=number, channel_id=channel.id, creator_id=interaction.user.id, question=question, options_json=json.dumps(choices, ensure_ascii=False), multiple=multiple_choices, ends_at=ends)
        async with async_session() as session:
            async with session.begin():
                session.add(row); await session.flush(); poll_id = row.id
        try:
            msg = await channel.send(embed=poll_embed(row), view=PollVoteView(poll_id, choices, multiple_choices),
                                     allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException):
            async with async_session() as session:
                await session.execute(Poll.__table__.update().where(Poll.id == poll_id).values(status="FAILED")); await session.commit()
            return await interaction.response.send_message("I could not post the poll in that channel.", ephemeral=True)
        async with async_session() as session:
            await session.execute(Poll.__table__.update().where(Poll.id == poll_id).values(message_id=msg.id)); await session.commit()
        self.bot.add_view(PollVoteView(poll_id, choices, multiple_choices), message_id=msg.id)
        await send_log(interaction.guild, LogEvent(LogType.MESSAGE, "📊 Poll Created", staff=interaction.user, ids={"Poll": number}))
        await interaction.response.send_message(f"Poll #{number:04d} posted in {channel.mention}.", ephemeral=True)

    @poll.command(name="defaults", description="Configure default channel and behavior for new polls")
    @requires_level(StaffLevel.ADMIN)
    async def poll_defaults(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None,
                            duration_hours: int | None = None, multiple_choices: bool | None = None):
        if duration_hours is not None and not 1 <= duration_hours <= 720:
            return await interaction.response.send_message("Default duration must be between 1 and 720 hours.", ephemeral=True)
        if channel is None and duration_hours is None and multiple_choices is None:
            current_channel = await config_service.get_setting(interaction.guild_id, "poll_default_channel")
            current_duration = await config_service.get_setting(interaction.guild_id, "poll_default_duration_hours")
            current_multiple = await config_service.get_setting(interaction.guild_id, "poll_default_multiple_choices")
            return await interaction.response.send_message(f"Default channel: {f'<#{current_channel}>' if current_channel else 'not set'}\nDuration: {current_duration}h\nMultiple choices: {'yes' if current_multiple else 'no'}", ephemeral=True)
        if channel is not None:
            await config_service.set_setting(interaction.guild_id, "poll_default_channel", channel.id, interaction.user.id)
        if duration_hours is not None:
            await config_service.set_setting(interaction.guild_id, "poll_default_duration_hours", duration_hours, interaction.user.id)
        if multiple_choices is not None:
            await config_service.set_setting(interaction.guild_id, "poll_default_multiple_choices", multiple_choices, interaction.user.id)
        await send_log(interaction.guild, LogEvent(LogType.MESSAGE, "📊 Poll defaults configured", staff=interaction.user))
        await interaction.response.send_message("Poll defaults saved for this server.", ephemeral=True)

    async def cast_vote(self, interaction, poll_id, indexes):
        async with async_session() as session:
            async with session.begin():
                poll = await session.scalar(select(Poll).where(Poll.id == poll_id, Poll.guild_id == interaction.guild_id).with_for_update())
                if not poll or poll.status != "OPEN" or utc_datetime(poll.ends_at) <= datetime.now(timezone.utc):
                    return await interaction.response.send_message("This poll is closed.", ephemeral=True)
                opts = json.loads(poll.options_json)
                if not indexes or any(i < 0 or i >= len(opts) for i in indexes) or (not poll.multiple and len(indexes) != 1):
                    return await interaction.response.send_message("Invalid vote selection.", ephemeral=True)
                await session.execute(delete(PollVote).where(PollVote.poll_id == poll_id, PollVote.user_id == interaction.user.id))
                session.add_all([PollVote(poll_id=poll_id, user_id=interaction.user.id, option_index=i) for i in indexes])
        await interaction.response.send_message("Your vote has been recorded.", ephemeral=True)

    async def finish_poll(self, poll):
        async with async_session() as session:
            counts = [await session.scalar(select(func.count()).select_from(PollVote).where(PollVote.poll_id == poll.id, PollVote.option_index == i)) for i, _ in enumerate(json.loads(poll.options_json))]
        guild = self.bot.get_guild(poll.guild_id)
        channel = guild.get_channel(poll.channel_id) if guild else None
        if channel and poll.message_id:
            try:
                msg = await channel.fetch_message(poll.message_id)
                await msg.edit(embed=poll_embed(poll, counts, True), view=None)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.exception("Could not close poll %s", poll.id)
        if guild:
            await send_log(guild, LogEvent(LogType.MESSAGE, "📊 Poll Closed", ids={"Poll": poll.poll_number}))

    @app_commands.command(name="confess", description="Submit an anonymous confession")
    @app_commands.guild_only()
    async def confess(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ConfessionModal())

    async def create_confession(self, interaction, content):
        content = content.strip()
        if not content:
            return await interaction.response.send_message("A confession cannot be blank.", ephemeral=True)
        key = (interaction.guild_id, interaction.user.id)
        lock = self._confession_locks.setdefault(key, asyncio.Lock())
        async with lock:
            await self._create_confession_locked(interaction, content)

    async def _create_confession_locked(self, interaction, content):
        key = (interaction.guild_id, interaction.user.id)
        now = datetime.now(timezone.utc)
        previous = self._last_confession.get(key)
        async with async_session() as session:
            db_previous = await session.scalar(select(Confession.created_at).where(Confession.guild_id == interaction.guild_id, Confession.author_id == interaction.user.id, Confession.status == "ACTIVE").order_by(Confession.created_at.desc()).limit(1))
        if (previous and now - previous < timedelta(minutes=5)) or (db_previous and now - utc_datetime(db_previous) < timedelta(minutes=5)):
            return await interaction.response.send_message("Please wait before submitting another confession.", ephemeral=True)
        channel_id = await config_service.get_setting(interaction.guild_id, "confession_channel")
        channel = interaction.guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("Confessions are not configured in this server.", ephemeral=True)
        self._last_confession[key] = now
        number = await next_number(interaction.guild_id, "confession")
        row = Confession(guild_id=interaction.guild_id, confession_number=number, author_id=interaction.user.id, content=content, channel_id=channel.id)
        async with async_session() as session:
            async with session.begin():
                session.add(row); await session.flush(); row_id = row.id
        try:
            message = await channel.send(embed=discord.Embed(title=f"🤫 CONFESSION #{number:04d}", description=discord.utils.escape_mentions(content), color=discord.Color.dark_purple()), allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException):
            async with async_session() as session:
                await session.execute(Confession.__table__.update().where(Confession.id == row_id).values(status="FAILED")); await session.commit()
            return await interaction.response.send_message("I could not publish your confession. Please try later.", ephemeral=True)
        async with async_session() as session:
            await session.execute(Confession.__table__.update().where(Confession.id == row_id).values(message_id=message.id)); await session.commit()
        await send_log(interaction.guild, LogEvent(LogType.MESSAGE, "🤫 Confession Created", ids={"Confession": number}))
        await interaction.response.send_message("Your confession was submitted anonymously.", ephemeral=True)

    @app_commands.command(name="serverinfo", description="Show information about this server")
    @app_commands.guild_only()
    async def serverinfo(self, interaction: discord.Interaction):
        g = interaction.guild
        e = discord.Embed(title=g.name, color=discord.Color.blurple(), description=f"Owner: <@{g.owner_id}>\nCreated: <t:{int(g.created_at.timestamp())}:D>\nMembers: {g.member_count}\nRoles: {len(g.roles)}\nChannels: {len(g.channels)}")
        if g.icon: e.set_thumbnail(url=g.icon.url)
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="userinfo", description="Show information about a member")
    @app_commands.guild_only()
    async def userinfo(self, interaction: discord.Interaction, member: discord.Member | None = None):
        m = member or interaction.user
        e = discord.Embed(title=f"User: {m.display_name}", color=discord.Color.blurple(), description=f"Account: <t:{int(m.created_at.timestamp())}:D>\nJoined: <t:{int(m.joined_at.timestamp())}:D>\nRoles: {len(m.roles)-1}")
        e.set_thumbnail(url=m.display_avatar.url)
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="avatar", description="Show a member's avatar")
    @app_commands.guild_only()
    async def avatar(self, interaction: discord.Interaction, member: discord.Member | None = None):
        m = member or interaction.user
        await interaction.response.send_message(embed=discord.Embed(title=f"{m.display_name}'s avatar", color=discord.Color.blurple()).set_image(url=m.display_avatar.url))

    @app_commands.command(name="roleinfo", description="Show information about a role")
    @app_commands.guild_only()
    async def roleinfo(self, interaction: discord.Interaction, role: discord.Role):
        e = discord.Embed(title=role.name, color=role.color, description=f"Members: {len(role.members)}\nPosition: {role.position}\nMentionable: {role.mentionable}\nCreated: <t:{int(role.created_at.timestamp())}:D>")
        await interaction.response.send_message(embed=e)

    @app_commands.command(name="channelinfo", description="Show information about a channel")
    @app_commands.guild_only()
    async def channelinfo(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        c = channel or interaction.channel
        await interaction.response.send_message(embed=discord.Embed(title=c.name, color=discord.Color.blurple(), description=f"Type: {type(c).__name__}\nCreated: <t:{int(c.created_at.timestamp())}:D>\nCategory: {getattr(c.category, 'name', 'None')}"))

    @app_commands.command(name="botinfo", description="Show bot uptime and latency information")
    async def botinfo(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=discord.Embed(title="Bot Information", color=discord.Color.blurple(), description=f"Latency: {self.bot.latency * 1000:.0f} ms\nServers: {len(self.bot.guilds)}\nUsers: {len(self.bot.users)}"))


async def setup(bot: commands.Bot):
    await bot.add_cog(CommunityCog(bot))
