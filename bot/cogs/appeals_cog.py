"""Guild-scoped member appeals using the existing ticket, permission and logging systems."""
import asyncio
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from bot.core.logger import logger
from bot.core.permissions import StaffLevel, has_level, requires_level
from bot.database.engine import async_session
from bot.database.models.ticket import Ticket
from bot.services import config_service
from bot.services.appeal_service import (
    close_appeal, create_appeal, decide_appeal, find_pending_appeal,
    get_appeal, reserve_appeal_number, set_appeal_message,
)
from bot.services.logging_service import LogEvent, LogType, send_log
from bot.services.ticket_service import close_ticket, create_ticket, reserve_ticket_number


def _appeal_label(number: int) -> str:
    return f"#{number:03d}"


async def _reply(interaction: discord.Interaction, content: str, *, ephemeral: bool = True):
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(content, ephemeral=ephemeral)


class AppealSubmissionModal(discord.ui.Modal, title="Submit Appeal"):
    reason = discord.ui.TextInput(
        label="Reason for appeal", placeholder="Why should the moderation action be reconsidered?",
        min_length=3, max_length=500, required=True,
    )
    explanation = discord.ui.TextInput(
        label="Explanation", placeholder="Explain the situation in detail.",
        min_length=3, max_length=1000, required=True, style=discord.TextStyle.paragraph,
    )
    additional = discord.ui.TextInput(
        label="Additional information (optional)",
        placeholder="Add any information or evidence that could help the review.",
        max_length=1000, required=False, style=discord.TextStyle.paragraph,
    )

    def __init__(self, cog: "AppealsCog"):
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.submit_appeal(
            interaction, str(self.reason.value).strip(), str(self.explanation.value).strip(),
            str(self.additional.value).strip(),
        )


class AppealDecisionModal(discord.ui.Modal):
    note = discord.ui.TextInput(
        label="Staff note / rejection reason", placeholder="Add a short explanation for the member.",
        max_length=1000, required=False, style=discord.TextStyle.paragraph,
    )

    def __init__(self, cog: "AppealsCog", appeal_id: int, *, accepted: bool):
        super().__init__(title="Accept Appeal" if accepted else "Reject Appeal", timeout=300)
        self.cog, self.appeal_id, self.accepted = cog, appeal_id, accepted
        if not accepted:
            self.note.placeholder = "Explain why the appeal was rejected."
            self.note.required = True
            self.note.min_length = 3

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.handle_decision(interaction, self.appeal_id, self.accepted, str(self.note.value).strip())


class AppealPanelView(discord.ui.View):
    def __init__(self, cog: "AppealsCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Submit Appeal", emoji="📝", style=discord.ButtonStyle.primary, custom_id="appeal:submit")
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            return await _reply(interaction, "Appeals can only be submitted in a server.")
        try:
            active = await find_pending_appeal(interaction.guild.id, interaction.user.id)
        except SQLAlchemyError:
            logger.exception("Unable to check for pending appeal")
            return await _reply(interaction, "We could not check your appeal status. Please try again shortly.")
        if active:
            return await _reply(interaction, "⚠️ You already have an active appeal. Please wait for the staff team to review it.")
        await interaction.response.send_modal(AppealSubmissionModal(self.cog))


class AppealControlsView(discord.ui.View):
    def __init__(self, cog: "AppealsCog", *, pending: bool = True):
        super().__init__(timeout=None)
        self.cog = cog
        if pending:
            accept = discord.ui.Button(label="Accept Appeal", emoji="🟢", style=discord.ButtonStyle.success, custom_id="appeal:accept")
            reject = discord.ui.Button(label="Reject Appeal", emoji="🔴", style=discord.ButtonStyle.danger, custom_id="appeal:reject")
            accept.callback = self._accept
            reject.callback = self._reject
            self.add_item(accept)
            self.add_item(reject)
        close = discord.ui.Button(label="Close Appeal", emoji="🔵", style=discord.ButtonStyle.secondary, custom_id="appeal:close")
        close.callback = self._close
        self.add_item(close)

    async def _accept(self, interaction: discord.Interaction):
        appeal = await self.cog._appeal_for_interaction(interaction)
        if appeal is None:
            return
        if not await self.cog._authorized_staff(interaction, appeal):
            return
        if appeal.status != "Pending":
            return await _reply(interaction, "This appeal has already been processed.")
        await interaction.response.send_modal(AppealDecisionModal(self.cog, appeal.id, accepted=True))

    async def _reject(self, interaction: discord.Interaction):
        appeal = await self.cog._appeal_for_interaction(interaction)
        if appeal is None:
            return
        if not await self.cog._authorized_staff(interaction, appeal):
            return
        if appeal.status != "Pending":
            return await _reply(interaction, "This appeal has already been processed.")
        await interaction.response.send_modal(AppealDecisionModal(self.cog, appeal.id, accepted=False))

    async def _close(self, interaction: discord.Interaction):
        appeal = await self.cog._appeal_for_interaction(interaction)
        if appeal is None:
            return
        if not await self.cog._authorized_staff(interaction, appeal):
            return
        if appeal.status == "Closed":
            return await _reply(interaction, "This appeal is already closed.")
        await interaction.response.send_message(
            f"Close appeal {_appeal_label(appeal.appeal_number)}? This will mark its ticket closed.",
            view=AppealCloseConfirmView(self.cog, appeal.id), ephemeral=True,
        )


class AppealCloseConfirmView(discord.ui.View):
    def __init__(self, cog: "AppealsCog", appeal_id: int):
        super().__init__(timeout=90)
        self.cog, self.appeal_id = cog, appeal_id

    @discord.ui.button(label="Confirm Close", emoji="✅", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_close(interaction, self.appeal_id)

    @discord.ui.button(label="Cancel", emoji="❌", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Appeal close cancelled.", view=None)


class AppealsCog(commands.Cog):
    appeal = app_commands.Group(name="appeal", description="Publish and manage moderation appeals", guild_only=True)

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._create_locks: dict[tuple[int, int], asyncio.Lock] = {}

    async def cog_load(self):
        self.bot.add_view(AppealPanelView(self))
        self.bot.add_view(AppealControlsView(self))

    @staticmethod
    def panel_embed() -> discord.Embed:
        return discord.Embed(
            title="Appeals",
            description=("If you believe a moderation action taken against you should be reconsidered, "
                         "submit an appeal below. Explain your situation clearly and include any helpful "
                         "information. The staff team will review your appeal and its status will be shown "
                         "in your private appeal ticket."),
            color=discord.Color.blurple(),
        )

    async def _ticket_overwrites(self, guild: discord.Guild, member: discord.Member):
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True,
                                                 attach_files=True, embed_links=True),
        }
        bot_member = guild.me
        if bot_member:
            overwrites[bot_member] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                read_message_history=True, manage_channels=True, manage_messages=True)
        role_keys = (
            "role_ticket_staff", "role_ticket_manager", "role_owner", "role_admin",
            "role_head_moderator", "role_moderator", "role_trial_moderator", "role_support", "role_staff",
        )
        team_roles = []
        for key in role_keys:
            role_id = await config_service.get_setting(guild.id, key)
            role = guild.get_role(role_id) if role_id else None
            if role:
                if key in {"role_ticket_staff", "role_ticket_manager", "role_support"}:
                    overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                        read_message_history=True)
                team_roles.append(role)
        return overwrites, team_roles

    async def submit_appeal(self, interaction: discord.Interaction, reason: str,
                            explanation: str, additional_information: str):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member) or not reason or not explanation:
            return await _reply(interaction, "Please provide both a reason and an explanation for your appeal.")
        lock = self._create_locks.setdefault((guild.id, member.id), asyncio.Lock())
        async with lock:
            try:
                if await find_pending_appeal(guild.id, member.id):
                    return await _reply(interaction, "⚠️ You already have an active appeal. Please wait for the staff team to review it.")
                category_id = await config_service.get_setting(guild.id, "ticket_category_assistance")
                if category_id is None:
                    category_id = await config_service.get_setting(guild.id, "ticket_category")
                category = guild.get_channel(category_id) if category_id else None
                if not isinstance(category, discord.CategoryChannel):
                    return await _reply(interaction, "Appeals are not configured yet. Please contact a server administrator.")
                appeal_number = await reserve_appeal_number(guild.id)
                ticket_number = await reserve_ticket_number(guild.id)
                overwrites, team_roles = await self._ticket_overwrites(guild, member)
                channel = None
                ticket = None
                try:
                    channel = await guild.create_text_channel(
                        f"appeal-{ticket_number:04d}", category=category, overwrites=overwrites,
                        reason=f"Appeal {_appeal_label(appeal_number)} submitted by {member}",
                    )
                    ticket = await create_ticket(
                        guild_id=guild.id, ticket_type="appeal", creator_id=member.id,
                        category_id=category.id, reason=f"{reason}\n\n{explanation}",
                        channel_id=channel.id, ticket_number=ticket_number,
                    )
                    appeal = await create_appeal(
                        guild_id=guild.id, appeal_number=appeal_number, ticket_id=ticket.id,
                        user_id=member.id, username=str(member), reason=reason,
                        explanation=explanation, additional_information=additional_information,
                    )
                except ValueError as exc:
                    if ticket is not None:
                        try: await close_ticket(guild.id, channel.id, self.bot.user.id)
                        except SQLAlchemyError: logger.exception("Failed to close an orphan appeal ticket")
                    if channel:
                        try: await channel.delete(reason="Appeal could not be recorded")
                        except discord.HTTPException: pass
                    if str(exc) == "open_ticket_exists":
                        return await _reply(interaction, "You already have an open support ticket. Please use that private ticket or close it before submitting an appeal.")
                    if str(exc) == "pending_appeal_exists":
                        return await _reply(interaction, "⚠️ You already have an active appeal. Please wait for the staff team to review it.")
                    raise
                except (discord.HTTPException, SQLAlchemyError, IntegrityError):
                    logger.exception("Appeal ticket creation failed in guild %s", guild.id)
                    if ticket is not None and channel is not None:
                        try: await close_ticket(guild.id, channel.id, self.bot.user.id)
                        except SQLAlchemyError: logger.exception("Failed to close an orphan appeal ticket")
                    if channel:
                        try: await channel.delete(reason="Appeal creation failed")
                        except discord.HTTPException: pass
                    await send_log(guild, LogEvent(LogType.TICKET, "⚠️ Appeal ticket creation failed", user=member,
                        reason="Channel or database error", ids={"Appeal ID": _appeal_label(appeal_number)}))
                    return await _reply(interaction, "We could not create your appeal ticket. Please try again later.")

                embed = self._appeal_embed(appeal, member)
                staff_role_id = await config_service.get_setting(guild.id, "role_ticket_staff")
                staff_role = guild.get_role(staff_role_id) if staff_role_id else None
                mention = staff_role.mention if staff_role else " ".join(role.mention for role in team_roles[:3])
                try:
                    message = await channel.send(content=mention or None, embed=embed, view=AppealControlsView(self),
                        allowed_mentions=discord.AllowedMentions(roles=True, users=False, everyone=False))
                    await set_appeal_message(guild.id, appeal.id, message.id)
                except (discord.HTTPException, SQLAlchemyError):
                    logger.exception("Appeal opening message send failed")
                    try:
                        await close_appeal(guild_id=guild.id, appeal_id=appeal.id, staff_id=self.bot.user.id)
                        await channel.delete(reason="Appeal opening message could not be sent")
                    except (discord.HTTPException, SQLAlchemyError):
                        logger.exception("Appeal message cleanup failed")
                    await send_log(guild, LogEvent(LogType.TICKET, "⚠️ Appeal opening message failed", user=member,
                        ids={"Appeal ID": _appeal_label(appeal.appeal_number), "Channel": channel.id}))
                    return await _reply(interaction, "Your appeal record was saved, but the ticket message could not be sent. Please contact staff.")
                await self._log(guild, "📝 Appeal created", appeal, member=member, reason=appeal.reason)
                await _reply(interaction, f"✅ Your appeal has been submitted successfully.\nAppeal ID: **{_appeal_label(appeal.appeal_number)}**\nStatus: **Pending**\nTicket: {channel.mention}")
            except (discord.Forbidden, discord.HTTPException) as exc:
                logger.exception("Appeal submission failed")
                await send_log(guild, LogEvent(LogType.TICKET, "⚠️ Appeal submission failed", user=member, reason=str(exc)))
                await _reply(interaction, "The bot could not create the private appeal ticket. Please contact an administrator.")
            except SQLAlchemyError as exc:
                logger.exception("Appeal persistence failed")
                await send_log(guild, LogEvent(LogType.TICKET, "⚠️ Appeal database error", user=member, reason=str(exc)))
                await _reply(interaction, "We could not save your appeal. Please try again later.")
            except Exception as exc:
                logger.exception("Unexpected appeal submission error")
                await send_log(guild, LogEvent(LogType.TICKET, "⚠️ Unexpected appeal error", user=member, reason=str(exc)))
                await _reply(interaction, "An unexpected error prevented your appeal from being submitted.")

    @staticmethod
    def _appeal_embed(appeal, member: discord.abc.User | None = None) -> discord.Embed:
        created = appeal.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        embed = discord.Embed(
            title=f"📝 Appeal {_appeal_label(appeal.appeal_number)}",
            description=f"**Status:** `{appeal.status}`\n**Member:** {member.mention if member else f'<@{appeal.user_id}>'}\n**User ID:** `{appeal.user_id}`\n**Submitted:** <t:{int(created.timestamp())}:F>",
            color={"Pending": discord.Color.orange(), "Accepted": discord.Color.green(),
                   "Rejected": discord.Color.red(), "Closed": discord.Color.dark_grey()}.get(appeal.status, discord.Color.blurple()),
        )
        embed.add_field(name="Reason for appeal", value=appeal.reason[:1024], inline=False)
        embed.add_field(name="Explanation", value=appeal.explanation[:1024], inline=False)
        if appeal.additional_information:
            embed.add_field(name="Additional information", value=appeal.additional_information[:1024], inline=False)
        if appeal.staff_id:
            embed.add_field(name="Reviewed by", value=f"<@{appeal.staff_id}>", inline=True)
        if appeal.staff_decision:
            embed.add_field(name="Staff note", value=appeal.staff_decision[:1024], inline=False)
        if appeal.decided_at:
            decided = appeal.decided_at
            if decided.tzinfo is None:
                decided = decided.replace(tzinfo=timezone.utc)
            embed.add_field(name="Decision date", value=f"<t:{int(decided.timestamp())}:F>", inline=True)
        if member:
            embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"Appeal ID {appeal.id} · Appeal {_appeal_label(appeal.appeal_number)}")
        return embed

    async def _appeal_for_interaction(self, interaction: discord.Interaction):
        if interaction.guild is None or interaction.channel is None:
            await _reply(interaction, "This action is not available here.")
            return None
        try:
            async with async_session() as session:
                ticket = await session.scalar(select(Ticket).where(
                    Ticket.guild_id == interaction.guild.id,
                    Ticket.channel_id == interaction.channel_id,
                    Ticket.ticket_type == "appeal",
                ))
                if ticket is None:
                    await _reply(interaction, "This channel is not an appeal ticket.")
                    return None
                appeal = await session.scalar(select(Appeal).where(
                    Appeal.guild_id == interaction.guild.id, Appeal.ticket_id == ticket.id,
                ))
                if appeal is None:
                    await _reply(interaction, "The appeal record for this ticket could not be found.")
                return appeal
        except SQLAlchemyError:
            logger.exception("Appeal ticket lookup failed")
            await _reply(interaction, "We could not load this appeal. Please try again later.")
            return None

    async def _authorized_staff(self, interaction: discord.Interaction, appeal) -> bool:
        if not isinstance(interaction.user, discord.Member):
            await _reply(interaction, "Only authorized staff can manage appeals.")
            return False
        member = interaction.user
        try:
            staff_role = await config_service.get_setting(member.guild.id, "role_ticket_staff")
            manager_role = await config_service.get_setting(member.guild.id, "role_ticket_manager")
            role_ids = {role.id for role in member.roles}
            if ((staff_role and staff_role in role_ids) or (manager_role and manager_role in role_ids)
                    or await has_level(member, StaffLevel.STAFF)):
                return True
        except SQLAlchemyError:
            logger.exception("Unable to verify permissions for appeal management")
            await _reply(interaction, "Staff permissions could not be checked. Please try again shortly.")
            return False
        await _reply(interaction, "You do not have permission to manage appeals.")
        return False

    async def handle_decision(self, interaction: discord.Interaction, appeal_id: int,
                              accepted: bool, staff_note: str):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await _reply(interaction, "This action is only available in a server.")
        appeal = await self._appeal_for_interaction(interaction)
        if appeal is None:
            return
        if appeal.id != appeal_id:
            return await _reply(interaction, "This control does not belong to the current appeal ticket.")
        if not await self._authorized_staff(interaction, appeal):
            return
        if not accepted and len(staff_note.strip()) < 3:
            return await _reply(interaction, "Please provide a reason for rejecting this appeal.")
        status = "Accepted" if accepted else "Rejected"
        try:
            saved = await decide_appeal(guild_id=interaction.guild.id, appeal_id=appeal.id,
                staff_id=interaction.user.id, status=status, staff_decision=staff_note)
        except SQLAlchemyError:
            logger.exception("Appeal decision could not be saved")
            await send_log(interaction.guild, LogEvent(LogType.TICKET, "⚠️ Appeal decision save failed", user=interaction.user, staff=interaction.user, ids={"Appeal ID": _appeal_label(appeal.appeal_number)}))
            return await _reply(interaction, "The appeal decision could not be saved. Please try again.")
        if saved is None:
            return await _reply(interaction, "This appeal has already been processed.")
        channel = interaction.guild.get_channel(interaction.channel_id)
        if isinstance(channel, discord.TextChannel):
            member = interaction.guild.get_member(saved.user_id)
            if member:
                try:
                    await channel.set_permissions(member, overwrite=None)
                except discord.HTTPException:
                    logger.exception("Could not revoke member access to closed appeal %s", saved.id)
                    await send_log(interaction.guild, LogEvent(LogType.TICKET,
                        "⚠️ Closed appeal access could not be updated", user=member,
                        staff=interaction.user, ids={"Appeal ID": _appeal_label(saved.appeal_number)}))
            try:
                if saved.opening_message_id:
                    message = await channel.fetch_message(saved.opening_message_id)
                    await message.edit(embed=self._appeal_embed(saved, interaction.guild.get_member(saved.user_id)), view=AppealControlsView(self, pending=False))
            except discord.HTTPException:
                logger.exception("Could not refresh appeal status embed")
        await self._log(interaction.guild, f"{'🟢' if accepted else '🔴'} Appeal {status.lower()}", saved,
            member=interaction.guild.get_member(saved.user_id), staff=interaction.user, reason=staff_note or None)
        if not accepted:
            try:
                target = interaction.guild.get_member(saved.user_id) or await self.bot.fetch_user(saved.user_id)
                await target.send(f"Your appeal {_appeal_label(saved.appeal_number)} was rejected. Staff note: {staff_note}")
            except (discord.Forbidden, discord.HTTPException):
                logger.info("Could not DM member %s about rejected appeal %s", saved.user_id, saved.id)
        await _reply(interaction, f"Appeal {_appeal_label(saved.appeal_number)} marked **{status}**.")

    async def handle_close(self, interaction: discord.Interaction, appeal_id: int):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return await _reply(interaction, "This action is only available in a server.")
        appeal = await self._appeal_for_interaction(interaction)
        if appeal is None:
            return
        if appeal.id != appeal_id:
            return await _reply(interaction, "This control does not belong to the current appeal ticket.")
        if not await self._authorized_staff(interaction, appeal):
            return
        try:
            saved = await close_appeal(guild_id=interaction.guild.id, appeal_id=appeal.id, staff_id=interaction.user.id)
        except SQLAlchemyError:
            logger.exception("Appeal could not be closed")
            return await _reply(interaction, "This appeal could not be closed. Please try again.")
        if saved is None:
            return await _reply(interaction, "This appeal is already closed.")
        await self._log(interaction.guild, "🔵 Appeal closed", saved,
            member=interaction.guild.get_member(saved.user_id), staff=interaction.user, reason=saved.reason)
        channel = interaction.guild.get_channel(interaction.channel_id)
        if isinstance(channel, discord.TextChannel):
            try:
                if saved.opening_message_id:
                    message = await channel.fetch_message(saved.opening_message_id)
                    await message.edit(embed=self._appeal_embed(saved, interaction.guild.get_member(saved.user_id)), view=None)
            except discord.HTTPException:
                logger.exception("Could not refresh closed appeal embed")
        if interaction.message is not None:
            await interaction.response.edit_message(content=f"Appeal {_appeal_label(saved.appeal_number)} closed.", view=None)
        else:
            await _reply(interaction, f"Appeal {_appeal_label(saved.appeal_number)} closed.")

    async def _log(self, guild: discord.Guild, title: str, appeal, *, member=None, staff=None, reason=None):
        ids = {"Appeal ID": _appeal_label(appeal.appeal_number), "Appeal record": appeal.id,
               "Ticket": appeal.ticket_id, "User ID": appeal.user_id}
        if appeal.decided_at:
            ids["Decision time"] = appeal.decided_at.isoformat()
        if appeal.updated_at:
            ids["Updated"] = appeal.updated_at.isoformat()
        await send_log(guild, LogEvent(LogType.TICKET, title, user=member, staff=staff, reason=reason, ids=ids))

    @appeal.command(name="panel", description="Send the appeals panel to a channel")
    @app_commands.describe(channel="Channel where members can submit appeals")
    @requires_level(StaffLevel.ADMIN)
    async def panel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        try:
            await config_service.set_setting(interaction.guild_id, "appeals_channel", channel.id, interaction.user.id)
            await channel.send(embed=self.panel_embed(), view=AppealPanelView(self))
        except discord.Forbidden:
            return await _reply(interaction, "I cannot send the appeal panel in that channel. Check my Send Messages and Embed Links permissions.")
        except (discord.HTTPException, SQLAlchemyError):
            logger.exception("Appeals panel could not be published")
            return await _reply(interaction, "The appeals panel could not be published. Please try again.")
        await interaction.response.send_message(f"Appeals panel sent to {channel.mention}.", ephemeral=True)

    @appeal.command(name="config", description="Show the current Appeals configuration")
    @requires_level(StaffLevel.ADMIN)
    async def config(self, interaction: discord.Interaction):
        panel_id = await config_service.get_setting(interaction.guild_id, "appeals_channel")
        category_id = await config_service.get_setting(interaction.guild_id, "ticket_category_assistance")
        if category_id is None:
            category_id = await config_service.get_setting(interaction.guild_id, "ticket_category")
        role_id = await config_service.get_setting(interaction.guild_id, "role_ticket_staff")
        embed = discord.Embed(title="Appeals Configuration", color=discord.Color.blurple())
        embed.add_field(name="Panel channel", value=f"<#{panel_id}>" if panel_id else "Not configured", inline=False)
        embed.add_field(name="Appeal ticket category", value=f"<#{category_id}>" if category_id else "Not configured", inline=False)
        embed.add_field(name="Appeals/Staff role", value=f"<@&{role_id}>" if role_id else "Using configured staff hierarchy", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(AppealsCog(bot))
