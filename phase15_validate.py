import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

_validation_database = Path(__file__).resolve().parent / "work" / "phase15-validation.db"
_validation_database.unlink(missing_ok=True)
_database_path = _validation_database.as_posix()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_database_path}"
os.environ.setdefault("DISCORD_TOKEN", "phase15-validation-only")

import discord
from sqlalchemy import func, select

from bot.core.bot import DiscordBot
from bot.database.engine import async_session, engine, init_db
from bot.database.models.member_xp import MemberXP, MemberXPHistory
from bot.database.models.phase14 import ModerationAction
from bot.database.models.verification import StaffXPHistory
from bot.services import config_service
from bot.services import automod_service as auto
from bot.services.logging_service import SendResult
from bot.services.member_xp_service import get_member_xp
from bot.cogs.automod_cog import AutoModCog
from bot.cogs.config_cog import ConfigCog
from bot.cogs.phase14_moderation_cog import Phase14ModerationCog
from bot.cogs.phase14_reactions_cog import Phase14ReactionsCog
from bot.cogs.game_cleanup_cog import GameCleanupCog


class FakeMember:
    def __init__(self, guild, member_id, *, bot=False, top_role=1, ban_members=True):
        self.guild = guild
        self.id = member_id
        self.bot = bot
        self.roles = []
        self.top_role = top_role
        self.guild_permissions = SimpleNamespace(administrator=False, ban_members=ban_members)
        self.display_name = f"member-{member_id}"
        self.mention = f"<@{member_id}>"
        self.sent = []
        self.bans = 0

    async def send(self, content):
        self.sent.append(content)

    async def ban(self, **kwargs):
        self.bans += 1


class FakeGuild:
    def __init__(self, guild_id):
        self.id = guild_id
        self.name = f"guild-{guild_id}"
        self.owner_id = guild_id + 999
        self.me = FakeMember(self, 7000 + guild_id, bot=True, top_role=100)


class FakeMessage:
    def __init__(self, guild, author, message_id, content):
        self.guild = guild
        self.author = author
        self.id = message_id
        self.content = content
        self.webhook_id = None
        self.channel = None
        self.deleted = 0

    def is_system(self):
        return False

    async def delete(self, **kwargs):
        self.deleted += 1


class FakeBot:
    def __init__(self):
        self.user = SimpleNamespace(id=9999, mention="<@9999>")
        self.command_prefix = "!"
        self.cog = None

    def get_cog(self, name):
        return self.cog if name == "Phase14ModerationCog" else None


async def main():
    # Word boundaries and case-insensitivity
    assert auto.word_pattern("badword").search("BADWORD")
    assert auto.word_pattern("badword").search("hello badword!")
    assert auto.word_pattern("badword").search("badwording") is None

    await init_db()
    guild_id = 912345678901234503
    guild = FakeGuild(guild_id)
    member = FakeMember(guild, 812345678901234503)
    for key, value in (
        ("automod_enabled", True), ("automod_block_badwords", True),
        ("automod_ignore_staff", False), ("automod_words", '["badword"]'),
        ("automod_warn_xp_penalty", 50), ("warn_expire_days", 0),
    ):
        await config_service.set_setting(guild_id, key, value)
    auto.invalidate_automod_cache(guild_id)
    persisted_config = await auto.get_automod_config(guild_id)
    assert persisted_config.enabled is True
    assert persisted_config.words == ("badword",)
    assert persisted_config.xp_penalty == 50
    other_guild_config = await auto.get_automod_config(guild_id + 1)
    assert other_guild_config.enabled is False and other_guild_config.words == ()
    await config_service.get_or_create_guild(guild_id)
    async with async_session() as session:
        async with session.begin():
            xp_row = await session.get(MemberXP, (guild_id, member.id))
            if xp_row is None:
                session.add(MemberXP(guild_id=guild_id, member_id=member.id, total_xp=250))
            else:
                xp_row.total_xp = 250

    real_member_type = discord.Member
    discord.Member = FakeMember
    bot = FakeBot()
    bot.cog = Phase14ModerationCog(bot)
    original_auto_log, original_mod_log = auto.send_log, __import__("bot.cogs.phase14_moderation_cog", fromlist=["send_log"]).send_log

    async def fake_log(*args, **kwargs):
        return SendResult.SENT

    async def no_level_change(*args, **kwargs):
        return None

    auto.send_log = fake_log
    import bot.cogs.phase14_moderation_cog as moderation
    moderation.send_log = fake_log
    auto.process_member_xp_change = no_level_change
    try:
        normal_message = FakeMessage(guild, member, 699999, "A normal message")
        bot_message = FakeMessage(guild, FakeMember(guild, 812345678901234504, bot=True), 699998, "badword")
        assert await auto.process_automod_message(normal_message, bot) is False
        assert await auto.process_automod_message(bot_message, bot) is False
        assert normal_message.deleted == 0 and bot_message.deleted == 0

        for index in range(1, 4):
            message = FakeMessage(guild, member, 700000 + index, "Hello BADWORD here")
            assert await auto.process_automod_message(message, bot)
            assert message.deleted == 1
        assert await get_member_xp(guild_id, member.id) == 100
        assert len(member.sent) == 3

        fourth = FakeMessage(guild, member, 700004, "badword")
        assert await auto.process_automod_message(fourth, bot)
        assert member.bans == 1
        assert await get_member_xp(guild_id, member.id) == 100
        assert len(member.sent) == 4 and "4th violation" in member.sent[-1]

        assert await auto.process_automod_message(fourth, bot)
        assert member.bans == 1
        assert await get_member_xp(guild_id, member.id) == 100

        async with async_session() as session:
            warns = await session.scalar(select(func.count(ModerationAction.id)).where(
                ModerationAction.guild_id == guild_id, ModerationAction.target_id == member.id,
                ModerationAction.action_type == "warn",
            ))
            xp_rows = await session.scalars(select(MemberXPHistory).where(
                MemberXPHistory.guild_id == guild_id, MemberXPHistory.member_id == member.id,
            ))
            xp_rows = list(xp_rows.all())
            ban_action = await session.scalar(select(ModerationAction).where(
                ModerationAction.guild_id == guild_id, ModerationAction.message_id == fourth.id,
            ))
            staff_rows = await session.scalar(select(func.count(StaffXPHistory.id)).where(
                StaffXPHistory.guild_id == guild_id, StaffXPHistory.staff_id == member.id,
            ))
        assert warns == 3
        assert len(xp_rows) == 3 and all(row.source == "automod_warn" and row.amount < 0 for row in xp_rows)
        assert ban_action.action_type == "ban" and ban_action.source == "AutoMod"
        assert staff_rows == 0
    finally:
        auto.send_log = original_auto_log
        moderation.send_log = original_mod_log
        discord.Member = real_member_type

    command_bot = DiscordBot()
    await command_bot.add_cog(ConfigCog(command_bot))
    await command_bot.add_cog(Phase14ModerationCog(command_bot))
    await command_bot.add_cog(Phase14ReactionsCog(command_bot))
    await command_bot.add_cog(GameCleanupCog(command_bot))
    await command_bot.add_cog(AutoModCog(command_bot))
    root = {command.name for command in command_bot.tree.get_commands()}
    assert "automod" in root and "warn" in root
    print("Phase 15 detection, 3-warning/4th-ban flow, XP floor/history isolation, duplicate guard, and slash registration passed.")
    await command_bot.close()
    await engine.dispose()
    _validation_database.unlink(missing_ok=True)


asyncio.run(main())
