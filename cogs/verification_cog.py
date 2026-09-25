from __future__ import annotations

import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import select

from bot.core.permissions import StaffLevel, requires_level
from bot.database.engine import async_session
from bot.database.models.verification import VerificationRecord
from bot.services import config_service, verification_service
from bot.services.logging_service import LogEvent, LogType, send_log
from bot.services.staff_xp_service import add_verification_xp

_record_locks: dict[int, asyncio.Lock] = {}

def _record_lock(record_id: int) -> asyncio.Lock:
    return _record_locks.setdefault(record_id, asyncio.Lock())

async def is_verification_team(member: discord.Member) -> bool:
    role_id = await config_service.get_setting(member.guild.id, "role_verification_team")
    return bool(role_id and any(role.id == role_id for role in member.roles))

def progress_embed(member: discord.Member, row: VerificationRecord) -> discord.Embed:
    gender = row.gender.title() if row.gender else "Not selected"
    other = "Yes" if row.is_other_server_staff is True else ("No" if row.is_other_server_staff is False else "Not answered")
    if row.other_server_name:
        other += f" — {row.other_server_name}"
    embed = discord.Embed(
        title="Valyria Verification",
        description=f"Member: {member.mention} ({member.id})",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Gender", value=gender, inline=True)
    embed.add_field(name="Staff elsewhere", value=other, inline=True)
    embed.add_field(name="Status", value=row.status, inline=True)
    return embed

async def eligible_members(guild: discord.Guild) -> list[discord.Member]:
    role_id = await config_service.get_setting(guild.id, "role_unverified")
    role = guild.get_role(role_id) if role_id else None
    if role is None:
        return []
    members = []
    try:
        async for member in guild.fetch_members(limit=None):
            if role in member.roles and not member.bot:
                members.append(member)
    except discord.HTTPException:
        members = [member for member in guild.members if role in member.roles and not member.bot]
    return sorted(members, key=lambda m: (m.joined_at or discord.utils.utcnow(), m.id))

class VerificationPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Select unverified member",
        emoji="🔎",
        style=discord.ButtonStyle.primary,
        custom_id="valyria:verification:open",
    )
    async def open_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not isinstance(interaction.user, discord.Member) or not await is_verification_team(interaction.user):
            return await interaction.response.send_message("Only Verification Team can use this panel.", ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        members = await eligible_members(interaction.guild)
        if not members:
            return await interaction.followup.send("No unverified members are waiting.", ephemeral=True)
        view = QueueView(members, 0)
        await interaction.followup.send(embed=view.embed(), view=view, ephemeral=True)

class QueueView(discord.ui.View):
    def __init__(self, members: list[discord.Member], page: int):
        super().__init__(timeout=300)
        self.members, self.page = members, page
        subset = members[page * 25:(page + 1) * 25]
        select = discord.ui.Select(
            placeholder="Choose an unverified member",
            options=[discord.SelectOption(label=m.display_name[:100] or str(m.id), description=f"ID {m.id}", value=str(m.id)) for m in subset],
        )
        select.callback = self.choose
        self.add_item(select)
        previous = discord.ui.Button(label="Previous", style=discord.ButtonStyle.secondary, disabled=page == 0)
        previous.callback = self.previous
        self.add_item(previous)
        next_page = discord.ui.Button(label="Next", style=discord.ButtonStyle.secondary, disabled=(page + 1) * 25 >= len(members))
        next_page.callback = self.next
        self.add_item(next_page)

    def embed(self):
        pages = max(1, (len(self.members) + 24) // 25)
        return discord.Embed(title="Unverified member queue", description=f"Page {self.page + 1}/{pages} · {len(self.members)} waiting", color=discord.Color.blurple())

    async def choose(self, interaction: discord.Interaction):
        if not isinstance(interaction.user, discord.Member) or not await is_verification_team(interaction.user):
            return await interaction.response.send_message("Verification Team role required.", ephemeral=True)
        member_id = int(interaction.data["values"][0])
        guild = interaction.guild
        if member_id == interaction.user.id:
            return await interaction.response.send_message("You cannot verify yourself.", ephemeral=True)
        member = guild.get_member(member_id)
        if member is None:
            try:
                member = await guild.fetch_member(member_id)
            except discord.HTTPException:
                return await interaction.response.send_message("Member is no longer in the server.", ephemeral=True)
        active = await verification_service.list_active(guild.id, member_id)
        row = next((item for item in active if item.status == "in_progress"), None)
        if row is None:
            row = await verification_service.start(guild.id, member_id, interaction.user.id)
        view = WorkflowView(row.id)
        content = "Resume the existing review." if row.staff_id != interaction.user.id else None
        await interaction.response.send_message(content=content, embed=progress_embed(member, row), view=view, ephemeral=True)
        interaction.client.add_view(view)

    async def previous(self, interaction: discord.Interaction):
        view = QueueView(self.members, self.page - 1)
        await interaction.response.edit_message(embed=view.embed(), view=view)

    async def next(self, interaction: discord.Interaction):
        view = QueueView(self.members, self.page + 1)
        await interaction.response.edit_message(embed=view.embed(), view=view)

class OtherStaffModal(discord.ui.Modal, title="Other Server Staff"):
    server_name = discord.ui.TextInput(label="Other server name", max_length=100, required=True)

    def __init__(self, record_id: int):
        super().__init__(timeout=300)
        self.record_id = record_id

    async def on_submit(self, interaction: discord.Interaction):
        await verification_service.set_answer(
            self.record_id, other_staff=True, server_name=str(self.server_name.value).strip()
        )
        await refresh_workflow(interaction, self.record_id)

class WorkflowView(discord.ui.View):
    def __init__(self, record_id: int):
        super().__init__(timeout=None)
        self.record_id = record_id
        controls = [
            ("Female", "👩", "female", discord.ButtonStyle.secondary),
            ("Male", "👨", "male", discord.ButtonStyle.secondary),
            ("Yes — elsewhere", "✅", "yes", discord.ButtonStyle.secondary),
            ("No", "❌", "no", discord.ButtonStyle.secondary),
            ("Verify", "🟢", "verify", discord.ButtonStyle.success),
            ("Reject", "⛔", "reject", discord.ButtonStyle.secondary),
            ("Ban", "🔨", "ban", discord.ButtonStyle.danger),
        ]
        for label, emoji, action, style in controls:
            button = discord.ui.Button(
                label=label, emoji=emoji, style=style,
                custom_id=f"valyria:verification:{action}:{record_id}",
            )
            button.callback = self.callback_for(action)
            self.add_item(button)

    def callback_for(self, action: str):
        async def callback(interaction: discord.Interaction):
            await handle_workflow_action(interaction, self.record_id, action)
        return callback

async def refresh_workflow(interaction: discord.Interaction, record_id: int):
    row = await verification_service.get_record(record_id)
    if row is None:
        return
    member = interaction.guild.get_member(row.member_id)
    if member is None:
        try:
            member = await interaction.guild.fetch_member(row.member_id)
        except discord.HTTPException:
            return
    await interaction.response.edit_message(embed=progress_embed(member, row), view=WorkflowView(record_id))

async def handle_workflow_action(interaction: discord.Interaction, record_id: int, action: str):
    if not isinstance(interaction.user, discord.Member) or not await is_verification_team(interaction.user):
        return await interaction.response.send_message("Verification Team role required.", ephemeral=True)
    if action in ("male", "female"):
        await verification_service.set_answer(record_id, gender=action)
        return await refresh_workflow(interaction, record_id)
    if action == "yes":
        return await interaction.response.send_modal(OtherStaffModal(record_id))
    if action == "no":
        await verification_service.set_answer(record_id, other_staff=False)
        return await refresh_workflow(interaction, record_id)

    async with _record_lock(record_id):
        row = await verification_service.get_record(record_id)
        if row is None or row.status != "in_progress":
            return await interaction.response.send_message("Verification is no longer active.", ephemeral=True)
        if action == "verify" and (row.gender is None or row.is_other_server_staff is None):
            return await interaction.response.send_message("Choose a gender and answer the staff question first.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        member = guild.get_member(row.member_id)
        if member is None:
            try:
                member = await guild.fetch_member(row.member_id)
            except discord.HTTPException:
                return await interaction.followup.send("Member has left the server.", ephemeral=True)
        if member.id == interaction.user.id:
            return await interaction.followup.send("You cannot verify yourself.", ephemeral=True)

        if action == "verify":
            keys = ("role_unverified", "role_verified", "role_male" if row.gender == "male" else "role_female")
            roles = []
            for key in keys:
                role_id = await config_service.get_setting(guild.id, key)
                role = guild.get_role(role_id) if role_id else None
                if role is None:
                    return await interaction.followup.send(f"Configure {key} with /config set-role first.", ephemeral=True)
                roles.append(role)
            bot_member = guild.me
            if not bot_member or not bot_member.guild_permissions.manage_roles or any(bot_member.top_role <= role for role in roles):
                return await interaction.followup.send("Bot needs Manage Roles and a role above the verification roles.", ephemeral=True)
            try:
                await member.remove_roles(roles[0], reason=f"Verified by {interaction.user} ({interaction.user.id})")
                await member.add_roles(roles[1], roles[2], reason=f"Verified by {interaction.user} ({interaction.user.id})")
            except discord.HTTPException as exc:
                return await interaction.followup.send(f"Role update failed: {exc}", ephemeral=True)
            saved = await verification_service.finish(record_id, "verified", staff_id=interaction.user.id)
            if saved is None:
                return await interaction.followup.send("Verification was already completed.", ephemeral=True)
            amount = await config_service.get_setting(guild.id, "staff_xp_verify") or 0
            awarded = await add_verification_xp(
                guild_id=guild.id, staff_id=interaction.user.id, target_id=member.id,
                verification_record_id=record_id, amount=amount,
            )
            await send_log(guild, LogEvent(
                LogType.VERIFICATION, "✅ Member Verified", user=member, staff=interaction.user,
                xp_change=amount if awarded else None,
                fields=[("Gender", row.gender.title(), True),
                        ("Staff elsewhere", "Yes" if row.is_other_server_staff else "No", True),
                        ("Other server", row.other_server_name or "—", False)],
                ids={"Verification record": record_id},
            ))
            return await interaction.followup.send(f"Verified {member.mention}; roles updated. Staff XP: {amount if awarded else 0}.", ephemeral=True)

        if action == "reject":
            saved = await verification_service.finish(record_id, "rejected", "Rejected by Verification Team", staff_id=interaction.user.id)
            if saved is None:
                return await interaction.followup.send("Verification was already completed.", ephemeral=True)
            await send_log(guild, LogEvent(LogType.VERIFICATION, "❌ Verification Rejected", user=member, staff=interaction.user, reason=saved.reason, ids={"Verification record": record_id}))
            return await interaction.followup.send(f"Rejected. {member.mention} remains Unverified.", ephemeral=True)

        if action == "ban":
            try:
                await guild.ban(member, reason=f"Verification ban by {interaction.user} ({interaction.user.id})")
            except discord.HTTPException as exc:
                return await interaction.followup.send(f"Ban failed: {exc}", ephemeral=True)
            saved = await verification_service.finish(record_id, "banned", "Verification Team ban", staff_id=interaction.user.id)
            if saved:
                await send_log(guild, LogEvent(LogType.VERIFICATION, "🔨 Member Banned", user=member, staff=interaction.user, reason=saved.reason, ids={"Verification record": record_id}))
            return await interaction.followup.send(f"Banned {member.mention}.", ephemeral=True)

class VerificationCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    verification = app_commands.Group(name="verification", description="Verification workflow", guild_only=True)

    @verification.command(name="panel", description="Post the Verification Team panel")
    @requires_level(StaffLevel.ADMIN)
    async def panel(self, interaction: discord.Interaction):
        channel_id = await config_service.get_setting(interaction.guild_id, "verification_panel_channel")
        channel = interaction.guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("Configure verification_panel_channel first with /config set-channel.", ephemeral=True)
        if not await config_service.get_setting(interaction.guild_id, "role_verification_team"):
            return await interaction.response.send_message("Configure role_verification_team first with /config set-role.", ephemeral=True)
        embed = discord.Embed(
            title="🐉 VALYRIA VERIFICATION",
            description="Verification Team: select an unverified member to begin. Only members with Unverified appear.",
            color=discord.Color.blurple(),
        )
        await channel.send(embed=embed, view=VerificationPanelView())
        await interaction.response.send_message(f"Panel posted in {channel.mention}.", ephemeral=True)

    @verification.command(name="status", description="Show a member's latest verification record")
    @app_commands.describe(member="Member to inspect")
    @requires_level(StaffLevel.STAFF)
    async def status(self, interaction: discord.Interaction, member: discord.Member):
        async with async_session() as session:
            row = await session.scalar(select(VerificationRecord).where(
                VerificationRecord.guild_id == interaction.guild_id,
                VerificationRecord.member_id == member.id,
            ).order_by(VerificationRecord.id.desc()))
        if row is None:
            return await interaction.response.send_message("No verification record exists.", ephemeral=True)
        await interaction.response.send_message(embed=progress_embed(member, row), ephemeral=True)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        try:
            changed = await verification_service.ensure_unverified(member)
            if changed:
                await send_log(member.guild, LogEvent(LogType.VERIFICATION, "🆕 Unverified role assigned", user=member, ids={"User ID": member.id}))
        except Exception as exc:
            await send_log(member.guild, LogEvent(LogType.VERIFICATION, "⚠️ Could not assign Unverified role", user=member, reason=str(exc), ids={"User ID": member.id}))

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if after.channel is None or before.channel == after.channel:
            return
        voice_id = await config_service.get_setting(member.guild.id, "verification_voice")
        unverified_id = await config_service.get_setting(member.guild.id, "role_unverified")
        if not voice_id or after.channel.id != voice_id or not unverified_id:
            return
        if all(role.id != unverified_id for role in member.roles):
            return
        if not await config_service.get_setting(member.guild.id, "verification_dm_enabled"):
            return
        team_id = await config_service.get_setting(member.guild.id, "role_verification_team")
        if not team_id:
            return
        cooldown = await config_service.get_setting(member.guild.id, "verification_request_cooldown") or 300
        team = [m for m in member.guild.members if not m.bot and any(r.id == team_id for r in m.roles)]
        if not team:
            try:
                async for candidate in member.guild.fetch_members(limit=None):
                    if not candidate.bot and any(r.id == team_id for r in candidate.roles):
                        team.append(candidate)
            except discord.HTTPException:
                return
        if not team:
            return
        record_id = await verification_service.create_waiting_if_due(member.guild.id, member.id, cooldown)
        if record_id is None:
            return
        embed = discord.Embed(title="🔔 New Verification Request", description=f"{member.mention} is waiting for verification.", color=discord.Color.orange())
        embed.add_field(name="User ID", value=str(member.id), inline=True)
        embed.add_field(name="Voice Channel", value=after.channel.mention, inline=True)
        delivered = 0
        for reviewer in team:
            try:
                await reviewer.send(embed=embed)
                delivered += 1
            except discord.HTTPException:
                continue
        await send_log(member.guild, LogEvent(
            LogType.VERIFICATION, "🔔 Verification requested", user=member,
            fields=[("DMs delivered", str(delivered), True)],
            ids={"Verification record": record_id, "Voice channel": after.channel.id},
        ))

async def setup(bot: commands.Bot):
    bot.add_view(VerificationPanelView())
    async with async_session() as session:
        rows = await session.scalars(select(VerificationRecord.id).where(VerificationRecord.status == "in_progress"))
        for record_id in rows:
            bot.add_view(WorkflowView(record_id))
    await bot.add_cog(VerificationCog(bot))
