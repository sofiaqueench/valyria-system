"""Staff XP progress command, backed by the existing XP ledger and rank service."""
import discord
from discord import app_commands
from discord.ext import commands

from bot.core.permissions import StaffLevel, requires_level
from bot.services import config_service
from bot.services.staff_promotion_service import (
    get_rank_config, get_staff_xp_total, get_promotion_state,
    is_rank_config_complete, on_staff_xp_awarded,
)
from bot.services.staff_xp_service import get_latest_staff_xp_change
from bot.services.staff_warn_service import issue_staff_warning


class StaffXPCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="staff-xp", description="View your Staff XP and promotion progress")
    @app_commands.guild_only()
    @requires_level(StaffLevel.STAFF)
    async def staff_xp(self, interaction: discord.Interaction):
        member = interaction.user
        await on_staff_xp_awarded(member, amount=0, source="progress check")
        try:
            member = await interaction.guild.fetch_member(member.id)
        except discord.HTTPException:
            pass
        xp = await get_staff_xp_total(interaction.guild_id, member.id)
        latest_change = await get_latest_staff_xp_change(interaction.guild_id, member.id)
        ranks = await get_rank_config(interaction.guild_id)
        state = await get_promotion_state(interaction.guild_id, member.id)
        missing_roles = [rank.rank for rank in ranks.values() if interaction.guild.get_role(rank.role_id) is None]
        rank_by_role = {rank.role_id: rank for rank in ranks.values()}
        held = [rank for role in member.roles if (rank := rank_by_role.get(role.id)) is not None]
        current = max(held, key=lambda rank: rank.rank) if held else (
            ranks.get(state.rank) if state else None
        )
        current_number = current.rank if current else (state.rank if state else 0)
        next_rank = ranks.get(current_number + 1)

        embed = discord.Embed(
            title="👑 Staff Progress",
            color=discord.Color.gold() if current and current.rank_type == "high_staff" else discord.Color.blurple(),
        )
        embed.add_field(name="Staff XP", value=f"{xp:,} XP", inline=True)
        if latest_change is not None:
            source = "Staff Warning" if latest_change.action == "staff_warn" else latest_change.action.replace("_", " ").title()
            embed.add_field(name="Last XP Change", value=f"{latest_change.xp_amount:+,} XP · {source}", inline=True)
        current_role = interaction.guild.get_role(current.role_id) if current else None
        if current_role is not None:
            role_status = "" if any(role.id == current_role.id for role in member.roles) else " · role not currently assigned"
            type_label = "High Staff" if current.rank_type == "high_staff" else "Staff"
            current_text = f"{current_role.mention} · {type_label} · Rank {current.rank}{role_status}"
        elif current_number:
            current_text = f"Rank {current_number} (configured role is missing)"
        else:
            current_text = "No promotion rank yet"
        embed.add_field(name="Current Rank", value=current_text, inline=True)

        if next_rank is not None:
            next_role = interaction.guild.get_role(next_rank.role_id)
            next_text = next_role.mention if next_role else f"Rank {next_rank.rank} (role missing)"
            remaining = max(0, next_rank.required_xp - xp)
            embed.add_field(name="Next Rank", value=next_text, inline=True)
            embed.add_field(name="Required XP", value=f"{next_rank.required_xp:,}", inline=True)
            embed.add_field(name="XP Remaining", value=f"{remaining:,}", inline=True)
            lower_threshold = current.required_xp if current else 0
            span = max(1, next_rank.required_xp - lower_threshold)
            progress = min(1.0, max(0.0, (xp - lower_threshold) / span))
            filled = int(progress * 10)
            embed.add_field(name="Progress", value=f"{'█' * filled}{'░' * (10 - filled)} {progress:.0%}", inline=False)
        elif current_number >= 11:
            embed.add_field(name="Next Rank", value="Maximum configured rank reached", inline=False)
        if not is_rank_config_complete(ranks) or missing_roles:
            missing = sorted(set(range(1, 12)) - set(ranks))
            missing_text = f"Missing rank slots: {', '.join(map(str, missing)) or 'none'}"
            if missing_roles:
                missing_text += f" · missing/deleted roles: {', '.join(map(str, missing_roles))}"
            embed.add_field(
                name="Promotion Setup",
                value=f"Promotions are inactive until rank setup is valid. {missing_text}.",
                inline=False,
            )
        footer = "Staff XP is cumulative and is not reset on promotion."
        if not is_rank_config_complete(ranks) or missing_roles:
            footer += " Promotions remain inactive until all 11 ranks are configured."
        embed.set_footer(text=footer)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="staff-warn", description="Issue a Staff warning and apply its configured XP penalty")
    @app_commands.guild_only()
    @app_commands.describe(member="Staff member to warn", reason="Reason for the Staff warning")
    async def staff_warn(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("This command is only available in a server.", ephemeral=True)
            return
        if member.bot:
            await interaction.response.send_message("Bots cannot receive Staff warnings.", ephemeral=True)
            return
        penalty = int(await config_service.get_setting(interaction.guild_id, "staff_warn_xp_penalty") or 0)
        if penalty < 0:
            await interaction.response.send_message("The configured Staff Warn penalty is invalid.", ephemeral=True)
            return
        try:
            result = await issue_staff_warning(
                member, interaction.user, reason=reason.strip(), penalty=penalty,
                interaction_id=interaction.id,
            )
        except PermissionError as exc:
            await interaction.response.send_message(f"⛔ {exc}", ephemeral=True)
            return
        except (ValueError, RuntimeError) as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return
        old_rank = result["previous_rank"]
        new_rank = result["current_rank"]
        rank_line = ""
        if old_rank and new_rank and old_rank.rank != new_rank.rank:
            rank_line = f"\nRank: **{old_rank.rank} → {new_rank.rank}**"
        elif old_rank and new_rank:
            rank_line = f"\nRank: **{old_rank.rank}** (unchanged)"
        elif old_rank and not result["rank_updated"]:
            rank_line = "\nRank: configuration incomplete; rank reconciliation is inactive."
        await interaction.response.send_message(
            f"⚠️ **Staff Warning Issued**\n\nMember: {member.mention}\nReason: {reason.strip()}\n"
            f"XP Penalty: **-{result['penalty']:,} XP**\n"
            f"Previous Staff XP: **{result['xp_before']:,} XP**\n"
            f"Current Staff XP: **{result['xp_after']:,} XP**{rank_line}",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StaffXPCog(bot))
