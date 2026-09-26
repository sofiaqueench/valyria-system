"""Invite cache lifecycle and staff-only invite statistics command."""
import discord
from discord import app_commands
from discord.ext import commands

from bot.core.permissions import StaffLevel, requires_level
from bot.services.invite_service import InviteTracker, get_invite_stats


class InviteCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.tracker = InviteTracker(bot)

    async def process_member_join(self, member: discord.Member) -> None:
        """Called by the existing join listener in LogEventsCog."""
        await self.tracker.process_member_join(member)

    @commands.Cog.listener()
    async def on_ready(self):
        for guild in self.bot.guilds:
            await self.tracker.refresh_guild(guild)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self.tracker.refresh_guild(guild)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        self.tracker.forget_guild(guild.id)

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        await self.tracker.on_invite_create(invite)

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        await self.tracker.on_invite_delete(invite)

    @app_commands.command(name="invites", description="Show invite attributions and invite Staff XP")
    @app_commands.guild_only()
    @app_commands.describe(member="Staff member whose invite statistics to show (defaults to you)")
    @requires_level(StaffLevel.STAFF)
    async def invites(self, interaction: discord.Interaction, member: discord.Member | None = None):
        target = member or interaction.user
        total, xp, codes = await get_invite_stats(interaction.guild_id, target.id)
        embed = discord.Embed(
            title=f"🎟️ Invite statistics — {target.display_name}",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Attributed members", value=str(total), inline=True)
        embed.add_field(name="Invite Staff XP", value=str(xp), inline=True)
        embed.add_field(
            name="Invite codes",
            value="\n".join(f"`{code}` — {count} member(s)" for code, count in codes) or "No attributed invites yet.",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(InviteCog(bot))
