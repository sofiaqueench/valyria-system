# bot/core/permissions.py
"""
نظام الصلاحيات المركزي (Central Permissions System).
...
"""
from __future__ import annotations
from enum import IntEnum
import discord
from discord import app_commands
from bot.services.config_service import get_setting


class StaffLevel(IntEnum):
    NONE = 0
    STAFF = 10
    SUPPORT = 20
    TRIAL_MODERATOR = 30
    MODERATOR = 40
    HEAD_MODERATOR = 50
    ADMIN = 60
    OWNER = 70


_LEVEL_SETTING_KEYS: list[tuple[StaffLevel, str]] = [
    (StaffLevel.OWNER, "role_owner"),
    (StaffLevel.ADMIN, "role_admin"),
    (StaffLevel.HEAD_MODERATOR, "role_head_moderator"),
    (StaffLevel.MODERATOR, "role_moderator"),
    (StaffLevel.TRIAL_MODERATOR, "role_trial_moderator"),
    (StaffLevel.SUPPORT, "role_support"),
    (StaffLevel.STAFF, "role_staff"),
]

_LEVEL_LABELS: dict[StaffLevel, str] = {
    StaffLevel.NONE: "بدون صلاحيات", StaffLevel.STAFF: "Staff",
    StaffLevel.SUPPORT: "Support", StaffLevel.TRIAL_MODERATOR: "Trial Moderator",
    StaffLevel.MODERATOR: "Moderator", StaffLevel.HEAD_MODERATOR: "Head Moderator",
    StaffLevel.ADMIN: "Admin", StaffLevel.OWNER: "Owner",
}


class InsufficientStaffLevel(app_commands.CheckFailure):
    def __init__(self, required: StaffLevel):
        self.required = required
        super().__init__(f"يتطلب مستوى {_LEVEL_LABELS[required]} أو أعلى.")


def level_label(level: StaffLevel) -> str:
    return _LEVEL_LABELS[level]


async def get_staff_level(member: discord.Member) -> StaffLevel:
    if member.guild.owner_id == member.id or member.guild_permissions.administrator:
        return StaffLevel.OWNER

    member_role_ids = {role.id for role in member.roles}
    for level, setting_key in _LEVEL_SETTING_KEYS:
        role_id = await get_setting(member.guild.id, setting_key)
        if role_id is not None and role_id in member_role_ids:
            return level
    return StaffLevel.NONE


async def has_level(member: discord.Member, required: StaffLevel) -> bool:
    return (await get_staff_level(member)) >= required


def requires_level(required: StaffLevel):
    async def predicate(interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            return False
        if await has_level(interaction.user, required):
            return True
        raise InsufficientStaffLevel(required)
    return app_commands.check(predicate)


async def is_role_hierarchy_safe(actor: discord.Member, target: discord.Member) -> bool:
    if target.id == target.guild.owner_id:
        return False
    if actor.id == actor.guild.owner_id:
        return True
    return actor.top_role > target.top_role