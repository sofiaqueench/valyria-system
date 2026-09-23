"""
أوامر /config: عرض وتعديل إعدادات السيرفر (الرومات، الرتب، القيم...).
كل الأوامر تعمل عبر config_service، والمفاتيح معرّفة في bot/config/schema.py.
الأوامر للمسؤولين فقط حاليًا — سيُستبدل هذا بنظام Permissions في Phase 6.
معالجة الأخطاء في bot/core/error_handler.py.
"""
import discord
from discord import app_commands
from discord.ext import commands

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
from bot.services import config_service

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
    text = str(value)
    return f"`{text[:60]}{'…' if len(text) > 60 else ''}`"


def _autocomplete_choices(kinds: frozenset[Kind] | set[Kind], current: str) -> list[app_commands.Choice[str]]:
    current = current.lower().strip()
    choices = []
    for definition in SETTINGS.values():
        if definition.kind not in kinds:
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
        default_permissions=discord.Permissions(administrator=True),
    )

    # ------------------------------------------------------------------ view
    @config.command(name="view", description="عرض إعدادات مجموعة معيّنة")
    @app_commands.describe(group="المجموعة التي تريد عرضها")
    @app_commands.choices(group=_GROUP_CHOICES)
    @app_commands.checks.has_permissions(administrator=True)
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

    # ----------------------------------------------------------- set-channel
    @config.command(name="set-channel", description="ضبط إعداد من نوع روم أو كاتيغوري")
    @app_commands.describe(key="اسم الإعداد", channel="الروم أو الكاتيغوري")
    @app_commands.checks.has_permissions(administrator=True)
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
        await interaction.response.send_message(f"✅ تم ضبط `{key}` على {channel.mention}", ephemeral=True)

    @set_channel.autocomplete("key")
    async def set_channel_key_autocomplete(self, interaction: discord.Interaction, current: str):
        return _autocomplete_choices(CHANNEL_KINDS, current)

    # -------------------------------------------------------------- set-role
    @config.command(name="set-role", description="ضبط إعداد من نوع رتبة")
    @app_commands.describe(key="اسم الإعداد", role="الرتبة")
    @app_commands.checks.has_permissions(administrator=True)
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
    @app_commands.checks.has_permissions(administrator=True)
    async def set_value(self, interaction: discord.Interaction, key: str, value: str):
        definition = get_definition(key)
        if definition.kind in ID_KINDS:
            raise ConfigError(f"«{key}» إعداد روم/رتبة. استعمل /config set-channel أو /config set-role.")

        parsed = definition.parse_input(value)
        saved = await config_service.set_setting(
            interaction.guild_id, key, parsed, updated_by=interaction.user.id
        )
        await interaction.response.send_message(
            f"✅ تم ضبط `{key}` على {format_value(definition, saved)}", ephemeral=True
        )

    @set_value.autocomplete("key")
    async def set_value_key_autocomplete(self, interaction: discord.Interaction, current: str):
        return _autocomplete_choices(VALUE_KINDS, current)

    # ----------------------------------------------------------------- reset
    @config.command(name="reset", description="إرجاع إعداد إلى قيمته الافتراضية")
    @app_commands.describe(key="اسم الإعداد")
    @app_commands.checks.has_permissions(administrator=True)
    async def reset(self, interaction: discord.Interaction, key: str):
        definition = get_definition(key)
        existed = await config_service.reset_setting(interaction.guild_id, key)
        if existed:
            message = f"✅ تمت إعادة `{key}` إلى الافتراضي: {format_value(definition, definition.default)}"
        else:
            message = f"ℹ️ `{key}` على القيمة الافتراضية أصلًا."
        await interaction.response.send_message(message, ephemeral=True)

    @reset.autocomplete("key")
    async def reset_key_autocomplete(self, interaction: discord.Interaction, current: str):
        return _autocomplete_choices(set(Kind), current)


async def setup(bot: commands.Bot):
    await bot.add_cog(ConfigCog(bot))
