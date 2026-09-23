"""
الخدمة المركزية لقراءة/كتابة إعدادات السيرفر.
كل نظام مستقبلي (Verification, Welcome, Tickets, Jail...) يستعمل هذه الدوال
بدل الوصول لقاعدة البيانات أو استخدام IDs مكتوبة يدويًا.

مثال:
    channel_id = await get_setting(guild.id, "log_moderation")   # int أو None
    xp_min     = await get_setting(guild.id, "message_xp_min")   # int
"""
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config.schema import GROUPS, SETTINGS, SettingDef, get_definition
from bot.core.errors import ConfigError
from bot.core.logger import logger
from bot.database.engine import async_session
from bot.database.models.guild import Guild
from bot.database.models.guild_setting import GuildSetting

# كاش في الذاكرة: guild_id -> {key: القيمة كنص}
# نقرأ الإعدادات كثيرًا (مثلًا مع كل رسالة لحساب XP) فلا نريد استعلامًا في كل مرة.
# يُمسح كاش السيرفر عند أي تعديل.
# (ملاحظة للمستقبل: عندما نضيف Dashboard كعملية منفصلة سنحتاج طريقة لإبطال الكاش.)
_cache: dict[int, dict[str, str]] = {}

# أزواج (الأصغر، الأكبر) يجب أن يبقى الأول <= الثاني
_RANGE_PAIRS = (
    ("message_xp_min", "message_xp_max"),
    ("jail_default_minutes", "jail_max_minutes"),
)


@dataclass(frozen=True)
class SettingState:
    """حالة إعداد واحد في سيرفر معيّن (للعرض في /config view)."""

    definition: SettingDef
    value: int | bool | str | None
    is_default: bool


async def get_or_create_guild(guild_id: int) -> Guild:
    """يجلب إعدادات السيرفر، أو ينشئه فارغًا إذا لم يكن موجودًا."""
    async with async_session() as session:
        guild = await _ensure_guild(session, guild_id)
        await session.commit()
        return guild


async def _ensure_guild(session: AsyncSession, guild_id: int) -> Guild:
    guild = await session.get(Guild, guild_id)
    if guild is None:
        guild = Guild(guild_id=guild_id)
        session.add(guild)
        await session.flush()  # يُدخل الصف الآن حتى يجده الـ Foreign Key
    return guild


async def _load_raw(guild_id: int) -> dict[str, str]:
    cached = _cache.get(guild_id)
    if cached is not None:
        return cached

    async with async_session() as session:
        result = await session.execute(
            select(GuildSetting.key, GuildSetting.value).where(GuildSetting.guild_id == guild_id)
        )
        raw = {key: value for key, value in result.all()}

    _cache[guild_id] = raw
    return raw


async def get_setting(guild_id: int, key: str) -> int | bool | str | None:
    """يرجع قيمة الإعداد بنوعها الصحيح، أو القيمة الافتراضية إذا لم تُضبط."""
    definition = get_definition(key)
    raw = (await _load_raw(guild_id)).get(key)
    if raw is None:
        return definition.default

    try:
        return definition.from_storage(raw)
    except ValueError:
        logger.warning("قيمة تالفة للإعداد %s في السيرفر %s — سيتم استعمال الافتراضي.", key, guild_id)
        return definition.default


async def set_setting(
    guild_id: int, key: str, value: int | bool | str, updated_by: int | None = None
) -> int | bool | str:
    """يتحقق من القيمة ثم يحفظها. يرفع ConfigError إذا كانت غير صالحة."""
    definition = get_definition(key)
    value = definition.validate(value)
    await _check_range_pairs(guild_id, key, value)

    async with async_session() as session:
        async with session.begin():
            await _ensure_guild(session, guild_id)
            row = await session.get(GuildSetting, (guild_id, key))
            stored = definition.to_storage(value)
            if row is None:
                session.add(
                    GuildSetting(guild_id=guild_id, key=key, value=stored, updated_by=updated_by)
                )
            else:
                row.value = stored
                row.updated_by = updated_by

    _cache.pop(guild_id, None)
    logger.info("تم تعديل الإعداد %s في السيرفر %s (بواسطة %s).", key, guild_id, updated_by)
    return value


async def reset_setting(guild_id: int, key: str) -> bool:
    """يحذف القيمة المخصّصة فيعود الإعداد للافتراضي. يرجع True إذا كانت هناك قيمة محذوفة."""
    get_definition(key)  # يتأكد أن المفتاح موجود

    async with async_session() as session:
        async with session.begin():
            result = await session.execute(
                delete(GuildSetting).where(
                    GuildSetting.guild_id == guild_id, GuildSetting.key == key
                )
            )

    _cache.pop(guild_id, None)
    return result.rowcount > 0


async def list_settings(guild_id: int, group: str | None = None) -> list[SettingState]:
    """كل إعدادات السيرفر (أو مجموعة واحدة) مع القيمة الحالية وهل هي افتراضية."""
    if group is not None and group not in GROUPS:
        raise ConfigError(f"المجموعة «{group}» غير موجودة.")

    raw = await _load_raw(guild_id)
    states = []
    for definition in SETTINGS.values():
        if group is not None and definition.group != group:
            continue
        value = await get_setting(guild_id, definition.key)
        states.append(SettingState(definition, value, is_default=definition.key not in raw))
    return states


async def _check_range_pairs(guild_id: int, key: str, value: int | bool | str) -> None:
    for low_key, high_key in _RANGE_PAIRS:
        if key not in (low_key, high_key):
            continue
        low = value if key == low_key else await get_setting(guild_id, low_key)
        high = value if key == high_key else await get_setting(guild_id, high_key)
        if low > high:
            raise ConfigError(
                f"«{low_key}» ({low}) لا يمكن أن يكون أكبر من «{high_key}» ({high}). "
                "عدّل الآخر أولًا."
            )
