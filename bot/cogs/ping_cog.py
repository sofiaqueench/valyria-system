"""
Cog تجريبي بسيط للتأكد أن الأوامر (Slash Commands) تعمل فعليًا.
"""
import discord
from discord import app_commands
from discord.ext import commands


class PingCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="ping", description="يتأكد أن البوت يعمل ويعرض زمن الاستجابة")
    async def ping(self, interaction: discord.Interaction):
        latency_ms = round(self.bot.latency * 1000)
        await interaction.response.send_message(f"🏓 Pong! ({latency_ms}ms)")


async def setup(bot: commands.Bot):
    await bot.add_cog(PingCog(bot))