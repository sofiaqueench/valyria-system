"""Professional, multi-guild ticket panel and moderation controls."""
import asyncio
import io
import json
import re

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy.exc import SQLAlchemyError

from bot.core.logger import logger
from bot.core.permissions import StaffLevel, has_level, requires_level
from bot.database.engine import async_session
from bot.database.models.ticket import Ticket
from bot.services import config_service
from bot.services.logging_service import LogEvent, LogType, send_log
from bot.services.staff_xp_service import add_staff_xp
from bot.services.ticket_service import claim_ticket, close_ticket, create_ticket, find_open_ticket, get_ticket, reserve_ticket_number

TYPES = {
    "staff_apply": ("Staff Application", "staff", "Staff Application\nPlease provide the information required by the staff team."),
    "manager_apply": ("Manager Application", "manager", "Manager Application\nPlease provide the information required by the management team."),
    "assistance": ("Assistance", "help", "Assistance\nPlease explain the support you need."),
    "response": ("Réponse / Administration Contact", "response", "Réponse / Administration Contact\nPlease explain your request to the administration."),
}
TYPE_CHOICES = [app_commands.Choice(name=label, value=key) for key, (label, _, _) in TYPES.items()]
DEFAULT_PANEL_LABELS = {"staff_apply": "🛡️ Staff Apply", "manager_apply": "👑 Manager Apply",
                        "assistance": "🆘 Assistance", "response": "💬 Réponse"}


def _tid(number: int) -> str:
    return f"{number:04d}"


async def _respond(interaction: discord.Interaction, message: str, *, ephemeral: bool = True):
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(message, ephemeral=ephemeral)


class TicketReasonModal(discord.ui.Modal, title="Open a ticket"):
    reason = discord.ui.TextInput(label="Reason", placeholder="Tell us briefly why you are opening the ticket...", min_length=1, max_length=1000, required=True, style=discord.TextStyle.paragraph)

    def __init__(self, cog: "TicketCog", ticket_type: str):
        super().__init__(timeout=300)
        self.cog, self.ticket_type = cog, ticket_type

    async def on_submit(self, interaction: discord.Interaction):
        reason = str(self.reason.value).strip()
        if not reason:
            return await _respond(interaction, "Please enter a brief reason before opening the ticket.")
        await self.cog.create_from_modal(interaction, self.ticket_type, reason)


class TicketPanelView(discord.ui.View):
    def __init__(self, cog: "TicketCog", labels: dict | None = None):
        super().__init__(timeout=None)
        self.cog = cog
        for key, style in (
            ("staff_apply", discord.ButtonStyle.primary),
            ("manager_apply", discord.ButtonStyle.primary),
            ("assistance", discord.ButtonStyle.success),
            ("response", discord.ButtonStyle.secondary),
        ):
            button = discord.ui.Button(label=(labels or {}).get(key, DEFAULT_PANEL_LABELS[key]), style=style,
                                       custom_id=f"ticket:create:{key}")
            async def callback(interaction: discord.Interaction, ticket_type=key):
                if interaction.guild is None:
                    return await _respond(interaction, "Tickets can only be opened in a server.")
                existing = await find_open_ticket(interaction.guild.id, interaction.user.id)
                if existing:
                    channel = interaction.guild.get_channel(existing.channel_id)
                    link = f" — <#{existing.channel_id}>" if channel else ""
                    return await _respond(interaction, f"You already have an open ticket: **Ticket #{_tid(existing.ticket_number)}**{link}")
                await interaction.response.send_modal(TicketReasonModal(self.cog, ticket_type))
            button.callback = callback
            self.add_item(button)


class TicketControlsView(discord.ui.View):
    def __init__(self, cog: "TicketCog"):
        super().__init__(timeout=None)
        self.cog = cog
        for label, emoji, style, action in (
            ("Close Ticket", "🔒", discord.ButtonStyle.danger, "close"),
            ("Claim Ticket", "👤", discord.ButtonStyle.primary, "claim"),
            ("Add Member", "➕", discord.ButtonStyle.secondary, "add"),
            ("Remove Member", "➖", discord.ButtonStyle.secondary, "remove"),
        ):
            button = discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"ticket:{action}")
            async def callback(interaction: discord.Interaction, action=action):
                await self.cog.control(interaction, action)
            button.callback = callback
            self.add_item(button)


class CloseConfirmView(discord.ui.View):
    def __init__(self, cog: "TicketCog", channel_id: int):
        super().__init__(timeout=90)
        self.cog, self.channel_id = cog, channel_id

    @discord.ui.button(label="Confirm Close", emoji="✅", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.finish_close(interaction, self.channel_id)

    @discord.ui.button(label="Cancel", emoji="❌", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Ticket close cancelled.", view=None)


class TicketMemberSelect(discord.ui.View):
    def __init__(self, cog: "TicketCog", action: str, ticket: Ticket):
        super().__init__(timeout=90)
        self.cog, self.action, self.ticket = cog, action, ticket
        select = discord.ui.UserSelect(placeholder="Select a server member", min_values=1, max_values=1)
        select.callback = self.selected
        self.add_item(select)

    async def selected(self, interaction: discord.Interaction):
        member = self.children[0].values[0]
        await self.cog.change_member_access(interaction, self.ticket, member, self.action == "add")


class TicketCog(commands.Cog):
    ticket = app_commands.Group(name="ticket", description="Configure and manage support tickets", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._locks: dict[int, asyncio.Lock] = {}

    async def cog_load(self):
        # Persistent IDs keep panel/control buttons working after restarts.
        self.bot.add_view(TicketPanelView(self))
        self.bot.add_view(TicketControlsView(self))

    async def _labels_for(self, guild_id: int) -> dict:
        raw = await config_service.get_setting(guild_id, "ticket_button_labels")
        try:
            labels = json.loads(raw or "{}")
            if isinstance(labels, dict):
                return {key: str(labels.get(key, DEFAULT_PANEL_LABELS[key]))[:80] for key in TYPES}
        except (ValueError, TypeError):
            pass
        return dict(DEFAULT_PANEL_LABELS)

    async def _panel_embed(self, guild: discord.Guild) -> discord.Embed:
        title = await config_service.get_setting(guild.id, "ticket_panel_title") or "VALYRIA SUPPORT CENTER"
        description = await config_service.get_setting(guild.id, "ticket_panel_description") or "Choose the type of request below. You will briefly explain why you are opening the ticket, then a private channel will be created for you and the appropriate team will assist you."
        embed = discord.Embed(title=str(title)[:256], description=str(description)[:4000], color=discord.Color.from_rgb(119, 85, 180))
        banner = await config_service.get_setting(guild.id, "ticket_banner_url")
        if banner:
            embed.set_image(url=str(banner))
        embed.set_footer(text="Valyria Ticket Panel")
        return embed

    async def _is_manager(self, member: discord.Member) -> bool:
        return await self._has_ticket_role(member, "role_ticket_manager") or await has_level(member, StaffLevel.ADMIN)

    async def _has_ticket_role(self, member: discord.Member, key: str) -> bool:
        role_id = await config_service.get_setting(member.guild.id, key)
        return bool(role_id and any(role.id == role_id for role in member.roles))

    async def _can_manage(self, member: discord.Member, row: Ticket) -> bool:
        if row.ticket_type == "manager_apply":
            return await self._is_manager(member)
        return await has_level(member, StaffLevel.STAFF) or await self._has_ticket_role(member, "role_ticket_staff") or await self._is_manager(member)

    async def _log(self, guild: discord.Guild, title: str, row: Ticket, *, user=None, staff=None, reason=None):
        await send_log(guild, LogEvent(LogType.TICKET, title, user=user, staff=staff, reason=reason,
            ids={"Ticket": f"#{_tid(row.ticket_number)}", "Type": row.ticket_type, "Channel": row.channel_id}))

    async def create_from_modal(self, interaction: discord.Interaction, ticket_type: str, reason: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            return await _respond(interaction, "Tickets can only be opened in a server.")
        # Per-process lock plus database unique/index checks makes double-clicks safe.
        lock = self._locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            existing = await find_open_ticket(guild.id, interaction.user.id)
            if existing:
                return await _respond(interaction, f"You already have an open ticket: **Ticket #{_tid(existing.ticket_number)}** <#{existing.channel_id}>")
            key = f"ticket_category_{ticket_type}"
            category_id = await config_service.get_setting(guild.id, key)
            if category_id is None:
                category_id = await config_service.get_setting(guild.id, "ticket_category")
            category = guild.get_channel(category_id) if category_id else None
            if not isinstance(category, discord.CategoryChannel):
                return await _respond(interaction, "The ticket category is not configured or no longer exists. Ask a server administrator to configure it.")
            if ticket_type == "manager_apply":
                manager_role_id = await config_service.get_setting(guild.id, "role_ticket_manager")
                if not manager_role_id or guild.get_role(manager_role_id) is None:
                    return await _respond(interaction, "The ticket manager role is not configured or no longer exists. Ask an administrator to set it first.")
            overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
            overwrites[interaction.user] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True, embed_links=True)
            bot_member = guild.me
            if bot_member:
                overwrites[bot_member] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True, manage_messages=True)
            role_keys = ("role_ticket_staff", "role_ticket_manager", "role_support")
            for role_key in role_keys:
                role_id = await config_service.get_setting(guild.id, role_key)
                role = guild.get_role(role_id) if role_id else None
                if role:
                    overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
            _, slug, _ = TYPES[ticket_type]
            # Reserve through database before channel creation; failed creation leaves a gap, never reusing a number.
            channel = None
            try:
                number = await reserve_ticket_number(guild.id)
                channel = await guild.create_text_channel(f"{slug}-{_tid(number)}", category=category,
                    overwrites=overwrites, reason=f"Ticket #{_tid(number)} opened by {interaction.user}")
                row = await create_ticket(guild_id=guild.id, ticket_type=ticket_type,
                    creator_id=interaction.user.id, category_id=category.id, reason=reason,
                    channel_id=channel.id, ticket_number=number)
            except discord.Forbidden:
                await send_log(guild, LogEvent(LogType.TICKET, "⚠️ Ticket channel creation failed", user=interaction.user, reason="Missing Manage Channels permission", fields=[("Ticket type", ticket_type, True)]))
                return await _respond(interaction, "I cannot create the ticket channel. Please grant me Manage Channels and the required category permissions.")
            except (discord.HTTPException, SQLAlchemyError, ValueError) as exc:
                logger.exception("Ticket creation failed in guild %s", guild.id)
                await send_log(guild, LogEvent(LogType.TICKET, "⚠️ Ticket creation failed", user=interaction.user, reason=str(exc), fields=[("Ticket type", ticket_type, True)]))
                if channel:
                    try: await channel.delete(reason="Ticket creation failed")
                    except discord.HTTPException: pass
                return await _respond(interaction, "I could not create the ticket because of a Discord or database error. Please try again later.")
            label, _, intro = TYPES[ticket_type]
            embed = discord.Embed(title=f"🎫 VALYRIA TICKET · #{_tid(row.ticket_number)}",
                description=f"**Category:** {label}\n**Created by:** {interaction.user.mention}\n**Reason:**\n{reason}\n\n{intro}\n\nA member of the appropriate team will assist you as soon as possible. Thank you for contacting Valyria.",
                color=discord.Color.from_rgb(119, 85, 180))
            embed.set_thumbnail(url=interaction.user.display_avatar.url)
            banner = await config_service.get_setting(guild.id, "ticket_banner_url")
            if banner: embed.set_image(url=str(banner))
            try:
                message = await channel.send(content=interaction.user.mention, embed=embed, view=TicketControlsView(self))
                async with async_session() as session:
                    async with session.begin():
                        dbrow = await session.get(Ticket, row.id)
                        dbrow.opening_message_id = message.id
            except discord.HTTPException:
                logger.exception("Ticket opening message failed for %s", channel.id)
                await self._log(guild, "⚠️ Ticket opening message failed", row, user=interaction.user, reason=reason)
            await self._log(guild, "🎫 Ticket created", row, user=interaction.user, reason=reason)
            await _respond(interaction, f"Your ticket **#{_tid(row.ticket_number)}** has been created: {channel.mention}")

    async def control(self, interaction: discord.Interaction, action: str):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await _respond(interaction, "This action is only available in a server.")
        row = await get_ticket(interaction.guild.id, interaction.channel_id)
        if not row or row.status != "open":
            return await _respond(interaction, "This is not an open ticket.")
        if row.ticket_type == "appeal":
            return await _respond(interaction, "Please use the Appeal controls in this channel so the appeal status and ticket are closed together.")
        if action == "close" and interaction.user.id == row.creator_id:
            await interaction.response.send_message("Are you sure you want to close this ticket?", view=CloseConfirmView(self, row.channel_id), ephemeral=True)
            return
        if not await self._can_manage(interaction.user, row):
            return await _respond(interaction, "You do not have permission to manage this ticket.")
        if action == "claim":
            if not await claim_ticket(row.guild_id, row.channel_id, interaction.user.id):
                return await _respond(interaction, "This ticket is already claimed or closed.")
            await self._refresh_opening_message(interaction.guild, row, interaction.user)
            await self._log(interaction.guild, "👤 Ticket claimed", row, user=interaction.user, staff=interaction.user)
            return await _respond(interaction, f"You claimed ticket #{_tid(row.ticket_number)}.")
        if action == "close":
            await interaction.response.send_message("Are you sure you want to close this ticket?", view=CloseConfirmView(self, row.channel_id), ephemeral=True)
        elif action in ("add", "remove"):
            await interaction.response.send_message("Select a member:", view=TicketMemberSelect(self, action, row), ephemeral=True)

    async def _refresh_opening_message(self, guild: discord.Guild, row: Ticket, staff: discord.Member):
        channel = guild.get_channel(row.channel_id)
        if not isinstance(channel, discord.TextChannel) or not row.opening_message_id:
            return
        try:
            message = await channel.fetch_message(row.opening_message_id)
            embed = message.embeds[0]
            fields = [f for f in embed.fields if f.name != "Assigned to"]
            embed.clear_fields()
            for field in fields: embed.add_field(name=field.name, value=field.value, inline=field.inline)
            embed.add_field(name="Assigned to", value=staff.mention, inline=False)
            await message.edit(embed=embed)
        except (discord.HTTPException, IndexError):
            logger.warning("Could not update opening message for ticket %s", row.id)

    async def change_member_access(self, interaction: discord.Interaction, row: Ticket, member: discord.Member, add: bool):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member) or not await self._can_manage(interaction.user, row):
            return await _respond(interaction, "You do not have permission to manage this ticket.")
        if not add and member.id == row.creator_id and not await self._is_manager(interaction.user):
            return await _respond(interaction, "Only a ticket manager can remove the ticket creator.")
        channel = interaction.guild.get_channel(row.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return await _respond(interaction, "The ticket channel no longer exists.")
        try:
            if add:
                await channel.set_permissions(member, view_channel=True, send_messages=True, read_message_history=True)
                await self._log(interaction.guild, "➕ Member added to ticket", row, user=member, staff=interaction.user)
                return await _respond(interaction, f"{member.mention} can now access this ticket.")
            await channel.set_permissions(member, overwrite=None)
            await self._log(interaction.guild, "➖ Member removed from ticket", row, user=member, staff=interaction.user)
            return await _respond(interaction, f"{member.mention} access was removed.")
        except discord.Forbidden:
            return await _respond(interaction, "I cannot update this channel's permissions. Check my Manage Channels permission and role position.")
        except discord.HTTPException:
            logger.exception("Ticket member access update failed")
            return await _respond(interaction, "Discord could not update the ticket permissions. Please try again.")

    async def finish_close(self, interaction: discord.Interaction, channel_id: int):
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            return await _respond(interaction, "This action is only available in a server.")
        row = await get_ticket(guild.id, channel_id)
        if not row or row.status != "open":
            return await _respond(interaction, "This ticket is already closed.")
        if interaction.user.id != row.creator_id and not await self._can_manage(interaction.user, row):
            return await _respond(interaction, "You do not have permission to close this ticket.")
        if not await close_ticket(guild.id, channel_id, interaction.user.id):
            return await _respond(interaction, "This ticket is already closed.")
        channel = guild.get_channel(channel_id)
        creator = guild.get_member(row.creator_id)
        access_update_failed = False
        if isinstance(channel, discord.TextChannel) and creator:
            try:
                await channel.set_permissions(creator, overwrite=None)
                await channel.set_permissions(guild.default_role, view_channel=False, send_messages=False)
            except discord.HTTPException:
                logger.exception("Could not update closed ticket permissions")
                access_update_failed = True
        await self._log(guild, "🔒 Ticket closed", row, user=creator, staff=interaction.user)
        if interaction.user.id != row.creator_id:
            try:
                xp_amount = await config_service.get_setting(guild.id, "staff_xp_ticket_close")
                await add_staff_xp(guild_id=guild.id, staff_id=interaction.user.id,
                    target_id=row.creator_id, action="ticket_close", amount=xp_amount or 0,
                    member=interaction.user)
            except Exception:
                logger.exception("Ticket closed but Staff XP could not be awarded")
                await send_log(guild, LogEvent(LogType.TICKET, "⚠️ Ticket Staff XP award failed", user=creator, staff=interaction.user, ids={"Ticket": row.ticket_number}))
        transcript_enabled = await config_service.get_setting(guild.id, "ticket_transcript_enabled")
        log_id = await config_service.get_setting(guild.id, "ticket_log_channel")
        log_channel = guild.get_channel(log_id) if log_id else None
        transcript_unavailable = bool(transcript_enabled and (not isinstance(channel, discord.TextChannel) or not isinstance(log_channel, discord.TextChannel)))
        if transcript_unavailable:
            logger.warning("Ticket transcript skipped: channel or configured ticket log channel is unavailable for ticket %s", row.id)
        if transcript_enabled and isinstance(channel, discord.TextChannel) and isinstance(log_channel, discord.TextChannel):
            try:
                lines = [f"Ticket #{_tid(row.ticket_number)} · {row.ticket_type}", f"Creator: {row.creator_id}",
                         f"Reason: {row.reason}", f"Created at: {row.created_at}",
                         f"Closed at: {discord.utils.utcnow().isoformat()}", f"Closed by: {interaction.user.id}",
                         "", "--- Messages ---"]
                async for message in channel.history(limit=2000, oldest_first=True):
                    content = message.clean_content or "[no text]"
                    if message.attachments: content += " [Attachments: " + ", ".join(a.url for a in message.attachments) + "]"
                    lines.append(f"[{message.created_at.isoformat()}] {message.author} ({message.author.id}): {content}")
                file = discord.File(io.BytesIO("\n".join(lines).encode("utf-8")), filename=f"ticket-{_tid(row.ticket_number)}-transcript.txt")
                await log_channel.send(content=f"Transcript for ticket #{_tid(row.ticket_number)}", file=file)
            except discord.HTTPException:
                logger.exception("Ticket transcript delivery failed")
                await send_log(guild, LogEvent(LogType.TICKET, "⚠️ Ticket transcript delivery failed", user=creator, staff=interaction.user, ids={"Ticket": row.ticket_number}))
        close_message = f"Ticket #{_tid(row.ticket_number)} is closed. The channel is retained for staff."
        if transcript_unavailable:
            close_message += " A transcript could not be sent because the ticket log channel is missing or unavailable."
        if access_update_failed:
            close_message += " I could not revoke the creator's channel access; please check my Manage Channels permission."
            await send_log(guild, LogEvent(LogType.TICKET, "⚠️ Closed ticket access could not be updated", user=creator, staff=interaction.user, ids={"Ticket": row.ticket_number}))
        if interaction.message is not None and not interaction.response.is_done():
            await interaction.response.edit_message(content=close_message, view=None)
        else:
            await _respond(interaction, close_message)

    @ticket.command(name="panel", description="Send the configured ticket panel")
    @requires_level(StaffLevel.ADMIN)
    async def panel(self, interaction: discord.Interaction):
        guild = interaction.guild
        channel_id = await config_service.get_setting(guild.id, "ticket_panel_channel")
        channel = guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            return await _respond(interaction, "Set a valid panel channel first with /ticket panel-channel.")
        labels = await self._labels_for(guild.id)
        try:
            await channel.send(embed=await self._panel_embed(guild), view=TicketPanelView(self, labels))
        except discord.Forbidden:
            return await _respond(interaction, "I cannot send messages or embeds in the configured panel channel.")
        await interaction.response.send_message(f"Ticket panel sent to {channel.mention}.", ephemeral=True)

    @ticket.command(name="panel-channel", description="Configure the ticket panel channel")
    @app_commands.describe(channel="Where the ticket panel will be posted")
    @requires_level(StaffLevel.ADMIN)
    async def panel_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await config_service.set_setting(interaction.guild_id, "ticket_panel_channel", channel.id, interaction.user.id)
        await interaction.response.send_message(f"Ticket panel channel set to {channel.mention}.", ephemeral=True)

    @ticket.command(name="set-category", description="Configure a category for a ticket type")
    @app_commands.choices(ticket_type=TYPE_CHOICES)
    @app_commands.describe(ticket_type="Ticket type", category="Category for this ticket type")
    @requires_level(StaffLevel.ADMIN)
    async def set_category(self, interaction: discord.Interaction, ticket_type: app_commands.Choice[str], category: discord.CategoryChannel):
        await config_service.set_setting(interaction.guild_id, f"ticket_category_{ticket_type.value}", category.id, interaction.user.id)
        await interaction.response.send_message(f"{TYPES[ticket_type.value][0]} tickets will be created in **{category.name}**.", ephemeral=True)

    @ticket.command(name="set-log-channel", description="Configure ticket activity and transcript channel")
    @requires_level(StaffLevel.ADMIN)
    async def set_log_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await config_service.set_setting(interaction.guild_id, "ticket_log_channel", channel.id, interaction.user.id)
        await config_service.set_setting(interaction.guild_id, "log_ticket", channel.id, interaction.user.id)
        await interaction.response.send_message(f"Ticket logs and transcripts will be sent to {channel.mention}.", ephemeral=True)

    @ticket.command(name="set-banner", description="Configure the ticket panel banner URL")
    @app_commands.describe(url="HTTPS image URL, or leave empty to clear it")
    @requires_level(StaffLevel.ADMIN)
    async def set_banner(self, interaction: discord.Interaction, url: str = ""):
        url = url.strip()
        if url and not re.match(r"^https://[^\s]+$", url, re.I):
            return await _respond(interaction, "The banner must be a valid HTTPS URL.")
        await config_service.set_setting(interaction.guild_id, "ticket_banner_url", url, interaction.user.id) if url else await config_service.reset_setting(interaction.guild_id, "ticket_banner_url")
        await interaction.response.send_message("Ticket banner updated." if url else "Ticket banner cleared.", ephemeral=True)

    @ticket.command(name="set-staff-role", description="Configure the ticket staff role")
    @requires_level(StaffLevel.ADMIN)
    async def set_staff_role(self, interaction: discord.Interaction, role: discord.Role):
        await config_service.set_setting(interaction.guild_id, "role_ticket_staff", role.id, interaction.user.id)
        await interaction.response.send_message(f"Ticket staff role set to {role.mention}.", ephemeral=True)

    @ticket.command(name="set-manager-role", description="Configure the ticket manager role")
    @requires_level(StaffLevel.ADMIN)
    async def set_manager_role(self, interaction: discord.Interaction, role: discord.Role):
        await config_service.set_setting(interaction.guild_id, "role_ticket_manager", role.id, interaction.user.id)
        await interaction.response.send_message(f"Ticket manager role set to {role.mention}.", ephemeral=True)

    @ticket.command(name="set-panel-text", description="Set ticket panel title and description")
    @app_commands.describe(title="Panel title", description="Panel introduction shown to members")
    @requires_level(StaffLevel.ADMIN)
    async def set_panel_text(self, interaction: discord.Interaction, title: str, description: str):
        if len(title) > 256 or len(description) > 4000:
            return await _respond(interaction, "Title must be at most 256 characters and description at most 4000.")
        await config_service.set_setting(interaction.guild_id, "ticket_panel_title", title, interaction.user.id)
        await config_service.set_setting(interaction.guild_id, "ticket_panel_description", description, interaction.user.id)
        await interaction.response.send_message("Ticket panel text updated.", ephemeral=True)

    @ticket.command(name="set-button-label", description="Customize one ticket panel button label")
    @app_commands.choices(ticket_type=TYPE_CHOICES)
    @app_commands.describe(ticket_type="Button to update", label="New label (up to 80 characters)")
    @requires_level(StaffLevel.ADMIN)
    async def set_button_label(self, interaction: discord.Interaction, ticket_type: app_commands.Choice[str], label: str):
        if not label.strip() or len(label) > 80:
            return await _respond(interaction, "Button labels must contain 1–80 characters.")
        labels = await self._labels_for(interaction.guild_id)
        labels[ticket_type.value] = label.strip()
        await config_service.set_setting(interaction.guild_id, "ticket_button_labels", json.dumps(labels, ensure_ascii=False), interaction.user.id)
        await interaction.response.send_message("Button label updated. Publish a new panel with /ticket panel.", ephemeral=True)

    @ticket.command(name="config", description="Show current ticket configuration")
    @requires_level(StaffLevel.ADMIN)
    async def config(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        rows = []
        panel = await config_service.get_setting(guild_id, "ticket_panel_channel")
        rows.append(f"Panel: {f'<#{panel}>' if panel else 'not set'}")
        for key, (label, _, _) in TYPES.items():
            category = await config_service.get_setting(guild_id, f"ticket_category_{key}") or await config_service.get_setting(guild_id, "ticket_category")
            rows.append(f"{label}: {f'<#{category}>' if category else 'not set'}")
        for key, label in (("role_ticket_staff", "Staff role"), ("role_ticket_manager", "Manager role"), ("ticket_log_channel", "Log channel")):
            value = await config_service.get_setting(guild_id, key)
            rows.append(f"{label}: {f'<@&{value}>' if 'role' in key and value else f'<#{value}>' if value else 'not set'}")
        banner = await config_service.get_setting(guild_id, "ticket_banner_url")
        rows.append(f"Banner: {'configured' if banner else 'not set'}")
        panel_title = await config_service.get_setting(guild_id, "ticket_panel_title")
        rows.append(f"Panel title: {str(panel_title or '—')[:120]}")
        labels = await self._labels_for(guild_id)
        rows.extend(f"Button · {TYPES[key][0]}: {value}" for key, value in labels.items())
        await interaction.response.send_message(embed=discord.Embed(title="Ticket Configuration", description="\n".join(rows), color=discord.Color.blurple()), ephemeral=True)

    @ticket.command(name="list", description="List open tickets in this server")
    @requires_level(StaffLevel.STAFF)
    async def list_open(self, interaction: discord.Interaction):
        from sqlalchemy import select
        async with async_session() as session:
            rows = list((await session.scalars(select(Ticket).where(Ticket.guild_id == interaction.guild_id, Ticket.status == "open").order_by(Ticket.ticket_number))).all())
        description = "\n".join(f"#{_tid(row.ticket_number)} · {TYPES.get(row.ticket_type, (row.ticket_type.title(), '', ''))[0]} · <#{row.channel_id}> · <@{row.creator_id}>" for row in rows[:25]) or "No open tickets."
        await interaction.response.send_message(embed=discord.Embed(title=f"Open Tickets ({len(rows)})", description=description), ephemeral=True)

    @ticket.command(name="close", description="Close the ticket in the current channel")
    async def close_command(self, interaction: discord.Interaction):
        await self.control(interaction, "close")


async def setup(bot: commands.Bot):
    await bot.add_cog(TicketCog(bot))
