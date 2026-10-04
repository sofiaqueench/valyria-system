"""Offline Phase 20 smoke and regression checks; never connects to Discord."""
from __future__ import annotations

import asyncio
import gc
import os
import weakref
from collections import deque
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace


PROJECT_ROOT = Path(__file__).resolve().parent


def _dotenv_value(path: Path, key: str) -> str | None:
    if not path.exists():
        return None
    values = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        name, separator, value = line.partition("=")
        if separator and name.strip() == key:
            values.append(value.split("#", 1)[0].strip().strip("\"'"))
    if len(values) > 1:
        raise AssertionError(f"{path.name} contains multiple {key} entries")
    return values[0] if values else None


example_guild_id = _dotenv_value(PROJECT_ROOT / ".env.example", "GUILD_ID")
local_guild_id = _dotenv_value(PROJECT_ROOT / ".env", "GUILD_ID")
if not example_guild_id or not example_guild_id.isdecimal():
    raise AssertionError(".env.example must contain a numeric GUILD_ID")
if local_guild_id is not None and local_guild_id != example_guild_id:
    raise AssertionError(".env and .env.example must use the same GUILD_ID")
expected_guild_id = int(local_guild_id or example_guild_id)
if expected_guild_id != 1499414146905804870:
    raise AssertionError("The configured development GUILD_ID must be 1499414146905804870")

# Isolate migrations from the configured bot database and make this a no-login test.
_temporary_database = PROJECT_ROOT / "work" / "phase20-validation.db"
_temporary_database.unlink(missing_ok=True)
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_temporary_database.as_posix()}"
os.environ.pop("GUILD_ID", None)
os.environ.setdefault("DISCORD_TOKEN", "phase20-validation-only")
if local_guild_id is None:
    os.environ["GUILD_ID"] = example_guild_id

import discord
from discord import app_commands
from sqlalchemy import text

from bot.core import bot as bot_module
from bot.core.bot import DiscordBot
from bot.cogs.phase14_reactions_cog import Phase14ReactionsCog, emoji_key, role_assignment_error
from bot.database.engine import engine, init_db
from bot.services import antispam_service
from bot.services import config_service
from bot.services.antispam_service import SpamSettings
from bot.services.member_xp_service import _locks, get_member_xp_mutation_lock


class _FakeCleanMessage:
    def __init__(self, created_at):
        self.created_at = created_at
        self.type = SimpleNamespace(is_deletable=lambda: True)
        self.deleted = False

    async def delete(self, *, reason=None):
        self.deleted = True


class _FakeCleanChannel:
    def __init__(self, messages):
        self.messages = messages
        self.bulk_deleted = []
        self.history_limit = None

    async def history(self, *, limit, oldest_first):
        self.history_limit = limit
        for message in self.messages[:limit]:
            yield message

    async def delete_messages(self, messages, *, reason=None):
        self.bulk_deleted.extend(messages)
        for message in messages:
            message.deleted = True


def _assert_unique_commands(commands) -> None:
    names = [command.name for command in commands]
    assert len(names) == len(set(names)), f"Duplicate command names: {names}"
    for command in commands:
        if isinstance(command, app_commands.Group):
            _assert_unique_commands(command.commands)


def _test_spam_detectors() -> None:
    now = 100.0
    message = lambda content, **extra: SimpleNamespace(
        content=content, mentions=[], role_mentions=[], mention_everyone=False, **extra
    )
    base = SpamSettings(True, 5, 5, 3, 15, 5, 12, 20, False, 5, False, frozenset(), 0)
    assert antispam_service._reason(message("normal conversation"), base, deque(), now) is None
    burst = SpamSettings(True, 3, 5, 3, 15, 5, 12, 20, False, 5, False, frozenset(), 0)
    assert antispam_service._reason(message("third"), burst, deque([(99.0, "one"), (98.0, "two")]), now)
    assert antispam_service._reason(message("Hello!"), base, deque([(99.0, "hello"), (98.0, "hello")]), now)
    mentions = SimpleNamespace(content="hello", mentions=[1, 2, 3], role_mentions=[], mention_everyone=False)
    mention_limit = SpamSettings(True, 5, 5, 3, 15, 2, 12, 20, False, 5, False, frozenset(), 0)
    assert antispam_service._reason(mentions, mention_limit, deque(), now)
    emoji = SpamSettings(True, 5, 5, 3, 15, 5, 1, 20, False, 5, False, frozenset(), 0)
    assert antispam_service._reason(message("😀😀"), emoji, deque(), now)
    repeats = SpamSettings(True, 5, 5, 3, 15, 5, 12, 4, False, 5, False, frozenset(), 0)
    assert antispam_service._reason(message("AAAA"), repeats, deque(), now)
    links = SpamSettings(True, 5, 5, 3, 15, 5, 12, 20, True, 1, False, frozenset(), 0)
    assert antispam_service._reason(message("https://a.test https://b.test"), links, deque(), now)

    first_guild = antispam_service._append_activity((expected_guild_id, 880001), now, "same", base)
    second_guild = antispam_service._append_activity((expected_guild_id + 1, 880001), now, "same", base)
    assert first_guild is not second_guild, "Anti-Spam activity must be guild-scoped"
    antispam_service._activity.pop((expected_guild_id, 880001), None)
    antispam_service._activity.pop((expected_guild_id + 1, 880001), None)


async def _validate() -> None:
    await init_db()
    async with engine.connect() as connection:
        revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    assert revision == "0014_security", f"Unexpected migration head: {revision}"

    bot = DiscordBot()
    await bot._async_setup_hook()
    sync_targets: list[int | None] = []

    async def offline_sync(*, guild=None):
        sync_targets.append(guild.id if guild else None)
        return []

    bot.tree.sync = offline_sync
    previous_shutdown = bot_module.announce_shutdown

    async def no_network_shutdown(_bot):
        return None

    bot_module.announce_shutdown = no_network_shutdown
    try:
        await bot.setup_hook()
        assert sync_targets == [expected_guild_id], f"Commands synced to {sync_targets}, expected the .env guild"
        global_commands = bot.tree.get_commands()
        _assert_unique_commands(global_commands)
        command_names = {command.name for command in global_commands}
        required = {"automod", "antiraid", "antispam", "security", "jail", "clean"}
        assert required <= command_names, f"Missing command groups: {sorted(required - command_names)}"
        assert len(bot.extensions) == 22, f"Only {len(bot.extensions)} extensions loaded"
        reaction_roles = bot.tree.get_command("reactionrole")
        assert reaction_roles is not None and "message_id" in {
            parameter.name for parameter in reaction_roles.get_command("clear").parameters
        }, "Reaction-role clear must be scoped to a message"
        assert emoji_key("<:gaming:123456789012345678>") == "id:123456789012345678"
        assert emoji_key("🎮") == "u:🎮"
        assert hasattr(Phase14ReactionsCog(bot), "_role_event_locks")

        class _Position:
            def __init__(self, value):
                self.value = value

            def __le__(self, other):
                return self.value <= (other.position if hasattr(other, "position") else other).value

        class _Role:
            def __init__(self, *, position=1, default=False, managed=False, admin=False):
                self.position = _Position(position)
                self._default = default
                self.managed = managed
                self.permissions = SimpleNamespace(administrator=admin)

            def is_default(self):
                return self._default

        bot_member = SimpleNamespace(guild_permissions=SimpleNamespace(manage_roles=True), top_role=_Position(10))
        assert role_assignment_error(SimpleNamespace(me=bot_member), _Role()) is None
        assert "Administrator" in role_assignment_error(SimpleNamespace(me=bot_member), _Role(admin=True))
        assert "Manage Roles" in role_assignment_error(
            SimpleNamespace(me=SimpleNamespace(guild_permissions=SimpleNamespace(manage_roles=False), top_role=_Position(10))),
            _Role(),
        )

        listeners = [listener.__qualname__ for listener in bot.extra_events.get("on_message", [])]
        assert listeners.count("MemberLevelingCog.on_message") == 1, listeners
        assert listeners.count("Phase14ReactionsCog.on_message") == 1, listeners
        for event_name in ("on_raw_reaction_add", "on_raw_reaction_remove"):
            event_listeners = bot.extra_events.get(event_name, [])
            assert sum(listener.__qualname__ == f"Phase14ReactionsCog.{event_name}" for listener in event_listeners) == 1

        _test_spam_detectors()

        security = bot.get_cog("SecurityCog")
        security_logs = []

        async def capture_security_log(*args, **kwargs):
            security_logs.append((args, kwargs))

        security._log = capture_security_log
        guild = SimpleNamespace(id=expected_guild_id, name="offline-validation", get_member=lambda _id: None)
        assert await security._set_raid_state(guild, True, None, "Phase 20 validation", 5)
        assert (await security._raid_state(expected_guild_id)).active
        assert await security._set_raid_state(guild, False, None, "Phase 20 validation complete")
        assert not (await security._raid_state(expected_guild_id)).active
        for _ in range(7):
            await security._mass_action(guild, "channel_delete", SimpleNamespace(id=880002))
        assert len(security_logs) == 3, "Raid transitions or mass-action alert throttle failed"

        clean = bot.get_cog("CleanCog")
        await config_service.set_setting(expected_guild_id, "clean_role_ids", "[880005]")
        await config_service.set_setting(expected_guild_id + 1, "clean_role_ids", "[880006]")
        authorized = SimpleNamespace(guild=SimpleNamespace(id=expected_guild_id), roles=[SimpleNamespace(id=880005)])
        unauthorized = SimpleNamespace(guild=SimpleNamespace(id=expected_guild_id), roles=[SimpleNamespace(id=880006)])
        other_guild_member = SimpleNamespace(guild=SimpleNamespace(id=expected_guild_id + 1), roles=[SimpleNamespace(id=880005)])
        assert await clean._authorized(authorized)
        assert not await clean._authorized(unauthorized)
        assert not await clean._authorized(other_guild_member), "Clean roles must be guild-scoped"
        now = discord.utils.utcnow()
        recent = [_FakeCleanMessage(now - timedelta(minutes=5)), _FakeCleanMessage(now - timedelta(minutes=2))]
        old = _FakeCleanMessage(now - timedelta(days=15))
        channel = _FakeCleanChannel([*recent, old])
        deleted = await clean._delete_recent(channel, 3)
        assert deleted == 3 and channel.history_limit == 3
        assert channel.bulk_deleted == recent and all(item.deleted for item in [*recent, old])

        # Locks must be shared while in use, then released from the registry afterward.
        lock_key = (expected_guild_id, 880003)
        lock = get_member_xp_mutation_lock(*lock_key)
        lock_ref = weakref.ref(lock)
        assert get_member_xp_mutation_lock(*lock_key) is lock
        del lock
        gc.collect()
        assert lock_ref() is None and lock_key not in _locks, "Idle Member XP locks are retained"

        leveling = bot.get_cog("MemberLevelingCog")
        member = SimpleNamespace(guild=SimpleNamespace(id=expected_guild_id), id=880004)
        member_key = (expected_guild_id, member.id)
        leveling._voice_sessions[member_key] = SimpleNamespace()
        leveling._voice_remainders[member_key] = 12.5
        await leveling.on_member_remove(member)
        assert member_key not in leveling._voice_sessions and member_key not in leveling._voice_remainders

        print(
            "Phase 20 passed: settings, migration head, 22 extensions, command/listener registration, "
            "Anti-Spam detectors and guild isolation, Anti-Raid persistence/throttling, Clean age handling, "
            "and transient XP/voice-state cleanup. No Discord connection was made."
        )
    finally:
        bot_module.announce_shutdown = previous_shutdown
        for cog_name in list(bot.cogs):
            await bot.remove_cog(cog_name)
        await bot.close()
        await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(_validate())
    finally:
        _temporary_database.unlink(missing_ok=True)
