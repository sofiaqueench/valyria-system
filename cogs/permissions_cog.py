# bot/cogs/permissions_cog.py
import discord
from discord import app_commands
from discord.ext import commands
from bot.core.permissions import StaffLevel, get_staff_level, level_label, requires_level


class PermissionsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    perms = app_commands.Group(name="perms", description="أدوات نظام الصلاحيات", guild_only=True)

    @app_commands.command(name="whoami", description="عرض مستوى الطاقم الخاص بك في هذا السيرفر")
    @app_commands.guild_only()
    async def whoami(self, interaction: discord.Interaction):
        level = await get_staff_level(interaction.user)
        await interaction.response.send_message(
            f"مستواك الحالي في **{interaction.guild.name}**: **{level_label(level)}**", ephemeral=True
        )

    @perms.command(name="check", description="عرض مستوى الطاقم لعضو آخر")
    @app_commands.describe(member="العضو الذي تريد معرفة مستواه")
    @requires_level(StaffLevel.STAFF)
    async def check(self, interaction: discord.Interaction, member: discord.Member):
        level = await get_staff_level(member)
        await interaction.response.send_message(f"مستوى {member.mention}: **{level_label(level)}**", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(PermissionsCog(bot))