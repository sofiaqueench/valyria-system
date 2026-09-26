"""Persistent owner controls and short-lived selection/modals for Temp Voice."""
from __future__ import annotations

import discord


class TempVoiceOwnerView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Call Staff", emoji="📞", style=discord.ButtonStyle.primary, custom_id="temp_voice:call", row=0)
    async def call_staff(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.dispatch_panel_action(interaction, "call")

    @discord.ui.button(label="Kick Member", emoji="🚫", style=discord.ButtonStyle.secondary, custom_id="temp_voice:kick", row=0)
    async def kick(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.dispatch_panel_action(interaction, "kick")

    @discord.ui.button(label="Rename Room", emoji="✏️", style=discord.ButtonStyle.secondary, custom_id="temp_voice:rename", row=0)
    async def rename(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.dispatch_panel_action(interaction, "rename")

    @discord.ui.button(label="User Limit", emoji="👥", style=discord.ButtonStyle.secondary, custom_id="temp_voice:limit", row=1)
    async def limit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.dispatch_panel_action(interaction, "limit")

    @discord.ui.button(label="Lock Room", emoji="🔒", style=discord.ButtonStyle.secondary, custom_id="temp_voice:lock", row=1)
    async def lock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.dispatch_panel_action(interaction, "lock")

    @discord.ui.button(label="Unlock Room", emoji="🔓", style=discord.ButtonStyle.secondary, custom_id="temp_voice:unlock", row=1)
    async def unlock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.dispatch_panel_action(interaction, "unlock")

    @discord.ui.button(label="Transfer Ownership", emoji="👑", style=discord.ButtonStyle.primary, custom_id="temp_voice:transfer", row=2)
    async def transfer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.dispatch_panel_action(interaction, "transfer")

    @discord.ui.button(label="Ban Member", emoji="🔨", style=discord.ButtonStyle.danger, custom_id="temp_voice:ban", row=2)
    async def ban(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.dispatch_panel_action(interaction, "ban")

    @discord.ui.button(label="Give Access", emoji="✅", style=discord.ButtonStyle.success, custom_id="temp_voice:give_access", row=3)
    async def give_access(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.dispatch_panel_action(interaction, "give_access")

    @discord.ui.button(label="Remove Access", emoji="❌", style=discord.ButtonStyle.danger, custom_id="temp_voice:remove_access", row=3)
    async def remove_access(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.dispatch_panel_action(interaction, "remove_access")


class TempVoiceMemberSelectView(discord.ui.View):
    def __init__(self, cog, channel_id: int, action: str, members: list[discord.Member]):
        super().__init__(timeout=120)
        self.cog = cog
        self.channel_id = channel_id
        placeholder = "Select a matching server member" if action in {"give_access", "ban"} else (
            "Select a member currently in this room"
        )
        options = [
            discord.SelectOption(
                label=(f"{member.display_name} (@{member.name})" if action in {"give_access", "ban"} else member.display_name)[:100],
                value=str(member.id),
            )
            for member in members[:25]
        ]
        self.add_item(TempVoiceMemberSelect(channel_id, action, options, cog, placeholder=placeholder))


class TempVoiceMemberSelect(discord.ui.Select):
    def __init__(
        self, channel_id: int, action: str, options: list[discord.SelectOption], cog,
        *, placeholder: str = "Select a member currently in this room",
    ):
        super().__init__(
            placeholder=placeholder,
            min_values=1, max_values=1, options=options,
            custom_id=f"temp_voice:{action}:{channel_id}",
        )
        self.channel_id = channel_id
        self.action = action
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        await self.cog.safe_select_action(
            interaction, self.channel_id, self.action, int(self.values[0])
        )


class TempVoiceSearchView(discord.ui.View):
    def __init__(self, cog, channel_id: int, action: str):
        super().__init__(timeout=120)
        self.cog = cog
        self.channel_id = channel_id
        self.action = action
        self.add_item(TempVoiceGuildUserSelect(cog, channel_id, action))

    @discord.ui.button(label="Search by name or ID", style=discord.ButtonStyle.secondary, row=1)
    async def search_member(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            TempVoiceMemberSearchModal(self.cog, self.channel_id, self.action)
        )


class TempVoiceGuildUserSelect(discord.ui.UserSelect):
    def __init__(self, cog, channel_id: int, action: str):
        super().__init__(
            placeholder="Search this server by username or display name",
            min_values=1, max_values=1,
            custom_id=f"temp_voice:{action}:search:{channel_id}",
        )
        self.cog = cog
        self.channel_id = channel_id
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        target = self.values[0]
        if not isinstance(target, discord.Member):
            return await self.cog.reply(interaction, "That user is not a member of this server.", ephemeral=True)
        await self.cog.safe_select_action(interaction, self.channel_id, self.action, target.id)


class TempVoiceMemberSearchModal(discord.ui.Modal, title="Search Server Members"):
    query = discord.ui.TextInput(
        label="Username, display name, or User ID", placeholder="alex or 123456789012345678",
        min_length=1, max_length=100, required=True,
    )

    def __init__(self, cog, channel_id: int, action: str):
        super().__init__(custom_id=f"temp_voice:{action}:search_input:{channel_id}", timeout=180)
        self.cog = cog
        self.channel_id = channel_id
        self.action = action

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.search_temp_voice_member(
            interaction, self.channel_id, self.action, str(self.query.value),
        )


class TempVoiceRenameModal(discord.ui.Modal, title="Rename Temporary Room"):
    name = discord.ui.TextInput(
        label="New room name", placeholder="Valyria Chamber", min_length=1,
        max_length=100, required=True,
    )

    def __init__(self, cog, channel_id: int):
        super().__init__(custom_id=f"temp_voice:rename:{channel_id}", timeout=180)
        self.cog = cog
        self.channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.safe_modal_action(interaction, self.channel_id, "rename", str(self.name.value))


class TempVoiceLimitModal(discord.ui.Modal, title="Set Room User Limit"):
    limit = discord.ui.TextInput(
        label="User limit (0 = unlimited)", placeholder="0–99", min_length=1,
        max_length=2, required=True,
    )

    def __init__(self, cog, channel_id: int):
        super().__init__(custom_id=f"temp_voice:limit:{channel_id}", timeout=180)
        self.cog = cog
        self.channel_id = channel_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.safe_modal_action(interaction, self.channel_id, "limit", str(self.limit.value))
