"""Invite cache, first-join attribution, and invite statistics."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import discord
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from bot.core.logger import logger
from bot.core.permissions import StaffLevel, get_staff_level
from bot.database.engine import async_session
from bot.database.models.invite import InviteAttribution
from bot.database.models.verification import StaffXPHistory
from bot.services import config_service
from bot.services.logging_service import LogEvent, LogType, send_log
from bot.services.staff_xp_service import add_staff_xp_to_session


@dataclass(frozen=True)
class InviteSnapshot:
    inviter_id: int | None
    uses: int | None


@dataclass(frozen=True)
class InviteJoinResult:
    status: str
    invite_code: str | None = None
    inviter_id: int | None = None
    uses_before: int | None = None
    uses_after: int | None = None
    xp_awarded: bool = False
    xp_amount: int = 0
    duplicate: bool = False
    xp_note: str = "No XP"


class InviteTracker:
    """Maintain per-guild invite snapshots and attribute joins conservatively."""

    def __init__(self, bot: discord.Client):
        self.bot = bot
        self._cache: dict[int, dict[str, InviteSnapshot]] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._permission_warned: set[int] = set()

    def _lock(self, guild_id: int) -> asyncio.Lock:
        return self._locks.setdefault(guild_id, asyncio.Lock())

    @staticmethod
    def _snapshot(invites) -> dict[str, InviteSnapshot]:
        return {
            invite.code: InviteSnapshot(
                inviter_id=invite.inviter.id if invite.inviter else None,
                uses=invite.uses,
            )
            for invite in invites
            if invite.code
        }

    async def refresh_guild(self, guild: discord.Guild) -> bool:
        """Fetch a baseline after startup/reconnect or when the bot joins a guild."""
        async with self._lock(guild.id):
            try:
                invites = await guild.invites()
            except discord.Forbidden:
                self._cache.pop(guild.id, None)
                self._warn_permission(guild)
                return False
            except discord.HTTPException:
                self._cache.pop(guild.id, None)
                logger.exception("Could not refresh invite cache for guild %s.", guild.id)
                return False
            except Exception:
                self._cache.pop(guild.id, None)
                logger.exception("Unexpected error refreshing invites for guild %s.", guild.id)
                return False
            self._cache[guild.id] = self._snapshot(invites)
            self._permission_warned.discard(guild.id)
            return True

    def forget_guild(self, guild_id: int) -> None:
        self._cache.pop(guild_id, None)
        self._permission_warned.discard(guild_id)

    async def on_invite_create(self, invite: discord.Invite) -> None:
        guild = invite.guild
        if guild is None or guild.id not in self._cache or not invite.code:
            return
        async with self._lock(guild.id):
            self._cache[guild.id][invite.code] = InviteSnapshot(
                inviter_id=invite.inviter.id if invite.inviter else None,
                uses=invite.uses,
            )

    async def on_invite_delete(self, invite: discord.Invite) -> None:
        guild = invite.guild
        if guild is not None and guild.id in self._cache and invite.code:
            async with self._lock(guild.id):
                self._cache[guild.id].pop(invite.code, None)

    def _warn_permission(self, guild: discord.Guild) -> None:
        if guild.id not in self._permission_warned:
            self._permission_warned.add(guild.id)
            logger.warning(
                "Invite attribution unavailable in guild %s: bot needs Manage Server to read invites.",
                guild.id,
            )

    async def process_member_join(self, member: discord.Member) -> None:
        """Attribute and log one join; tracking errors never escape into other join systems."""
        try:
            await self._process_member_join(member)
        except Exception:
            logger.exception("Invite tracking failed for member %s in guild %s.", member.id, member.guild.id)

    async def _process_member_join(self, member: discord.Member) -> None:
        guild = member.guild
        async with self._lock(guild.id):
            before = self._cache.get(guild.id)
            current_invites = None
            try:
                current_invites = await guild.invites()
            except discord.Forbidden:
                self._cache.pop(guild.id, None)
                self._warn_permission(guild)
                result = InviteJoinResult(status="unavailable", xp_note="Invite permission unavailable")
            except discord.HTTPException:
                self._cache.pop(guild.id, None)
                logger.exception("Could not fetch invites on member join in guild %s.", guild.id)
                result = InviteJoinResult(status="unavailable", xp_note="Discord invite API unavailable")
            except Exception:
                self._cache.pop(guild.id, None)
                logger.exception("Unexpected error fetching invites on member join in guild %s.", guild.id)
                result = InviteJoinResult(status="unavailable", xp_note="Invite data unavailable")
            else:
                after = self._snapshot(current_invites)
                self._cache[guild.id] = after
                if before is None:
                    result = InviteJoinResult(status="cache_cold", xp_note="Invite cache was not initialized")
                else:
                    changes: list[tuple[str, InviteSnapshot, InviteSnapshot]] = []
                    for code, new_snapshot in after.items():
                        old_snapshot = before.get(code)
                        if old_snapshot is None or old_snapshot.uses is None or new_snapshot.uses is None:
                            continue
                        if new_snapshot.uses > old_snapshot.uses:
                            changes.append((code, old_snapshot, new_snapshot))

                    if len(changes) != 1 or changes[0][2].uses != changes[0][1].uses + 1:
                        status = "ambiguous" if changes else "not_detected"
                        result = InviteJoinResult(status=status, xp_note="Invite use could not be reliably identified")
                    else:
                        code, old_snapshot, new_snapshot = changes[0]
                        status = "attributed" if new_snapshot.inviter_id is not None else "unknown_inviter"
                        result = InviteJoinResult(
                            status=status,
                            invite_code=code,
                            inviter_id=new_snapshot.inviter_id,
                            uses_before=old_snapshot.uses,
                            uses_after=new_snapshot.uses,
                            xp_note="Inviter is unavailable",
                        )

        await self._persist_and_log(member, result, current_invites)

    async def _persist_and_log(
        self,
        member: discord.Member,
        result: InviteJoinResult,
        current_invites: list[discord.Invite] | None = None,
    ) -> InviteJoinResult:
        guild = member.guild
        invite = next((item for item in current_invites or [] if item.code == result.invite_code), None)
        inviter_user = invite.inviter if invite and invite.inviter else None
        inviter_member = guild.get_member(result.inviter_id) if result.inviter_id else None
        eligible = False
        xp_amount = 0
        xp_note = result.xp_note

        if result.inviter_id is not None and not member.bot:
            bot_user = self.bot.user
            if result.inviter_id == member.id:
                xp_note = "Member joined using their own invite"
            elif bot_user is not None and result.inviter_id == bot_user.id:
                xp_note = "Invite creator is the bot"
            elif inviter_user is not None and getattr(inviter_user, "bot", False):
                xp_note = "Invite creator is a bot"
            else:
                if inviter_member is None:
                    try:
                        inviter_member = await guild.fetch_member(result.inviter_id)
                    except discord.HTTPException:
                        inviter_member = None
                if inviter_member is not None:
                    try:
                        eligible = await get_staff_level(inviter_member) >= StaffLevel.STAFF
                    except Exception:
                        logger.exception("Could not check Staff eligibility for inviter %s.", result.inviter_id)
                        xp_note = "Staff eligibility could not be checked"
                if eligible:
                    try:
                        xp_amount = int(await config_service.get_setting(guild.id, "staff_xp_invite") or 0)
                        xp_note = "Staff XP awarded" if xp_amount > 0 else "Invite XP is disabled"
                    except Exception:
                        logger.exception("Could not read invite Staff XP setting for guild %s.", guild.id)
                        xp_note = "Invite XP setting could not be read"
                elif xp_note == "Inviter is unavailable":
                    xp_note = "Invite creator is not Staff"
        elif member.bot:
            xp_note = "Joined account is a bot"

        await config_service.get_or_create_guild(guild.id)
        try:
            async with async_session() as session:
                async with session.begin():
                    existing = await session.scalar(select(InviteAttribution.id).where(
                        InviteAttribution.guild_id == guild.id,
                        InviteAttribution.member_id == member.id,
                    ))
                    if existing is not None:
                        result = InviteJoinResult(
                            status=result.status,
                            invite_code=result.invite_code,
                            inviter_id=result.inviter_id,
                            uses_before=result.uses_before,
                            uses_after=result.uses_after,
                            duplicate=True,
                            xp_note="Member was already attributed; rejoin XP prevented",
                        )
                    else:
                        result = InviteJoinResult(
                            status=result.status,
                            invite_code=result.invite_code,
                            inviter_id=result.inviter_id,
                            uses_before=result.uses_before,
                            uses_after=result.uses_after,
                            xp_note=xp_note,
                        )
                        attribution = InviteAttribution(
                            guild_id=guild.id,
                            member_id=member.id,
                            inviter_id=result.inviter_id,
                            invite_code=result.invite_code,
                            uses_before=result.uses_before,
                            uses_after=result.uses_after,
                            status=result.status,
                            xp_awarded=False,
                            xp_amount=0,
                        )
                        session.add(attribution)
                        if eligible and xp_amount > 0 and result.inviter_id is not None:
                            awarded = await add_staff_xp_to_session(
                                session,
                                guild_id=guild.id,
                                staff_id=result.inviter_id,
                                target_id=member.id,
                                action="invite",
                                amount=xp_amount,
                            )
                            result = InviteJoinResult(
                                status=result.status,
                                invite_code=result.invite_code,
                                inviter_id=result.inviter_id,
                                uses_before=result.uses_before,
                                uses_after=result.uses_after,
                                xp_awarded=awarded,
                                xp_amount=xp_amount if awarded else 0,
                                xp_note="Staff XP awarded" if awarded else "XP award was not recorded",
                            )
                            attribution.xp_awarded = awarded
                            attribution.xp_amount = xp_amount if awarded else 0
        except IntegrityError:
            result = InviteJoinResult(
                status=result.status,
                invite_code=result.invite_code,
                inviter_id=result.inviter_id,
                uses_before=result.uses_before,
                uses_after=result.uses_after,
                duplicate=True,
                xp_note="Concurrent/rejoin attribution already exists; XP prevented",
            )

        await self._log_attribution(member, result, inviter_user)
        return result

    @staticmethod
    async def _log_attribution(
        member: discord.Member,
        result: InviteJoinResult,
        inviter_user: discord.abc.User | None,
    ) -> None:
        fields = [
            ("Invite", f"`{result.invite_code}`" if result.invite_code else "Unknown", True),
            (
                "Invited by",
                f"{inviter_user.mention} (`{result.inviter_id}`)" if inviter_user and result.inviter_id else (
                    f"<@{result.inviter_id}> (`{result.inviter_id}`)" if result.inviter_id else "Unknown"
                ),
                True,
            ),
            ("Attribution", result.status, True),
            ("Staff XP", f"+{result.xp_amount} XP" if result.xp_awarded else f"No XP — {result.xp_note}", False),
        ]
        if result.uses_before is not None and result.uses_after is not None:
            fields.append(("Invite uses", f"{result.uses_before} → {result.uses_after}", True))
        if result.duplicate:
            fields.append(("Rejoin", "Previously attributed; no repeat XP", False))
        event = LogEvent(
            LogType.INVITE,
            "🎟️ Invite Used" if result.invite_code else "❔ Invite Could Not Be Identified",
            user=member,
            xp_change=result.xp_amount if result.xp_awarded else None,
            fields=fields,
        )
        try:
            await send_log(member.guild, event)
        except Exception:
            logger.exception("Could not write invite attribution log for member %s.", member.id)


async def get_invite_stats(guild_id: int, inviter_id: int) -> tuple[int, int, list[tuple[str, int]]]:
    """Return unique attributed members, invite XP earned, and most-used codes."""
    async with async_session() as session:
        total = await session.scalar(
            select(func.count(InviteAttribution.id)).where(
                InviteAttribution.guild_id == guild_id,
                InviteAttribution.inviter_id == inviter_id,
            )
        )
        xp = await session.scalar(
            select(func.coalesce(func.sum(StaffXPHistory.xp_amount), 0)).where(
                StaffXPHistory.guild_id == guild_id,
                StaffXPHistory.staff_id == inviter_id,
                StaffXPHistory.action == "invite",
            )
        )
        rows = await session.execute(
            select(InviteAttribution.invite_code, func.count(InviteAttribution.id))
            .where(
                InviteAttribution.guild_id == guild_id,
                InviteAttribution.inviter_id == inviter_id,
                InviteAttribution.invite_code.is_not(None),
            )
            .group_by(InviteAttribution.invite_code)
            .order_by(func.count(InviteAttribution.id).desc())
            .limit(5)
        )
    return int(total or 0), int(xp or 0), [(str(code), int(count)) for code, count in rows.all()]
