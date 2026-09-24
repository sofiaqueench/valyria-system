"""
مستمعو الأحداث (Listeners) التي تُرسل سجلات Discord: الأعضاء، الرسائل، حالة البوت.
هذا الملف لا يبني Embeds ولا يعرف الرومات: كل ذلك في logging_service.
يحتوي أيضًا أمر /logs test لاختبار الرومات.
"""
import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from bot.services.config_service import get_setting
from bot.services.logging_service import (
    LogEvent,
    LogType,
    SendResult,
    broadcast,
    find_audit_executor,
    format_user,
    is_log_channel,
    send_log,
    truncate,
)

MAX_CONTENT = 1000  # حقل الـ Embed لا يقبل أكثر من 1024 حرفًا
AUDIT_DELAY = 1.0   # Discord قد يتأخر ثانية في كتابة Audit Log
CONTENT_DISABLED = "(content logging is disabled in settings)"


class LogEventsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._start_announced = False  # on_ready قد يتكرر عند إعادة الاتصال

    logs = app_commands.Group(
        name="logs",
        description="أدوات نظام السجلات",
        guild_only=True,
        default_permissions=discord.Permissions(administrator=True),
    )

    # ================================================================ BOT
    @commands.Cog.listener()
    async def on_ready(self):
        if self._start_announced:
            return
        self._start_announced = True
        event = LogEvent(
            LogType.BOT,
            "🟢 Bot Started",
            description=f"{self.bot.user} is online.",
            fields=[
                ("Latency", f"{round(self.bot.latency * 1000)} ms", True),
                ("Servers", str(len(self.bot.guilds)), True),
            ],
        )
        await broadcast(self.bot.guilds, event)

    # ============================================================= MESSAGES
    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        # نستعمل الحدث الخام (raw) لأنه يصلنا حتى لو الرسالة ليست في الكاش
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None or await is_log_channel(guild.id, payload.channel_id):
            return

        message = payload.cached_message  # None إذا لم تكن الرسالة محفوظة في ذاكرة البوت
        if message is not None and message.author.bot:
            if await get_setting(guild.id, "message_log_ignore_bots"):
                return

        channel = guild.get_channel_or_thread(payload.channel_id)
        channel_text = channel.mention if channel else f"<#{payload.channel_id}>"
        sent_at = discord.utils.snowflake_time(payload.message_id)  # وقت الإرسال يُستخرج من الـ ID

        fields: list[tuple[str, str, bool]] = []
        ids: dict[str, int | str] = {"Message ID": payload.message_id, "Channel ID": payload.channel_id}
        author = None

        if message is None:
            fields.append(("Author", "Unknown — the message was not in the bot's cache", True))
        fields.append(("Channel", channel_text, True))
        fields.append(("Sent", discord.utils.format_dt(sent_at, "F"), True))

        if message is None:
            fields.append((
                "Content",
                "Unavailable — Discord doesn't send the content of deleted messages. "
                "The message was sent before the bot started, or is too old for its cache.",
                False,
            ))
            deleter = "Unknown — the author isn't known, so the Audit Log can't be searched."
        else:
            author = message.author
            ids["User ID"] = author.id
            if await get_setting(guild.id, "message_log_include_content"):
                content = message.content or "(no text content)"
            else:
                content = CONTENT_DISABLED
            fields.append(("Content", truncate(content, MAX_CONTENT), False))
            if message.attachments:
                names = ", ".join(attachment.filename for attachment in message.attachments)
                fields.append(("Attachments", names, False))
            deleter = await self._describe_message_deleter(guild, message)

        if deleter:
            fields.append(("Deleted by", deleter, False))

        await send_log(guild, LogEvent(
            LogType.MESSAGE, "🗑️ Message Deleted",
            user=author, user_label="Author", fields=fields, ids=ids,
        ))

    async def _describe_message_deleter(self, guild: discord.Guild, message: discord.Message) -> str | None:
        if not await get_setting(guild.id, "message_log_use_audit_log"):
            return None  # الميزة مطفأة من الإعدادات

        await asyncio.sleep(AUDIT_DELAY)
        executor, status = await find_audit_executor(
            guild,
            discord.AuditLogAction.message_delete,
            message.author.id,
            channel_id=message.channel.id,
        )
        if status == "found":
            return f"{format_user(executor)} (from the Audit Log)"
        if status == "no_permission":
            return "Unknown — the bot lacks the View Audit Log permission."
        if status == "error":
            return "Unknown — the Audit Log could not be read."
        return (
            "Unknown — no matching Audit Log entry. Discord doesn't record members deleting "
            "their own messages, so it was probably deleted by the author."
        )

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        # يصلنا فقط للرسائل الموجودة في الكاش (لأننا نحتاج النص القديم)
        if after.guild is None or before.content == after.content:
            return  # الرسائل الخاصة، أو تغيّر Embed/تثبيت بدون تغيير النص
        guild = after.guild
        if after.author.bot and await get_setting(guild.id, "message_log_ignore_bots"):
            return
        if await is_log_channel(guild.id, after.channel.id):
            return

        if await get_setting(guild.id, "message_log_include_content"):
            before_text = truncate(before.content or "(empty)", MAX_CONTENT)
            after_text = truncate(after.content or "(empty)", MAX_CONTENT)
        else:
            before_text = after_text = CONTENT_DISABLED

        await send_log(guild, LogEvent(
            LogType.MESSAGE, "✏️ Message Edited",
            description=f"[Jump to message]({after.jump_url})",
            user=after.author, user_label="Author",
            fields=[
                ("Channel", after.channel.mention, True),
                ("Before", before_text, False),
                ("After", after_text, False),
            ],
            ids={"Message ID": after.id, "Channel ID": after.channel.id, "User ID": after.author.id},
        ))

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None or await is_log_channel(guild.id, payload.channel_id):
            return

        channel = guild.get_channel_or_thread(payload.channel_id)
        fields = [
            ("Channel", channel.mention if channel else f"<#{payload.channel_id}>", True),
            ("Messages deleted", str(len(payload.message_ids)), True),
            ("Still in cache", str(len(payload.cached_messages)), True),
        ]

        staff = None
        if await get_setting(guild.id, "message_log_use_audit_log"):
            await asyncio.sleep(AUDIT_DELAY)
            executor, status = await find_audit_executor(
                guild, discord.AuditLogAction.message_bulk_delete, payload.channel_id
            )
            if status == "found":
                staff = executor
            else:
                fields.append(("Deleted by", "Unknown — no matching Audit Log entry (or no permission).", False))

        await send_log(guild, LogEvent(
            LogType.MESSAGE, "🗑️ Bulk Messages Deleted",
            staff=staff, fields=fields, ids={"Channel ID": payload.channel_id},
        ))

    # =============================================================== MEMBERS
    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await send_log(member.guild, LogEvent(
            LogType.MEMBER, "📥 Member Joined",
            user=member,
            fields=[
                ("Account created", discord.utils.format_dt(member.created_at, "R"), True),
                ("Member count", str(member.guild.member_count), True),
                ("Bot account", "Yes" if member.bot else "No", True),
            ],
            thumbnail_url=member.display_avatar.url,
        ))

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        roles = [role.mention for role in member.roles if not role.is_default()]
        fields = [("Member count", str(member.guild.member_count), True)]
        if member.joined_at:
            fields.append(("Joined", discord.utils.format_dt(member.joined_at, "R"), True))
        fields.append(("Roles", ", ".join(roles) if roles else "None", False))

        await send_log(member.guild, LogEvent(
            LogType.MEMBER, "📤 Member Left",
            user=member, fields=fields, thumbnail_url=member.display_avatar.url,
        ))

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        guild = after.guild

        added = [role for role in after.roles if role not in before.roles]
        removed = [role for role in before.roles if role not in after.roles]
        if added or removed:
            fields = []
            if added:
                fields.append(("Roles added", ", ".join(role.mention for role in added), False))
            if removed:
                fields.append(("Roles removed", ", ".join(role.mention for role in removed), False))
            await asyncio.sleep(AUDIT_DELAY)
            executor, _ = await find_audit_executor(guild, discord.AuditLogAction.member_role_update, after.id)
            await send_log(guild, LogEvent(
                LogType.MEMBER, "🏷️ Roles Updated", user=after, staff=executor, fields=fields,
            ))

        if before.nick != after.nick:
            await asyncio.sleep(AUDIT_DELAY)
            executor, _ = await find_audit_executor(
                guild,
                discord.AuditLogAction.member_update,
                after.id,
                predicate=lambda entry: hasattr(entry.after, "nick"),  # نتأكد أن التغيير هو الاسم فعلًا
            )
            fields = [
                ("Before", before.nick or "(none)", True),
                ("After", after.nick or "(none)", True),
            ]
            staff = None
            if executor is not None and executor.id == after.id:
                fields.append(("Changed by", "The member themself", True))
            else:
                staff = executor
            await send_log(guild, LogEvent(
                LogType.MEMBER, "✏️ Nickname Changed", user=after, staff=staff, fields=fields,
            ))

    # ================================================================ VOICE STATE (Mute/Deafen)
    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ):
        # server mute/deafen فقط (وليس self_mute/self_deaf التي يتحكم بها العضو بنفسه)
        if before.mute != after.mute:
            await self._log_voice_mod_change(
                member, attr="mute", new_value=after.mute,
                title="🔇 Server Muted" if after.mute else "🔊 Server Unmuted",
            )
        if before.deaf != after.deaf:
            await self._log_voice_mod_change(
                member, attr="deaf", new_value=after.deaf,
                title="🔇 Server Deafened" if after.deaf else "🔊 Server Undeafened",
            )

    async def _log_voice_mod_change(
        self, member: discord.Member, *, attr: str, new_value: bool, title: str
    ) -> None:
        guild = member.guild
        await asyncio.sleep(AUDIT_DELAY)
        executor, status = await find_audit_executor(
            guild,
            discord.AuditLogAction.member_update,
            member.id,
            predicate=lambda entry: getattr(entry.after, attr, None) == new_value,
        )

        fields: list[tuple[str, str, bool]] = []
        staff = None
        if status == "found":
            staff = executor
        else:
            # لا نخترع من قام بالفعل — نوضح أنه غير معروف
            fields.append(("Actor", "Unknown / Unavailable", False))

        await send_log(guild, LogEvent(
            LogType.MEMBER, title, user=member, staff=staff, fields=fields,
            ids={"User ID": member.id},
        ))


    @app_commands.checks.has_permissions(administrator=True)
    async def test(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)  # الإرسال قد يأخذ أكثر من 3 ثوانٍ

        lines = []
        for log_type in LogType:
            result = await send_log(interaction.guild, LogEvent(
                log_type,
                f"🧪 Test Log — {log_type.name.title()}",
                description=f"Requested by {interaction.user.mention}. If you can read this, `{log_type.value}` works.",
            ))
            if result is SendResult.SENT:
                lines.append(f"✅ `{log_type.value}` — تم الإرسال")
            elif result is SendResult.NOT_CONFIGURED:
                lines.append(f"⚪ `{log_type.value}` — غير مضبوط (`/config set-channel`)")
            else:
                lines.append(f"❌ `{log_type.value}` — فشل (تحقق من صلاحيات البوت في الروم)")

        await interaction.followup.send("\n".join(lines), ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(LogEventsCog(bot))
