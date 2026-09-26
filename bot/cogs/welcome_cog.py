"""Welcome card event handler."""
import discord
from discord.ext import commands

from bot.core.logger import logger
from bot.services import config_service
from bot.services.welcome_service import build_welcome_embed


class WelcomeCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        try:
            if not await config_service.get_setting(member.guild.id, "welcome_enabled"):
                return
            channel_id = await config_service.get_setting(member.guild.id, "welcome_channel")
            channel = member.guild.get_channel(channel_id) if channel_id else None
            if not isinstance(channel, discord.TextChannel):
                logger.warning("Welcome is enabled but welcome_channel is not configured for guild %s.", member.guild.id)
                return
            embed = await build_welcome_embed(member)
            await channel.send(
                content=member.mention,
                embed=embed,
                allowed_mentions=discord.AllowedMentions(users=[member]),
            )
        except (discord.HTTPException, discord.Forbidden):
            logger.exception("Discord rejected welcome message for member %s in guild %s.", member.id, member.guild.id)
        except Exception:
            logger.exception("Failed to build/send welcome message for member %s in guild %s.", member.id, member.guild.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(WelcomeCog(bot))
