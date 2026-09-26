"""
أوامر /config: عرض وتعديل إعدادات السيرفر (الرومات، الرتب، القيم...).
كل الأوامر تعمل عبر config_service، والمفاتيح معرّفة في bot/config/schema.py.
الأوامر للمسؤولين فقط حاليًا — سيُستبدل هذا بنظام Permissions في Phase 6.
معالجة الأخطاء في bot/core/error_handler.py.
"""
import json

import discord
from discord import app_commands
from discord.ext import commands

from bot.config.game_registry import GAMES
from bot.config.schema import (
    CHANNEL_KINDS,
    GROUPS,
    ID_KINDS,
    SETTINGS,
    VALUE_KINDS,
    Kind,
    SettingDef,
    get_definition,
)
from bot.core.errors import ConfigError
from bot.core.permissions import StaffLevel, requires_level
from bot.services import config_service
from bot.services.level_progression_service import get_level_role_mapping
from bot.services.logging_service import LogEvent, LogType, send_log
from bot.services.staff_promotion_service import (
    RANK_COUNT, get_rank_config, is_rank_config_complete, remove_rank_config, set_rank_config,
)

# نوع الروم المقبول لكل نوع إعداد
_CHANNEL_CLASSES = {
    Kind.TEXT_CHANNEL: discord.TextChannel,
    Kind.VOICE_CHANNEL: discord.VoiceChannel,
    Kind.CATEGORY: discord.CategoryChannel,
}
_CHANNEL_LABELS = {
    Kind.TEXT_CHANNEL: "روم نصي",
    Kind.VOICE_CHANNEL: "روم صوتي",
    Kind.CATEGORY: "كاتيغوري",
}

_GROUP_CHOICES = [app_commands.Choice(name=label, value=key) for key, label in GROUPS.items()]
_LEVELING_SETTINGS = {
    "message_xp_min", "message_xp_max", "message_xp_cooldown", "voice_xp_interval",
    "voice_xp_amount", "voice_xp_ignore_deafened", "level_up_enabled", "level_up_banner",
    "level_up_emoji",
}
_TEMP_VOICE_SETTINGS = {
    "temp_voice_default_name", "temp_voice_default_limit", "temp_voice_delete_delay",
    "temp_voice_panel_enabled", "temp_voice_panel_staff_override",
    "temp_voice_staff_call_cooldown", "temp_voice_banner",
}


def format_value(definition: SettingDef, value: int | bool | str | None) -> str:
    """يحوّل قيمة الإعداد لنص جميل للعرض في Discord."""
    if value is None:
        return "— غير مضبوط"
    if definition.kind in CHANNEL_KINDS:
        return f"<#{value}>"
    if definition.kind is Kind.ROLE:
        return f"<@&{value}>"
    if definition.kind is Kind.BOOL:
        return "✅ مفعّل" if value else "❌ معطّل"
    if definition.key == "staff_rank_config":
        return "Use /config staff-rank view"
    text = str(value)
    return f"`{text[:60]}{'…' if len(text) > 60 else ''}`"


def _autocomplete_choices(kinds: frozenset[Kind] | set[Kind], current: str) -> list[app_commands.Choice[str]]:
    current = current.lower().strip()
    choices = []
    for definition in SETTINGS.values():
        if definition.kind not in kinds:
            continue
        if definition.key in {"level_roles", "staff_rank_config"}:
            continue
        if current and current not in definition.key and current not in definition.description:
            continue
        label = f"{definition.key} — {definition.description}"
        choices.append(app_commands.Choice(name=label[:100], value=definition.key))
        if len(choices) == 25:  # الحد الأقصى في Discord
            break
    return choices


class ConfigCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    config = app_commands.Group(
        name="config",
        description="إعدادات البوت لهذا السيرفر",
        guild_only=True,
    )
    staff_rank = app_commands.Group(
        name="staff-rank", description="Configure the 11 Staff promotion ranks", parent=config,
    )

    # ------------------------------------------------------------------ view
    @config.command(name="view", description="عرض إعدادات مجموعة معيّنة")
    @app_commands.describe(group="المجموعة التي تريد عرضها")
    @app_commands.choices(group=_GROUP_CHOICES)
    @requires_level(StaffLevel.ADMIN)
    async def view(self, interaction: discord.Interaction, group: app_commands.Choice[str]):
        states = await config_service.list_settings(interaction.guild_id, group.value)

        lines = []
        for state in states:
            mark = "" if state.is_default else " ✏️"
            lines.append(f"`{state.definition.key}` → {format_value(state.definition, state.value)}{mark}")

        embed = discord.Embed(
            title=f"⚙️ {GROUPS[group.value]}",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="✏️ = تم تغييرها عن القيمة الافتراضية")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @staff_rank.command(name="set", description="Set a Staff promotion rank, role, type, and XP threshold")
    @app_commands.describe(
        rank="Promotion slot from 1 to 11", role="Role assigned at this rank",
        rank_type="Staff or High Staff", required_xp="Cumulative Staff XP required",
    )
    @app_commands.choices(rank_type=[
        app_commands.Choice(name="Staff", value="staff"),
        app_commands.Choice(name="High Staff", value="high_staff"),
    ])
    @requires_level(StaffLevel.ADMIN)
    async def staff_rank_set(
        self, interaction: discord.Interaction,
        rank: app_commands.Range[int, 1, RANK_COUNT],
        role: discord.Role,
        rank_type: app_commands.Choice[str],
        required_xp: app_commands.Range[int, 1, 2_000_000_000],
    ):
        if role.is_default():
            raise ConfigError("@everyone cannot be a Staff promotion role.")
        try:
            ranks = await set_rank_config(
                interaction.guild_id, rank, role.id, rank_type.value, required_xp,
                updated_by=interaction.user.id,
            )
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        message = f"✅ Rank {rank} set to {role.mention} ({rank_type.name}) at {required_xp:,} Staff XP."
        bot_member = interaction.guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_roles or bot_member.top_role <= role:
            message += " ⚠️ The bot must have Manage Roles and its highest role must be above this role."
        if not is_rank_config_complete(ranks):
            missing = sorted(set(range(1, RANK_COUNT + 1)) - set(ranks))
            message += f" Promotion remains inactive until all 11 ranks are configured; missing: {', '.join(map(str, missing))}."
        await send_log(interaction.guild, LogEvent(
            LogType.XP, "⚙️ Staff Promotion Rank Configured", staff=interaction.user,
            fields=[("Rank", str(rank), True), ("Role", role.mention, True),
                    ("Type", rank_type.name, True), ("Required XP", f"{required_xp:,}", True)],
        ))
        await interaction.response.send_message(message, ephemeral=True)

    @staff_rank.command(name="remove", description="Remove a Staff promotion rank configuration")
    @app_commands.describe(rank="Rank slot to remove")
    @requires_level(StaffLevel.ADMIN)
    async def staff_rank_remove(
        self, interaction: discord.Interaction, rank: app_commands.Range[int, 1, RANK_COUNT],
    ):
        try:
            ranks = await remove_rank_config(
                interaction.guild_id, rank, updated_by=interaction.user.id,
            )
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        await send_log(interaction.guild, LogEvent(
            LogType.XP, "⚙️ Staff Promotion Rank Removed", staff=interaction.user,
            fields=[("Rank", str(rank), True)],
        ))
        missing = sorted(set(range(1, RANK_COUNT + 1)) - set(ranks))
        await interaction.response.send_message(
            f"✅ Rank {rank} configuration removed. Promotions require all 11 ranks; missing: {', '.join(map(str, missing))}.",
            ephemeral=True,
        )

    @staff_rank.command(name="view", description="View configured Staff and High Staff promotion ranks")
    @requires_level(StaffLevel.ADMIN)
    async def staff_rank_view(self, interaction: discord.Interaction):
        ranks = await get_rank_config(interaction.guild_id)
        lines = []
        for number in range(1, RANK_COUNT + 1):
            item = ranks.get(number)
            if item is None:
                lines.append(f"**Rank {number}:** Not configured")
                continue
            role = interaction.guild.get_role(item.role_id)
            mention = role.mention if role else f"Missing role `{item.role_id}`"
            type_label = "High Staff" if item.rank_type == "high_staff" else "Staff"
            lines.append(f"**Rank {number}:** {mention} · {type_label} · {item.required_xp:,} XP")
        complete = is_rank_config_complete(ranks)
        missing_roles = [rank.rank for rank in ranks.values() if interaction.guild.get_role(rank.role_id) is None]
        embed = discord.Embed(
            title="🛡️ Staff Promotion System",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        staff_channel_id = await config_service.get_setting(interaction.guild_id, "staff_promotion_channel")
        high_channel_id = await config_service.get_setting(interaction.guild_id, "high_staff_promotion_channel")
        embed.add_field(name="Staff Promotion Channel", value=f"<#{staff_channel_id}>" if staff_channel_id else "Not configured", inline=True)
        embed.add_field(name="High Staff Promotion Channel", value=f"<#{high_channel_id}>" if high_channel_id else "Not configured", inline=True)
        if missing_roles:
            footer = f"Missing/deleted Discord roles for rank(s): {', '.join(map(str, missing_roles))}"
        elif complete:
            footer = "All 11 ranks configured; a missing announcement channel does not block promotions."
        else:
            footer = "Incomplete — promotions are inactive until all 11 ranks are configured"
        embed.set_footer(text=footer)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ----------------------------------------------------------- set-channel
    @config.command(name="set-channel", description="ضبط إعداد من نوع روم أو كاتيغوري")
    @app_commands.describe(key="اسم الإعداد", channel="الروم أو الكاتيغوري")
    @requires_level(StaffLevel.ADMIN)
    async def set_channel(
        self, interaction: discord.Interaction, key: str, channel: discord.abc.GuildChannel
    ):
        definition = get_definition(key)
        if definition.kind not in CHANNEL_KINDS:
            raise ConfigError(f"«{key}» ليس إعداد روم. استعمل /config set-role أو /config set-value.")

        expected = _CHANNEL_CLASSES[definition.kind]
        if not isinstance(channel, expected):
            raise ConfigError(f"«{key}» يحتاج {_CHANNEL_LABELS[definition.kind]}.")

        await config_service.set_setting(
            interaction.guild_id, key, channel.id, updated_by=interaction.user.id
        )
        if key == "level_up_channel":
            await send_log(interaction.guild, LogEvent(
                LogType.XP, "⚙️ Level-Up Channel Configured", staff=interaction.user,
                fields=[("Channel", channel.mention, True)],
            ))
        if key == "permanent_voice_channel":
            await send_log(interaction.guild, LogEvent(
                LogType.VOICE, "⚙️ Permanent Voice Configuration Changed", staff=interaction.user,
                fields=[("Channel", channel.mention, True)],
            ))
        if key == "permanent_voice_channel":
            voice_cog = self.bot.get_cog("PermanentVoiceCog")
            if voice_cog is not None:
                voice_cog.request_reconnect(interaction.guild)
        if key in {"staff_promotion_channel", "high_staff_promotion_channel", "staff_demotion_channel"}:
            await send_log(interaction.guild, LogEvent(
                LogType.XP, "⚙️ Staff Rank Notification Channel Configured", staff=interaction.user,
                fields=[("Setting", key, True), ("Channel", channel.mention, True)],
            ))
        if any(key in {game.panel_channel_key, game.results_channel_key} for game in GAMES.values()):
            await send_log(interaction.guild, LogEvent(
                LogType.XP, "⚙️ Staff Games Channel Configured", staff=interaction.user,
                fields=[("Setting", key, True), ("Channel", channel.mention, True)],
            ))
        if key in {"temp_voice_create_channel", "temp_voice_category", "temp_voice_staff_alert"}:
            await send_log(interaction.guild, LogEvent(
                LogType.VOICE, "⚙️ Temporary Voice Configuration Changed", staff=interaction.user,
                fields=[("Setting", key, True), ("Channel", channel.mention, True)],
            ))
        await interaction.response.send_message(f"✅ تم ضبط `{key}` على {channel.mention}", ephemeral=True)

    @set_channel.autocomplete("key")
    async def set_channel_key_autocomplete(self, interaction: discord.Interaction, current: str):
        return _autocomplete_choices(CHANNEL_KINDS, current)

    # -------------------------------------------------------------- set-role
    @config.command(name="set-role", description="ضبط إعداد من نوع رتبة")
    @app_commands.describe(key="اسم الإعداد", role="الرتبة")
    @requires_level(StaffLevel.ADMIN)
    async def set_role(self, interaction: discord.Interaction, key: str, role: discord.Role):
        definition = get_definition(key)
        if definition.kind is not Kind.ROLE:
            raise ConfigError(f"«{key}» ليس إعداد رتبة. استعمل /config set-channel أو /config set-value.")
        if role.is_default():
            raise ConfigError("لا يمكن استعمال رتبة @everyone هنا.")

        await config_service.set_setting(
            interaction.guild_id, key, role.id, updated_by=interaction.user.id
        )
        await interaction.response.send_message(f"✅ تم ضبط `{key}` على {role.mention}", ephemeral=True)

    @set_role.autocomplete("key")
    async def set_role_key_autocomplete(self, interaction: discord.Interaction, current: str):
        return _autocomplete_choices({Kind.ROLE}, current)

    # ------------------------------------------------------------- set-value
    @config.command(name="set-value", description="ضبط إعداد رقمي أو نصي أو true/false")
    @app_commands.describe(key="اسم الإعداد", value="القيمة الجديدة")
    @requires_level(StaffLevel.ADMIN)
    async def set_value(self, interaction: discord.Interaction, key: str, value: str):
        if key == "level_roles":
            raise ConfigError("Use `/config level-role` to manage Level Roles safely.")
        if key == "staff_rank_config":
            raise ConfigError("Use `/config staff-rank set` to manage Staff promotion ranks safely.")
        definition = get_definition(key)
        if definition.kind in ID_KINDS:
            raise ConfigError(f"«{key}» إعداد روم/رتبة. استعمل /config set-channel أو /config set-role.")

        parsed = definition.parse_input(value)
        saved = await config_service.set_setting(
            interaction.guild_id, key, parsed, updated_by=interaction.user.id
        )
        if key in _LEVELING_SETTINGS:
            await send_log(interaction.guild, LogEvent(
                LogType.XP, "⚙️ Member Leveling Setting Changed", staff=interaction.user,
                fields=[("Setting", key, True), ("Value", str(saved)[:900], False)],
            ))
        if key in _TEMP_VOICE_SETTINGS:
            await send_log(interaction.guild, LogEvent(
                LogType.VOICE, "⚙️ Temporary Voice Configuration Changed", staff=interaction.user,
                fields=[("Setting", key, True), ("Value", str(saved)[:900], False)],
            ))
        await interaction.response.send_message(
            f"✅ تم ضبط `{key}` على {format_value(definition, saved)}", ephemeral=True
        )

    @config.command(name="game-xp", description="Set Staff XP awarded to the host of a completed game")
    @app_commands.describe(game="Game type", xp="Staff XP awarded to its host")
    @app_commands.choices(game=[app_commands.Choice(name=item.label, value=item.key) for item in GAMES.values()])
    @requires_level(StaffLevel.ADMIN)
    async def game_xp(self, interaction: discord.Interaction, game: app_commands.Choice[str], xp: app_commands.Range[int, 0, 1000000]):
        definition = GAMES[game.value]
        saved = await config_service.set_setting(
            interaction.guild_id, definition.xp_key, xp, updated_by=interaction.user.id,
        )
        await send_log(interaction.guild, LogEvent(
            LogType.XP, "⚙️ Staff Game Host XP Configured", staff=interaction.user,
            fields=[("Game", definition.label, True), ("Staff XP", str(saved), True)],
        ))
        await interaction.response.send_message(
            f"✅ {definition.emoji} {definition.label} host reward set to **{saved:,} Staff XP**.", ephemeral=True,
        )

    @set_value.autocomplete("key")
    async def set_value_key_autocomplete(self, interaction: discord.Interaction, current: str):
        return _autocomplete_choices(VALUE_KINDS, current)

    # ----------------------------------------------------------------- reset
    @config.command(name="reset", description="إرجاع إعداد إلى قيمته الافتراضية")
    @app_commands.describe(key="اسم الإعداد")
    @requires_level(StaffLevel.ADMIN)
    async def reset(self, interaction: discord.Interaction, key: str):
        if key == "staff_rank_config":
            raise ConfigError("Use `/config staff-rank remove` to manage Staff promotion ranks safely.")
        definition = get_definition(key)
        existed = await config_service.reset_setting(interaction.guild_id, key)
        if key == "permanent_voice_channel":
            if existed:
                await send_log(interaction.guild, LogEvent(
                    LogType.VOICE, "⚙️ Permanent Voice Configuration Reset", staff=interaction.user,
                    fields=[("Channel", "Cleared", True)],
                ))
            voice_cog = self.bot.get_cog("PermanentVoiceCog")
            if voice_cog is not None:
                voice_cog.request_reconnect(interaction.guild)
        if existed and (key in _LEVELING_SETTINGS or key in {"level_roles", "level_up_channel"}):
            await send_log(interaction.guild, LogEvent(
                LogType.XP, "⚙️ Member Leveling Setting Reset", staff=interaction.user,
                fields=[("Setting", key, True), ("Default", str(definition.default)[:900], False)],
            ))
        if existed and key in {"staff_promotion_channel", "high_staff_promotion_channel", "staff_demotion_channel"}:
            await send_log(interaction.guild, LogEvent(
                LogType.XP, "⚙️ Staff Rank Notification Channel Reset", staff=interaction.user,
                fields=[("Setting", key, True)],
            ))
        if existed and any(key in {game.panel_channel_key, game.results_channel_key} for game in GAMES.values()):
            await send_log(interaction.guild, LogEvent(
                LogType.XP, "⚙️ Staff Games Channel Reset", staff=interaction.user,
                fields=[("Setting", key, True)],
            ))
        if existed and (key in _TEMP_VOICE_SETTINGS or key in {"temp_voice_create_channel", "temp_voice_category", "temp_voice_staff_alert"}):
            await send_log(interaction.guild, LogEvent(
                LogType.VOICE, "⚙️ Temporary Voice Setting Reset", staff=interaction.user,
                fields=[("Setting", key, True)],
            ))
        if existed:
            message = f"✅ تمت إعادة `{key}` إلى الافتراضي: {format_value(definition, definition.default)}"
        else:
            message = f"ℹ️ `{key}` على القيمة الافتراضية أصلًا."
        await interaction.response.send_message(message, ephemeral=True)

    # ------------------------------------------------------- permanent voice
    @config.command(name="permanent-voice", description="Set or clear the bot's permanent voice channel")
    @app_commands.describe(channel="Voice channel; omit to clear the configuration")
    @requires_level(StaffLevel.ADMIN)
    async def permanent_voice(
        self, interaction: discord.Interaction, channel: discord.VoiceChannel | None = None
    ):
        if channel is None:
            existed = await config_service.reset_setting(interaction.guild_id, "permanent_voice_channel")
            message = "✅ Permanent Voice configuration cleared." if existed else "ℹ️ No Permanent Voice channel was configured."
            channel_text = "Cleared"
        else:
            await config_service.set_setting(
                interaction.guild_id, "permanent_voice_channel", channel.id,
                updated_by=interaction.user.id,
            )
            message = f"✅ Permanent Voice channel set to {channel.mention}."
            bot_member = interaction.guild.me
            if bot_member is None:
                message += " ⚠️ The bot's guild member is unavailable; it will validate permissions after reconnecting."
            else:
                permissions = channel.permissions_for(bot_member)
                if not permissions.view_channel or not permissions.connect:
                    message += " ⚠️ Grant the bot View Channel and Connect in that channel. Speak is not required."
                bot_permissions = bot_member.guild_permissions
                missing = [name for name, present in (
                    ("Manage Channels", bot_permissions.manage_channels),
                    ("Manage Roles", bot_permissions.manage_roles),
                    ("Move Members", bot_permissions.move_members),
                ) if not present]
                if missing:
                    message += " ⚠️ Server permissions also needed: " + ", ".join(missing) + "."
            channel_text = channel.mention

        await send_log(interaction.guild, LogEvent(
            LogType.VOICE, "⚙️ Permanent Voice Configuration Changed",
            staff=interaction.user, fields=[("Channel", channel_text, True)],
        ))
        voice_cog = self.bot.get_cog("PermanentVoiceCog")
        if voice_cog is not None:
            voice_cog.request_reconnect(interaction.guild)
        await interaction.response.send_message(message, ephemeral=True)

    # ------------------------------------------------------------ level-role
    @config.command(name="level-role", description="Configure or remove a Member Level role milestone")
    @app_commands.describe(level="Level milestone", role="Role for that milestone; omit to remove it")
    @requires_level(StaffLevel.ADMIN)
    async def level_role(
        self,
        interaction: discord.Interaction,
        level: app_commands.Range[int, 1, 1_000_000],
        role: discord.Role | None = None,
    ):
        mapping = await get_level_role_mapping(interaction.guild_id)
        previous_role_id = mapping.get(level)
        if role is None:
            if previous_role_id is None:
                return await interaction.response.send_message(f"No Level Role is configured for Level {level}.", ephemeral=True)
            del mapping[level]
            result_text = f"Removed the Level Role mapping for Level {level}."
        else:
            if role.is_default():
                raise ConfigError("@everyone cannot be a Level Role.")
            if level not in mapping and len(mapping) >= 8:
                raise ConfigError("At most 8 Level Role milestones can be configured.")
            if any(existing_level != level and role_id == role.id for existing_level, role_id in mapping.items()):
                raise ConfigError("Each Level Role must be unique; this role is already assigned to another level.")
            mapping[level] = role.id
            result_text = f"Level {level} now uses {role.mention}."
            bot_member = interaction.guild.me
            if (
                bot_member is None
                or not bot_member.guild_permissions.manage_roles
                or bot_member.top_role <= role
            ):
                result_text += " ⚠️ The bot currently cannot manage this role; grant Manage Roles and move its highest role above it."

        stored = json.dumps({str(key): mapping[key] for key in sorted(mapping)}, separators=(",", ":"))
        await config_service.set_setting(
            interaction.guild_id, "level_roles", stored, updated_by=interaction.user.id
        )
        await send_log(interaction.guild, LogEvent(
            LogType.XP, "⚙️ Level Role Configuration Changed", staff=interaction.user,
            fields=[("Level", str(level), True), ("Role", role.mention if role else "Removed", True)],
        ))
        await interaction.response.send_message(f"✅ {result_text}", ephemeral=True)

    @reset.autocomplete("key")
    async def reset_key_autocomplete(self, interaction: discord.Interaction, current: str):
        return _autocomplete_choices(set(Kind), current)


async def setup(bot: commands.Bot):
    await bot.add_cog(ConfigCog(bot))
