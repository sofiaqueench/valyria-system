"""
أوامر تجريبية لاختبار ConfigService: ضبط وعرض إعدادات السيرفر.
لاحقًا سنوسّع هذا لكل الإعدادات (verification, logs, tickets...).
"""
import discord
from discord import app_commands
from discord.ext import commands

from bot.services.config_service import get_or_create_guild, set_welcome_channel


class ConfigCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="setconfig", description="ضبط قناة الترحيب لهذا السيرفر (اختبار مؤقت)")
    @app_commands.describe(channel="القناة التي سيُرسل فيها الترحيب")
    @app_commands.checks.has_permissions(administrator=True)
    async def setconfig(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await set_welcome_channel(interaction.guild_id, channel.id)
        await interaction.response.send_message(
            f"✅ تم ضبط قناة الترحيب على {channel.mention}"
        )

    @app_commands.command(name="showconfig", description="عرض إعدادات هذا السيرفر الحالية")
    async def showconfig(self, interaction: discord.Interaction):
        guild_config = await get_or_create_guild(interaction.guild_id)

        welcome = (
            f"<#{guild_config.welcome_channel_id}>"
            if guild_config.welcome_channel_id
            else "غير مضبوطة"
        )

        embed = discord.Embed(title="⚙️ إعدادات السيرفر", color=discord.Color.blurple())
        embed.add_field(name="قناة الترحيب", value=welcome, inline=False)
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(ConfigCog(bot))