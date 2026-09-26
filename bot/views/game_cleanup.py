"""Persistent self-service controls for restoring a game nickname and removing configured roles."""
import discord


class GameCleanupView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="🔄 Reset Nickname", style=discord.ButtonStyle.primary, custom_id="game_cleanup:reset_nickname")
    async def reset_nickname(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.reset_nickname(interaction)

    @discord.ui.button(label="🎮 Remove Game Role", style=discord.ButtonStyle.secondary, custom_id="game_cleanup:remove_game_role")
    async def remove_game_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.remove_game_role(interaction)

    @discord.ui.button(label="🎭 Remove Self Role", style=discord.ButtonStyle.secondary, custom_id="game_cleanup:remove_self_role")
    async def remove_self_role(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.remove_self_role(interaction)
