"""Persistent, game-specific controls and ephemeral Staff host selection."""
import discord

from bot.config.game_registry import GAMES


def panel_embed(game_key: str) -> discord.Embed:
    game = GAMES[game_key]
    embed = discord.Embed(
        title=f"{game.emoji} {game.label.upper()} GAME PANEL",
        description=f"Manage {game.label} games from this Staff-only panel.",
        color=game.color,
    )
    embed.set_footer(text="Valyria • Staff Games")
    return embed


class StaffGamePanelView(discord.ui.View):
    def __init__(self, cog, game_key: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.game_key = game_key
        start = discord.ui.Button(
            label="🎮 Start / Register Game", style=discord.ButtonStyle.success,
            custom_id=f"staff_game:{game_key}:start",
        )
        start.callback = self._start
        submit = discord.ui.Button(
            label="🏆 Submit Result", style=discord.ButtonStyle.primary,
            custom_id=f"staff_game:{game_key}:submit",
        )
        submit.callback = self._submit
        current = discord.ui.Button(
            label="📋 View Current Game", style=discord.ButtonStyle.secondary,
            custom_id=f"staff_game:{game_key}:view",
        )
        current.callback = self._view
        cancel = discord.ui.Button(
            label="❌ Cancel Game", style=discord.ButtonStyle.danger,
            custom_id=f"staff_game:{game_key}:cancel",
        )
        cancel.callback = self._cancel
        self.add_item(start)
        self.add_item(submit)
        self.add_item(current)
        self.add_item(cancel)

    async def _start(self, interaction: discord.Interaction):
        await self.cog.panel_start(interaction, self.game_key)

    async def _submit(self, interaction: discord.Interaction):
        await self.cog.panel_submit(interaction, self.game_key)

    async def _view(self, interaction: discord.Interaction):
        await self.cog.panel_view(interaction, self.game_key)

    async def _cancel(self, interaction: discord.Interaction):
        await self.cog.panel_cancel(interaction, self.game_key)


class StaffGameHostSelect(discord.ui.Select):
    def __init__(self, parent: "StaffGameHostPicker"):
        self.picker = parent
        options = parent.page_options()
        super().__init__(
            placeholder="Select the Staff game host",
            min_values=1, max_values=1, options=options,
            custom_id=f"staff_game:{parent.game_key}:{parent.action}:host:{parent.page}",
        )

    async def callback(self, interaction: discord.Interaction):
        await self.picker.selected(interaction, int(self.values[0]))


class StaffGameHostPicker(discord.ui.View):
    def __init__(
        self, cog, game_key: str, action: str,
        members: list[tuple[discord.Member, str]], *, session_id: int | None = None,
    ):
        super().__init__(timeout=180)
        self.cog = cog
        self.game_key = game_key
        self.action = action
        self.session_id = session_id
        self.members = members
        self.page = 0
        self.select = StaffGameHostSelect(self)
        self.add_item(self.select)
        self.previous_page.disabled = True
        self.next_page.disabled = len(self.members) <= 25

    def page_options(self) -> list[discord.SelectOption]:
        start = self.page * 25
        options = []
        for member, level_name in self.members[start:start + 25]:
            options.append(discord.SelectOption(
                label=f"{member.display_name} (@{member.name})"[:100],
                value=str(member.id), description=level_name[:100],
            ))
        return options

    async def _change_page(self, interaction: discord.Interaction, delta: int):
        if not await self.cog.is_panel_authorized(interaction, self.game_key):
            return await interaction.response.send_message("❌ You do not have permission to use this Game Panel.", ephemeral=True)
        self.page = max(0, min((len(self.members) - 1) // 25, self.page + delta))
        self.select.options = self.page_options()
        self.previous_page.disabled = self.page == 0
        self.next_page.disabled = self.page >= (len(self.members) - 1) // 25
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, row=1)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._change_page(interaction, -1)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._change_page(interaction, 1)

    async def selected(self, interaction: discord.Interaction, member_id: int):
        host = next((member for member, _ in self.members if member.id == member_id), None)
        if host is None:
            return await interaction.response.send_message("That Staff host is no longer available.", ephemeral=True)
        await self.cog.selected_game_host(
            interaction, self.game_key, self.action, host, self.session_id,
        )


class StaffGameResultModal(discord.ui.Modal, title="Submit Game Result"):
    winner = discord.ui.TextInput(
        label="Winner (member, team, or side)",
        placeholder="Player name, Mafia, Red Team…",
        min_length=1, max_length=200, required=True,
    )

    def __init__(self, cog, game_key: str, session_id: int, host_id: int):
        super().__init__(custom_id=f"staff_game:{game_key}:result:{session_id}", timeout=300)
        self.cog = cog
        self.game_key = game_key
        self.session_id = session_id
        self.host_id = host_id

    async def on_submit(self, interaction: discord.Interaction):
        await self.cog.submit_result(
            interaction, self.game_key, self.session_id,
            self.host_id, str(self.winner.value).strip(),
        )
