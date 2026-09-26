"""Temporary voice channel lifecycle and owner controls, integrated with existing systems."""
from __future__ import annotations

import asyncio
from urllib.parse import urlparse

import discord
from discord.ext import commands, tasks

from bot.core.logger import logger
from bot.core.permissions import StaffLevel, has_level
from bot.services import config_service, temp_voice_service as store
from bot.services.logging_service import LogEvent, LogType, send_log
from bot.views.temp_voice import (
    TempVoiceLimitModal, TempVoiceMemberSelectView, TempVoiceOwnerView,
    TempVoiceRenameModal, TempVoiceSearchView,
)


STAFF_ROLE_KEYS = (
    "role_staff", "role_support", "role_trial_moderator", "role_moderator",
    "role_head_moderator", "role_admin", "role_owner",
)


class TempVoiceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._creation_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._room_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._pending_delete: dict[tuple[int, int], float] = {}
        self._staff_call_until: dict[tuple[int, int, int], float] = {}
        self._recovered = False

    async def cog_load(self) -> None:
        # Stable custom IDs keep controls working after restarts.
        self.bot.add_view(TempVoiceOwnerView(self))
        if not self.delete_empty_rooms.is_running():
            self.delete_empty_rooms.start()

    def cog_unload(self) -> None:
        self.delete_empty_rooms.cancel()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._recovered:
            return
        try:
            await self.recover_rooms()
            self._recovered = True
        except Exception:
            logger.exception("Temporary Voice recovery failed.")

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        # Recovery after joining an additional guild while the bot is online.
        await self.recover_rooms(guild)

    @tasks.loop(seconds=3)
    async def delete_empty_rooms(self) -> None:
        now = asyncio.get_running_loop().time()
        for key, due in list(self._pending_delete.items()):
            if now < due:
                continue
            lock = self._room_locks.setdefault(key, asyncio.Lock())
            deleted_channel = None
            async with lock:
                room = await store.get_room(*key)
                if room is None:
                    self._pending_delete.pop(key, None)
                    continue
                guild = self.bot.get_guild(room.guild_id)
                channel = guild.get_channel(room.channel_id) if guild else None
                if channel is None:
                    await self._cleanup_missing_room(room.guild_id, room.channel_id)
                    continue
                if self._human_members(channel):
                    self._pending_delete.pop(key, None)
                    continue
                if asyncio.get_running_loop().time() < self._pending_delete.get(key, due):
                    continue
                try:
                    await channel.delete(reason="Temporary voice room empty after grace period")
                    deleted_channel = channel
                except discord.NotFound:
                    deleted_channel = channel
                except discord.Forbidden:
                    logger.warning("Missing Manage Channels permission to delete temp room %s.", channel.id)
                    # Don't hammer Discord; retry after the owner leaves or the bot is restarted.
                    self._pending_delete.pop(key, None)
                except discord.HTTPException:
                    logger.exception("Could not delete empty temp room %s.", channel.id)
                    self._pending_delete[key] = asyncio.get_running_loop().time() + 30
            if deleted_channel is not None:
                await self.process_deleted_channel(deleted_channel)

    @delete_empty_rooms.before_loop
    async def before_delete_empty_rooms(self) -> None:
        await self.bot.wait_until_ready()

    async def process_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState,
    ) -> None:
        if member.bot:
            return
        guild = member.guild
        generator_id = await config_service.get_setting(guild.id, "temp_voice_create_channel")
        if after.channel is not None and after.channel.id == generator_id:
            if before.channel is None or before.channel.id != generator_id:
                await self._create_for_member(
                    member, source_channel_id=before.channel.id if before.channel else None,
                )

        if after.channel is not None:
            room = await store.get_room(guild.id, after.channel.id)
            if room is not None:
                self._pending_delete.pop((guild.id, room.channel_id), None)
                if await store.is_room_banned(guild.id, room.channel_id, member.id):
                    try:
                        await member.move_to(None, reason="Member is banned from this temporary room")
                    except (discord.Forbidden, discord.HTTPException):
                        logger.exception("Could not enforce temp room ban for member %s.", member.id)
                    return
                await self.refresh_panel(guild, room.channel_id)

        if before.channel is None or before.channel == after.channel:
            return
        old_key = (guild.id, before.channel.id)
        old_room = await store.get_room(*old_key)
        if old_room is None:
            return
        lock = self._room_locks.setdefault(old_key, asyncio.Lock())
        async with lock:
            old_room = await store.get_room(*old_key)
            if old_room is None:
                return
            channel = guild.get_channel(old_room.channel_id)
            if channel is None:
                await self._cleanup_missing_room(*old_key)
                return
            remaining = [user for user in self._human_members(channel) if user.id != member.id]
            if member.id == old_room.owner_id and remaining:
                try:
                    await self._transfer_ownership(guild, channel, old_room, remaining[0], automatic=True)
                except Exception:
                    logger.exception("Automatic temp room ownership transfer failed for channel %s.", channel.id)
            if not remaining:
                await self._schedule_empty_delete(guild, channel.id)
            await self.refresh_panel(guild, channel.id)

    async def process_deleted_channel(self, channel: discord.abc.GuildChannel) -> None:
        key = (channel.guild.id, channel.id)
        lock = self._room_locks.setdefault(key, asyncio.Lock())
        deleted_room = None
        async with lock:
            self._pending_delete.pop(key, None)
            room = await store.get_room(*key)
            if room is not None and await store.delete_room(*key):
                deleted_room = room
        if deleted_room is not None:
            await self._log_action(channel.guild, "🗑️ Temporary Voice Room Deleted", deleted_room, reason="Channel deleted")
        if not lock.locked():
            self._room_locks.pop(key, None)

    async def _create_for_member(self, member: discord.Member, *, source_channel_id: int | None = None) -> None:
        key = (member.guild.id, member.id)
        lock = self._creation_locks.setdefault(key, asyncio.Lock())
        async with lock:
            generator_id = await config_service.get_setting(member.guild.id, "temp_voice_create_channel")
            category_id = await config_service.get_setting(member.guild.id, "temp_voice_category")
            if not generator_id or not category_id:
                logger.warning("Temp Voice generator/category is not configured in guild %s.", member.guild.id)
                await self._notify_member(member, "The temporary room system is not fully configured. Please contact staff.")
                return
            if member.voice is None or member.voice.channel is None or member.voice.channel.id != generator_id:
                return
            guild = member.guild
            category = guild.get_channel(category_id)
            if not isinstance(category, discord.CategoryChannel):
                logger.warning("Configured Temp Voice category %s is missing in guild %s.", category_id, guild.id)
                await self._notify_member(member, "The temporary room category is unavailable. Please contact staff.")
                return

            # A repeated gateway update moves the member back to their existing room instead
            # of creating duplicates.
            for existing in await store.list_rooms(guild.id):
                if existing.owner_id != member.id:
                    continue
                old_channel = guild.get_channel(existing.channel_id)
                if isinstance(old_channel, discord.VoiceChannel):
                    if old_channel.id != source_channel_id:
                        await member.move_to(old_channel, reason="Reuse existing temporary room")
                        return
                    # An owner may leave their current room through the generator to create
                    # a fresh one; the per-member lock and live-state recheck prevent duplicates.
                    continue
                await self._cleanup_missing_room(guild.id, existing.channel_id)

            default_limit = int(await config_service.get_setting(guild.id, "temp_voice_default_limit") or 0)
            template = str(await config_service.get_setting(guild.id, "temp_voice_default_name") or "🔊・{display_name}'s Chamber")
            name = self._format_room_name(template, member)
            try:
                overwrites = await self._creation_overwrites(guild, member)
                channel = await guild.create_voice_channel(
                    name=name, category=category, user_limit=default_limit,
                    overwrites=overwrites, reason=f"Temporary voice room for {member} ({member.id})",
                )
            except discord.Forbidden:
                logger.warning("Cannot create Temp Voice channel in guild %s: missing Manage Channels permission.", guild.id)
                await self._notify_member(member, "I cannot create your room. Staff must grant me Manage Channels and Move Members.")
                return
            except discord.HTTPException:
                logger.exception("Could not create Temp Voice channel for member %s.", member.id)
                await self._notify_member(member, "Your temporary room could not be created. Please try again or contact staff.")
                return

            try:
                room = await store.create_room(
                    guild.id, channel.id, member.id, category.id, name, default_limit,
                )
                await self._ensure_panel(guild, channel, room)
                await member.move_to(channel, reason="Move to newly created temporary room")
            except Exception:
                logger.exception("Temp Voice setup failed after creating channel %s.", channel.id)
                try:
                    await channel.delete(reason="Temporary room setup failed")
                except discord.HTTPException:
                    logger.exception("Could not roll back partially created temp room %s.", channel.id)
                await store.delete_room(guild.id, channel.id)
                await self._notify_member(member, "Your room could not be initialized. Please contact staff.")
                return
            await self._log_action(guild, "➕ Temporary Voice Room Created", room, user=member)

    async def _creation_overwrites(self, guild: discord.Guild, owner: discord.Member):
        overwrites: dict[discord.abc.Snowflake, discord.PermissionOverwrite] = {
            guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=True),
            owner: discord.PermissionOverwrite(view_channel=True, connect=True),
        }
        bot_member = guild.me
        if bot_member:
            overwrites[bot_member] = discord.PermissionOverwrite(
                view_channel=True, connect=True, manage_channels=True,
                move_members=True, send_messages=True, embed_links=True,
            )
        for role_id in await self._staff_role_ids(guild.id):
            role = guild.get_role(role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(view_channel=True, connect=True)
        return overwrites

    @staticmethod
    def _format_room_name(template: str, member: discord.Member) -> str:
        values = {
            "username": member.name,
            "display_name": member.display_name,
            "member": member.display_name,
        }
        class SafeValues(dict):
            def __missing__(self, key):
                return ""
        try:
            name = template.format_map(SafeValues(values)).strip()
        except (ValueError, AttributeError):
            name = f"🔊・{member.display_name}'s Chamber"
        return (name or f"🔊・{member.display_name}'s Chamber")[:100]

    async def _notify_member(self, member: discord.Member, text: str) -> None:
        try:
            await member.send(text)
        except discord.HTTPException:
            logger.info("Could not DM member %s about Temp Voice setup.", member.id)

    def _human_members(self, channel) -> list[discord.Member]:
        return [member for member in getattr(channel, "members", []) if not member.bot]

    async def _schedule_empty_delete(self, guild: discord.Guild, channel_id: int) -> None:
        key = (guild.id, channel_id)
        configured = await config_service.get_setting(guild.id, "temp_voice_delete_delay")
        seconds = max(0, int(configured if configured is not None else 5))
        self._pending_delete[key] = asyncio.get_running_loop().time() + seconds

    async def recover_rooms(self, only_guild: discord.Guild | None = None) -> None:
        rooms = await store.list_rooms(only_guild.id if only_guild else None)
        for room in rooms:
            guild = only_guild or self.bot.get_guild(room.guild_id)
            if guild is None:
                continue
            channel = guild.get_channel(room.channel_id)
            if not isinstance(channel, discord.VoiceChannel):
                await self._cleanup_missing_room(room.guild_id, room.channel_id)
                continue
            try:
                await self._apply_room_state(guild, channel, room)
                await self._ensure_panel(guild, channel, room)
                if self._human_members(channel):
                    self._pending_delete.pop((guild.id, channel.id), None)
                else:
                    await self._schedule_empty_delete(guild, channel.id)
            except discord.NotFound:
                await self._cleanup_missing_room(guild.id, channel.id)
            except discord.Forbidden:
                logger.warning("Cannot recover temp room %s: missing channel permissions.", channel.id)
            except discord.HTTPException:
                logger.exception("Could not recover temp room %s.", channel.id)

    async def _ensure_panel(self, guild: discord.Guild, channel: discord.VoiceChannel, room) -> None:
        if not await config_service.get_setting(guild.id, "temp_voice_panel_enabled"):
            return
        if room.panel_message_id:
            try:
                message = await channel.fetch_message(room.panel_message_id)
                await message.edit(embed=await self._panel_embed(guild, channel, room), view=TempVoiceOwnerView(self))
                return
            except discord.NotFound:
                pass
        try:
            message = await channel.send(embed=await self._panel_embed(guild, channel, room), view=TempVoiceOwnerView(self))
            await store.update_room(guild.id, channel.id, panel_message_id=message.id)
            room.panel_message_id = message.id
        except discord.Forbidden:
            logger.warning("Cannot restore Temp Voice panel in channel %s; grant Send Messages and Embed Links.", channel.id)
        except discord.HTTPException:
            logger.exception("Could not send/restore Temp Voice panel in channel %s.", channel.id)

    async def _panel_embed(self, guild: discord.Guild, channel: discord.VoiceChannel, room) -> discord.Embed:
        owner = guild.get_member(room.owner_id)
        count = len(self._human_members(channel))
        limit = room.user_limit or "∞"
        embed = discord.Embed(
            title="🔥 Your Chamber is Ready!", color=discord.Color.from_rgb(91, 55, 145),
            description="Manage your temporary voice chamber using the controls below.",
        )
        embed.add_field(name="🔊 Channel", value=channel.mention, inline=True)
        embed.add_field(name="👑 Owner", value=owner.mention if owner else f"<@{room.owner_id}>", inline=True)
        embed.add_field(name="🆔 Channel ID", value=f"`{channel.id}`", inline=True)
        embed.add_field(name="👥 Members", value=f"{count}/{limit}", inline=True)
        embed.add_field(name="🔒 Status", value="Locked" if room.locked else "Unlocked", inline=True)
        embed.set_footer(text="Valyria • Temporary Voice System")
        if owner:
            embed.set_thumbnail(url=owner.display_avatar.url)
        banner = await config_service.get_setting(guild.id, "temp_voice_banner")
        if banner:
            parsed = urlparse(str(banner))
            if parsed.scheme in {"https", "http"} and parsed.netloc:
                embed.set_image(url=str(banner))
        return embed

    async def dispatch_panel_action(self, interaction: discord.Interaction, action: str) -> None:
        try:
            room, channel, member, _staff = await self._authorize(
                interaction, require_panel=True, call_only=(action == "call")
            )
            if action == "call":
                await interaction.response.defer(ephemeral=True, thinking=True)
                return await self._call_staff(interaction, room, channel, member)
            if action == "rename":
                return await interaction.response.send_modal(TempVoiceRenameModal(self, room.channel_id))
            if action == "limit":
                return await interaction.response.send_modal(TempVoiceLimitModal(self, room.channel_id))
            if action in {"kick", "transfer"}:
                candidates = [m for m in self._human_members(channel) if m.id not in {room.owner_id, member.id}]
                if not candidates:
                    return await interaction.response.send_message("No eligible members are currently in this room.", ephemeral=True)
                prompt = {"kick": "Select a member to kick", "transfer": "Transfer ownership to"}[action]
                return await interaction.response.send_message(
                    prompt, view=TempVoiceMemberSelectView(self, room.channel_id, action, candidates), ephemeral=True,
                )
            if action in {"give_access", "ban"}:
                prompt = "Search this server for a member to give room access." if action == "give_access" else (
                    "Search this server for a member to ban from this room. Only a member currently in the room can be banned."
                )
                return await interaction.response.send_message(
                    prompt, view=TempVoiceSearchView(self, room.channel_id, action), ephemeral=True,
                )
            if action == "remove_access":
                access_ids = await store.list_room_access(room.guild_id, room.channel_id)
                access_ids = [uid for uid in access_ids if uid != room.owner_id]
                if not access_ids:
                    return await interaction.response.send_message("⚠️ No custom access members found.", ephemeral=True)
                options = []
                for user_id in access_ids[:25]:
                    target = interaction.guild.get_member(user_id)
                    label = target.display_name if target else f"Member {user_id}"
                    options.append(discord.SelectOption(label=label[:100], value=str(user_id)))
                view = discord.ui.View(timeout=120)
                view.add_item(TempVoiceAccessSelect(self, room.channel_id, options))
                return await interaction.response.send_message("Select a member whose custom access should be removed.", view=view, ephemeral=True)
            if action in {"lock", "unlock"}:
                await interaction.response.defer(ephemeral=True, thinking=True)
                return await self._set_locked(interaction, room, channel, action == "lock")
        except Exception as exc:
            logger.exception("Temp Voice panel action %s failed.", action)
            await self.reply(interaction, f"⚠️ The room action could not be completed: {exc}", ephemeral=True)

    async def _authorize(
        self, interaction: discord.Interaction, *, require_panel: bool,
        call_only: bool = False, channel_id: int | None = None,
    ):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            raise ValueError("This control only works in the server.")
        guild = interaction.guild
        channel_id = channel_id or interaction.channel_id
        if channel_id is None or interaction.channel_id != channel_id:
            raise ValueError("This control is no longer attached to its temporary room.")
        room = await store.get_room(guild.id, channel_id)
        channel = guild.get_channel(channel_id)
        member = interaction.user
        if room is None or not isinstance(channel, discord.VoiceChannel):
            raise ValueError("This temporary room no longer exists.")
        if require_panel and (interaction.message is None or room.panel_message_id != interaction.message.id):
            raise ValueError("This is an old or invalid room panel.")
        if member.bot:
            raise ValueError("Bots cannot use room controls.")
        if call_only:
            if member.voice is None or member.voice.channel is None or member.voice.channel.id != channel.id:
                raise ValueError("Join this temporary voice room before calling staff.")
            return room, channel, member, False

        owner = member.id == room.owner_id
        staff_override = bool(await config_service.get_setting(guild.id, "temp_voice_panel_staff_override"))
        staff = staff_override and await has_level(member, StaffLevel.STAFF)
        if not owner and not staff:
            raise PermissionError("Only this room's current owner or authorized staff may use these controls.")
        if not owner and staff:
            return room, channel, member, True
        if member.voice is None or member.voice.channel is None or member.voice.channel.id != channel.id:
            raise ValueError("Join your temporary voice room to use its controls.")
        if not channel.permissions_for(member).view_channel:
            raise PermissionError("You no longer have access to this room.")
        return room, channel, member, False

    async def _set_locked(self, interaction, room, channel, locked: bool) -> None:
        key = (room.guild_id, room.channel_id)
        async with self._room_locks.setdefault(key, asyncio.Lock()):
            fresh, live_channel, _member, _staff = await self._authorize(interaction, require_panel=True)
            # Re-read the room inside the lock: ownership or another room action may
            # have changed since the caller first loaded this record.
            room = fresh
            channel = live_channel
            everyone = live_channel.overwrites_for(interaction.guild.default_role)
            everyone.connect = not locked
            targets = [(interaction.guild.default_role, everyone)]
            for role_id in await self._staff_role_ids(room.guild_id):
                role = interaction.guild.get_role(role_id)
                if role is not None:
                    staff_overwrite = live_channel.overwrites_for(role)
                    staff_overwrite.view_channel = True
                    staff_overwrite.connect = True
                    targets.append((role, staff_overwrite))
            originals = [(target, live_channel.overwrites_for(target)) for target, _ in targets]
            try:
                for target, overwrite in targets:
                    await live_channel.set_permissions(target, overwrite=overwrite, reason="Temp Voice lock state")
                if not await store.update_room(room.guild_id, room.channel_id, locked=locked):
                    raise ValueError("Room record was removed.")
            except Exception:
                for target, original in originals:
                    await self._restore_overwrite(live_channel, target, original)
                raise
        await self.refresh_panel(interaction.guild, channel.id)
        title = "🔒 Temporary Voice Room Locked" if locked else "🔓 Temporary Voice Room Unlocked"
        await self._log_action(interaction.guild, title, room, user=interaction.user)
        await interaction.followup.send("🔒 Room locked." if locked else "🔓 Room unlocked.", ephemeral=True)

    async def safe_select_action(self, interaction: discord.Interaction, channel_id: int, action: str, target_id: int) -> None:
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            if interaction.guild is None:
                raise ValueError("This control only works in the server.")
            key = (interaction.guild.id, channel_id)
            async with self._room_locks.setdefault(key, asyncio.Lock()):
                room, channel, actor, _staff = await self._authorize(
                    interaction, require_panel=False, channel_id=channel_id,
                )
                target = interaction.guild.get_member(target_id)
                if action in {"kick", "transfer", "ban"}:
                    if target is None or target.bot or target.id == actor.id or target.id == room.owner_id:
                        raise ValueError("That member cannot be selected for this action.")
                    if target.voice is None or target.voice.channel is None or target.voice.channel.id != channel.id:
                        raise ValueError("⚠️ This member is no longer in the room.")
                if action == "kick":
                    await target.move_to(None, reason=f"Removed from temporary room by {actor}")
                    await self._log_action(interaction.guild, "🚫 Temporary Room Member Kicked", room, user=target, staff=actor)
                    text = f"✅ {target.mention} was removed from the room."
                elif action == "transfer":
                    if target.voice is None or target.voice.channel is None or target.voice.channel.id != channel.id:
                        raise ValueError("⚠️ Ownership transfer failed. The selected member may have left the room.")
                    if await store.is_room_banned(room.guild_id, room.channel_id, target.id):
                        raise ValueError("A room-banned member cannot become its owner.")
                    await self._transfer_ownership(interaction.guild, channel, room, target, automatic=False)
                    text = f"👑 Ownership transferred to {target.mention}."
                elif action == "ban":
                    removed = await self._ban_member(interaction.guild, channel, room, actor, target)
                    text = f"🔨 {target.mention} was banned from this room only." if removed else (
                        f"🔨 {target.mention} is room-banned, but I could not move them out. Check Move Members permission."
                    )
                elif action == "give_access":
                    if target is None or target.bot or target.id == room.owner_id:
                        raise ValueError("Choose a non-bot member other than the current owner.")
                    if await store.is_room_banned(room.guild_id, room.channel_id, target.id):
                        raise ValueError("Remove this member's room ban before granting access.")
                    await store.set_room_access(room.guild_id, room.channel_id, target.id, True)
                    try:
                        await self._set_member_access(channel, target, granted=True)
                    except Exception:
                        await store.set_room_access(room.guild_id, room.channel_id, target.id, False)
                        raise
                    await self._log_action(interaction.guild, "✅ Temporary Room Access Granted", room, user=target, staff=actor)
                    text = f"✅ {target.mention} can now access this room."
                elif action == "remove_access":
                    if target_id == room.owner_id or target_id not in await store.list_room_access(room.guild_id, room.channel_id):
                        raise ValueError("⚠️ No custom access entry exists for this member.")
                    if target is None:
                        try:
                            target = await interaction.guild.fetch_member(target_id)
                        except discord.NotFound:
                            target = None
                    await store.set_room_access(room.guild_id, room.channel_id, target_id, False)
                    try:
                        # Discord removes a departed member's overwrite automatically.
                        if target is not None:
                            await self._set_member_access(channel, target, granted=False)
                    except Exception:
                        await store.set_room_access(room.guild_id, room.channel_id, target_id, True)
                        raise
                    await self._log_action(interaction.guild, "❌ Temporary Room Access Removed", room, user=target, staff=actor)
                    text = "✅ The custom room access was removed."
                else:
                    raise ValueError("Unknown room action.")
            if action != "transfer":
                await self.refresh_panel(interaction.guild, channel_id)
            await interaction.followup.send(text, ephemeral=True)
        except Exception as exc:
            logger.exception("Temp Voice select action %s failed.", action)
            await self.reply(interaction, f"⚠️ {exc}", ephemeral=True)

    async def search_temp_voice_member(
        self, interaction: discord.Interaction, channel_id: int, action: str, query: str,
    ) -> None:
        """Resolve a typed ID or return substring matches from this guild for the existing action select."""
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            if interaction.guild is None:
                raise ValueError("This search only works inside the server.")
            await self._authorize(interaction, require_panel=False, channel_id=channel_id)
            query = query.strip()
            if not query:
                raise ValueError("Enter a username, display name, or User ID.")

            if query.isdigit():
                try:
                    matches = [await interaction.guild.fetch_member(int(query))]
                except discord.NotFound:
                    return await interaction.followup.send(
                        "⚠️ No member with that ID was found in this server.", ephemeral=True,
                    )
            else:
                normalized = query.casefold()
                matches = [
                    member async for member in interaction.guild.fetch_members(limit=None)
                    if normalized in member.name.casefold()
                    or normalized in member.display_name.casefold()
                ]

            if not matches:
                return await interaction.followup.send(
                    "⚠️ No server members match that username or display name.", ephemeral=True,
                )
            return await interaction.followup.send(
                f"Select a matching member ({min(len(matches), 25)} shown):",
                view=TempVoiceMemberSelectView(self, channel_id, action, matches[:25]),
                ephemeral=True,
            )
        except Exception as exc:
            logger.exception("Temp Voice member search for %s failed.", action)
            await self.reply(interaction, f"⚠️ {exc}", ephemeral=True)

    async def safe_modal_action(self, interaction: discord.Interaction, channel_id: int, action: str, value: str) -> None:
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
            if interaction.guild is None:
                raise ValueError("This control only works in the server.")
            async with self._room_locks.setdefault((interaction.guild.id, channel_id), asyncio.Lock()):
                room, channel, actor, _staff = await self._authorize(interaction, require_panel=False, channel_id=channel_id)
                if action == "rename":
                    name = value.strip()
                    if not name:
                        raise ValueError("Room name cannot be empty.")
                    if len(name) > 100:
                        raise ValueError("Room names are limited to 100 characters.")
                    old_name = channel.name
                    await channel.edit(name=name, reason=f"Temporary room renamed by {actor}")
                    try:
                        if not await store.update_room(room.guild_id, room.channel_id, name=name):
                            raise ValueError("Room record was removed.")
                    except Exception:
                        await channel.edit(name=old_name, reason="Rollback failed Temp Voice rename")
                        raise
                    text = f"✅ Room renamed to **{discord.utils.escape_markdown(name)}**."
                    await self._log_action(interaction.guild, "✏️ Temporary Voice Room Renamed", room, user=actor, reason=name)
                elif action == "limit":
                    try:
                        limit = int(value.strip())
                    except ValueError:
                        raise ValueError("Enter a number from 0 to 99.") from None
                    if not 0 <= limit <= 99:
                        raise ValueError("Enter a number from 0 to 99. Use 0 for unlimited.")
                    old_limit = channel.user_limit
                    await channel.edit(user_limit=limit, reason=f"Temporary room limit changed by {actor}")
                    try:
                        if not await store.update_room(room.guild_id, room.channel_id, user_limit=limit):
                            raise ValueError("Room record was removed.")
                    except Exception:
                        await channel.edit(user_limit=old_limit, reason="Rollback failed Temp Voice user limit change")
                        raise
                    await self._log_action(interaction.guild, "👥 Temporary Voice User Limit Changed", room, user=actor, reason=str(limit))
                    text = f"✅ User limit set to {'unlimited' if limit == 0 else limit}."
                else:
                    raise ValueError("Unknown room action.")
            await self.refresh_panel(interaction.guild, channel_id)
            await interaction.followup.send(text, ephemeral=True)
        except Exception as exc:
            logger.exception("Temp Voice modal action %s failed.", action)
            await self.reply(interaction, f"⚠️ {exc}", ephemeral=True)

    async def _call_staff(self, interaction, room, channel, requester) -> None:
        alert_id = await config_service.get_setting(room.guild_id, "temp_voice_staff_alert")
        alert_channel = interaction.guild.get_channel(alert_id) if alert_id else None
        if not isinstance(alert_channel, discord.TextChannel):
            return await interaction.followup.send("⚠️ Staff Alert channel is not configured.", ephemeral=True)
        cooldown = max(0, int(await config_service.get_setting(room.guild_id, "temp_voice_staff_call_cooldown") or 0))
        key = (room.guild_id, room.channel_id, requester.id)
        now = asyncio.get_running_loop().time()
        remaining = self._staff_call_until.get(key, 0) - now
        if remaining > 0:
            return await interaction.followup.send(
                f"⏳ Staff has already been notified. Please wait {int(remaining + 0.999)} seconds before calling again.",
                ephemeral=True,
            )
        if cooldown:
            self._staff_call_until[key] = now + cooldown
        embed = discord.Embed(
            title="🚨 STAFF ASSISTANCE REQUEST", color=discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="👤 Requested By", value=requester.mention, inline=True)
        embed.add_field(name="🔊 Temporary Room", value=channel.mention, inline=True)
        embed.add_field(name="👑 Owner", value=f"<@{room.owner_id}>", inline=True)
        embed.add_field(name="🆔 Room ID", value=f"`{channel.id}`", inline=True)
        embed.add_field(name="👥 Members", value=str(len(self._human_members(channel))), inline=True)
        embed.add_field(name="🔒 Status", value="Locked" if room.locked else "Unlocked", inline=True)
        embed.add_field(name="📍 Guild", value=interaction.guild.name, inline=False)
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(
            label="Open Room", style=discord.ButtonStyle.link,
            url=f"https://discord.com/channels/{room.guild_id}/{room.channel_id}",
        ))
        try:
            await alert_channel.send(embed=embed, view=view)
        except discord.HTTPException:
            self._staff_call_until.pop(key, None)
            logger.exception("Could not send Temp Voice staff alert for room %s.", channel.id)
            return await interaction.followup.send("⚠️ I could not send the Staff Alert. Check my channel permissions.", ephemeral=True)
        await self._log_action(interaction.guild, "📞 Temporary Room Staff Called", room, user=requester)
        await interaction.followup.send("✅ Staff has been notified.", ephemeral=True)

    async def _ban_member(self, guild, channel, room, actor, target) -> bool:
        await store.set_room_ban(guild.id, channel.id, target.id, True)
        original = channel.overwrites_for(target)
        overwrite = discord.PermissionOverwrite.from_pair(*original.pair())
        overwrite.view_channel = True
        overwrite.connect = False
        try:
            await channel.set_permissions(target, overwrite=overwrite, reason=f"Banned from temporary room by {actor}")
        except Exception:
            await store.set_room_ban(guild.id, channel.id, target.id, False)
            raise
        removed = True
        if target.voice and target.voice.channel and target.voice.channel.id == channel.id:
            try:
                await target.move_to(None, reason=f"Banned from temporary room by {actor}")
            except (discord.Forbidden, discord.HTTPException):
                removed = False
        await self._log_action(guild, "🔨 Temporary Room Member Banned", room, user=target, staff=actor)
        return removed

    async def _set_member_access(self, channel, member, *, granted: bool) -> None:
        overwrite = channel.overwrites_for(member)
        overwrite.view_channel = True if granted else None
        overwrite.connect = True if granted else None
        if all(value is None for _, value in overwrite):
            await channel.set_permissions(member, overwrite=None, reason="Temporary room access removed")
        else:
            await channel.set_permissions(member, overwrite=overwrite, reason="Temporary room custom access")

    async def _transfer_ownership(self, guild, channel, room, new_owner, *, automatic: bool) -> None:
        if new_owner.bot or new_owner.id == room.owner_id or not new_owner.voice or not new_owner.voice.channel or new_owner.voice.channel.id != channel.id:
            raise ValueError("Ownership transfer failed. The selected member may have left the room.")
        if await store.is_room_banned(guild.id, channel.id, new_owner.id):
            raise ValueError("A room-banned member cannot own this room.")
        old_id = room.owner_id
        old_owner = guild.get_member(old_id)
        old_overwrite = channel.overwrites_for(old_owner) if old_owner else None
        new_overwrite = channel.overwrites_for(new_owner)
        desired_new = discord.PermissionOverwrite.from_pair(*new_overwrite.pair())
        desired_new.view_channel = True
        desired_new.connect = True
        try:
            if old_owner is not None:
                await self._set_member_access(
                    channel, old_owner,
                    granted=old_id in await store.list_room_access(guild.id, channel.id),
                )
            await channel.set_permissions(new_owner, overwrite=desired_new, reason="Temporary room ownership transfer")
            if not await store.update_room(guild.id, channel.id, owner_id=new_owner.id):
                raise ValueError("Room record was removed during ownership transfer.")
        except Exception:
            if old_owner is not None:
                await self._restore_overwrite(channel, old_owner, old_overwrite)
            await self._restore_overwrite(channel, new_owner, new_overwrite)
            raise
        room.owner_id = new_owner.id
        await self.refresh_panel(guild, channel.id)
        title = "👑 Temporary Room Ownership Automatically Transferred" if automatic else "👑 Temporary Room Ownership Transferred"
        await self._log_action(guild, title, room, user=new_owner, target=old_owner)

    @staticmethod
    async def _restore_overwrite(channel, target, overwrite) -> None:
        if overwrite is None or all(value is None for _, value in overwrite):
            await channel.set_permissions(target, overwrite=None, reason="Rollback temporary room permission change")
        else:
            await channel.set_permissions(target, overwrite=overwrite, reason="Rollback temporary room permission change")

    async def _apply_room_state(self, guild, channel, room) -> None:
        everyone = channel.overwrites_for(guild.default_role)
        everyone.view_channel = True
        everyone.connect = not room.locked
        await channel.set_permissions(guild.default_role, overwrite=everyone, reason="Restore Temp Voice room state")
        owner = guild.get_member(room.owner_id)
        if owner:
            overwrite = channel.overwrites_for(owner)
            overwrite.view_channel = True
            overwrite.connect = True
            await channel.set_permissions(owner, overwrite=overwrite, reason="Restore Temp Voice owner access")
        for role_id in await self._staff_role_ids(guild.id):
            role = guild.get_role(role_id)
            if role:
                overwrite = channel.overwrites_for(role)
                overwrite.view_channel = True
                overwrite.connect = True
                await channel.set_permissions(role, overwrite=overwrite, reason="Restore authorized staff room access")
        for user_id in await store.list_room_access(guild.id, channel.id):
            member = guild.get_member(user_id)
            if member:
                overwrite = channel.overwrites_for(member)
                overwrite.view_channel = True
                overwrite.connect = True
                await channel.set_permissions(member, overwrite=overwrite, reason="Restore Temp Voice custom access")
        banned_ids = await store.list_room_bans(guild.id, channel.id)
        for user_id in banned_ids:
            member = guild.get_member(user_id)
            if member:
                overwrite = channel.overwrites_for(member)
                overwrite.view_channel = True
                overwrite.connect = False
                await channel.set_permissions(member, overwrite=overwrite, reason="Restore Temp Voice room ban")

    async def _staff_role_ids(self, guild_id: int) -> set[int]:
        values = await asyncio.gather(*(config_service.get_setting(guild_id, key) for key in STAFF_ROLE_KEYS))
        return {int(value) for value in values if value}

    async def refresh_panel(self, guild: discord.Guild, channel_id: int) -> None:
        room = await store.get_room(guild.id, channel_id)
        channel = guild.get_channel(channel_id)
        if room is None or not isinstance(channel, discord.VoiceChannel):
            return
        await self._ensure_panel(guild, channel, room)

    async def _cleanup_missing_room(self, guild_id: int, channel_id: int) -> None:
        room = await store.get_room(guild_id, channel_id)
        if room is not None and await store.delete_room(guild_id, channel_id):
            guild = self.bot.get_guild(guild_id)
            if guild:
                await self._log_action(guild, "🧹 Temporary Voice Orphan Record Cleaned", room, reason="Channel no longer exists")
        self._pending_delete.pop((guild_id, channel_id), None)
        self._room_locks.pop((guild_id, channel_id), None)

    async def _log_action(
        self, guild: discord.Guild, title: str, room, *,
        user: discord.abc.User | None = None, target: discord.abc.User | None = None,
        staff: discord.abc.User | None = None, reason: str | None = None,
    ) -> None:
        channel = guild.get_channel(room.channel_id)
        try:
            await send_log(guild, LogEvent(
                LogType.VOICE, title, user=user, target=target, staff=staff, reason=reason,
                fields=[
                    ("Room", channel.mention if channel else f"<#{room.channel_id}>", True),
                    ("Owner", f"<@{room.owner_id}>", True),
                    ("Room ID", str(room.channel_id), True),
                ],
                ids={"Channel ID": room.channel_id, "Owner ID": room.owner_id},
            ))
        except Exception:
            logger.exception("Could not write Temp Voice action log (%s).", title)

    @staticmethod
    async def reply(interaction: discord.Interaction, content: str, *, ephemeral: bool = True) -> None:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(content, ephemeral=ephemeral)
            else:
                await interaction.response.send_message(content, ephemeral=ephemeral)
        except discord.HTTPException:
            logger.debug("Could not send Temp Voice interaction response.", exc_info=True)


class TempVoiceAccessSelect(discord.ui.Select):
    def __init__(self, cog: TempVoiceCog, channel_id: int, options: list[discord.SelectOption]):
        super().__init__(
            placeholder="Select custom access to remove", min_values=1, max_values=1,
            options=options, custom_id=f"temp_voice:remove_access:{channel_id}",
        )
        self.cog = cog
        self.channel_id = channel_id

    async def callback(self, interaction: discord.Interaction):
        await self.cog.safe_select_action(
            interaction, self.channel_id, "remove_access", int(self.values[0])
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TempVoiceCog(bot))
