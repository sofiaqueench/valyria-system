"""
سجل الإعدادات (Settings Registry) — المكان الوحيد الذي تُعرَّف فيه كل إعدادات البوت.

كل إعداد له: مفتاح، نوع، مجموعة، وصف، وقيمة افتراضية.
- القيم الافتراضية تُغيَّر هنا (في هذا الملف).
- قيم كل سيرفر تُغيَّر من Discord بأمر /config (وتُحفظ في جدول guild_settings).

لإضافة إعداد جديد في أي Phase قادم: أضف سطرًا واحدًا في القائمة SETTING_LIST
بدون أي Migration.
"""
from dataclasses import dataclass
from enum import Enum

from bot.core.errors import ConfigError, UnknownSettingError
from bot.config.game_registry import GAMES

MAX_STR_LENGTH = 4000

_TRUE_WORDS = {"true", "yes", "on", "1", "نعم"}
_FALSE_WORDS = {"false", "no", "off", "0", "لا"}


class Kind(str, Enum):
    TEXT_CHANNEL = "text_channel"
    VOICE_CHANNEL = "voice_channel"
    CATEGORY = "category"
    ROLE = "role"
    INT = "int"
    BOOL = "bool"
    STR = "str"
    CHOICE = "choice"


CHANNEL_KINDS = frozenset({Kind.TEXT_CHANNEL, Kind.VOICE_CHANNEL, Kind.CATEGORY})
ID_KINDS = CHANNEL_KINDS | {Kind.ROLE}
VALUE_KINDS = frozenset({Kind.INT, Kind.BOOL, Kind.STR, Kind.CHOICE})

# ترتيب المجموعات هو ترتيب ظهورها في /config view
GROUPS: dict[str, str] = {
    "roles": "👥 رتب الطاقم",
    "verification": "✅ التحقق",
    "welcome": "👋 الترحيب",
    "logs": "📜 السجلات",
    "xp": "⭐ XP الأعضاء",
    "voice": "🔊 الصوت الدائم",
    "staff_xp": "🛡️ XP الطاقم",
    "staff_games": "🎮 ألعاب الطاقم",
    "phase14": "🛡️ Moderation & Reactions",
    "temp_voice": "🔊 الرومات المؤقتة",
    "tickets": "🎫 التذاكر",
    "applications": "📝 الطلبات والاستئنافات",
    "moderation": "🔨 الإشراف",
    "automod": "🤖 AutoMod",
    "clean": "🧹 Clean Messages",
    "community": "🌐 Community",
    "security": "🚨 الأمان",
    "jail": "🔒 Jail",
}


@dataclass(frozen=True)
class SettingDef:
    key: str
    kind: Kind
    group: str
    description: str
    default: int | bool | str | None = None
    min_value: int | None = None
    max_value: int | None = None
    choices: tuple[str, ...] = ()

    def parse_input(self, text: str) -> int | bool | str:
        """يحوّل نصًا كتبه الأدمن إلى قيمة من النوع الصحيح (أو يرفع ConfigError)."""
        text = text.strip()

        if self.kind is Kind.INT:
            try:
                number = int(text)
            except ValueError:
                raise ConfigError("القيمة يجب أن تكون رقمًا صحيحًا.") from None
            return self.validate(number)

        if self.kind is Kind.BOOL:
            lowered = text.lower()
            if lowered in _TRUE_WORDS:
                return True
            if lowered in _FALSE_WORDS:
                return False
            raise ConfigError("القيمة يجب أن تكون: true أو false (أو نعم / لا).")

        return self.validate(text)  # STR أو CHOICE

    def validate(self, value: object) -> int | bool | str:
        """يتأكد أن القيمة صالحة لهذا الإعداد ويرجعها بعد التنظيف."""
        if self.kind is Kind.INT:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigError("القيمة يجب أن تكون رقمًا صحيحًا.")
            if self.min_value is not None and value < self.min_value:
                raise ConfigError(f"أقل قيمة مسموحة هي {self.min_value}.")
            if self.max_value is not None and value > self.max_value:
                raise ConfigError(f"أكبر قيمة مسموحة هي {self.max_value}.")
            return value

        if self.kind is Kind.BOOL:
            if not isinstance(value, bool):
                raise ConfigError("القيمة يجب أن تكون true أو false.")
            return value

        if self.kind is Kind.STR:
            if not isinstance(value, str) or not value.strip():
                raise ConfigError("النص لا يمكن أن يكون فارغًا.")
            value = value.strip()
            if len(value) > MAX_STR_LENGTH:
                raise ConfigError(f"النص طويل جدًا (الحد الأقصى {MAX_STR_LENGTH} حرف).")
            return value

        if self.kind is Kind.CHOICE:
            lowered = str(value).strip().lower()
            if lowered not in self.choices:
                raise ConfigError("الخيارات المتاحة: " + " / ".join(self.choices))
            return lowered

        # الرومات والرتب: معرّف رقمي موجب
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ConfigError("المعرّف غير صالح.")
        return value

    def to_storage(self, value: int | bool | str) -> str:
        """القيمة → نص للحفظ في قاعدة البيانات."""
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    def from_storage(self, raw: str) -> int | bool | str:
        """نص من قاعدة البيانات → قيمة من النوع الصحيح (يرفع ValueError إذا كان تالفًا)."""
        if self.kind is Kind.BOOL:
            if raw == "true":
                return True
            if raw == "false":
                return False
            raise ValueError(f"قيمة bool تالفة: {raw!r}")
        if self.kind is Kind.INT or self.kind in ID_KINDS:
            return int(raw)
        return raw


# ---------- دوال مساعدة لتعريف الإعدادات بسطر واحد ----------
def _text(key: str, group: str, desc: str) -> SettingDef:
    return SettingDef(key, Kind.TEXT_CHANNEL, group, desc)


def _voice(key: str, group: str, desc: str) -> SettingDef:
    return SettingDef(key, Kind.VOICE_CHANNEL, group, desc)


def _category(key: str, group: str, desc: str) -> SettingDef:
    return SettingDef(key, Kind.CATEGORY, group, desc)


def _role(key: str, group: str, desc: str) -> SettingDef:
    return SettingDef(key, Kind.ROLE, group, desc)


def _int(key: str, group: str, desc: str, default: int, lo: int, hi: int) -> SettingDef:
    return SettingDef(key, Kind.INT, group, desc, default, min_value=lo, max_value=hi)


def _bool(key: str, group: str, desc: str, default: bool) -> SettingDef:
    return SettingDef(key, Kind.BOOL, group, desc, default)


def _str(key: str, group: str, desc: str, default: str) -> SettingDef:
    return SettingDef(key, Kind.STR, group, desc, default)


def _choice(key: str, group: str, desc: str, default: str, choices: tuple[str, ...]) -> SettingDef:
    return SettingDef(key, Kind.CHOICE, group, desc, default, choices=choices)


_WARN_ACTIONS = ("none", "timeout", "jail", "kick", "ban")

# =====================================================================
#                          قائمة الإعدادات
# =====================================================================
SETTING_LIST: list[SettingDef] = [
    # ---- رتب الطاقم (يستعملها نظام Permissions في Phase 6) ----
    _role("role_owner", "roles", "رتبة المالك"),
    _role("role_admin", "roles", "رتبة الأدمن"),
    _role("role_head_moderator", "roles", "رتبة رئيس المشرفين"),
    _role("role_moderator", "roles", "رتبة المشرف"),
    _role("role_trial_moderator", "roles", "رتبة مشرف تحت التجربة"),
    _role("role_support", "roles", "رتبة الدعم"),
    _role("role_staff", "roles", "رتبة الطاقم العامة"),

    # ---- التحقق ----
    _text("verification_channel", "verification", "روم التحقق"),
    _text("verification_rules_channel", "verification", "روم قوانين التحقق"),
    _voice("verification_voice", "verification", "روم التحقق الصوتي"),
    _text("verification_staff_channel", "verification", "روم إشعارات الطاقم للتحقق"),
    _role("role_verified", "verification", "رتبة Verified"),
    _role("role_unverified", "verification", "رتبة Unverified"),
    _text("verification_panel_channel", "verification", "Verification panel channel"),
    _role("role_verification_team", "verification", "Verification Team role"),
    _role("role_male", "verification", "Male role"),
    _role("role_female", "verification", "Female role"),
    _bool("verification_dm_enabled", "verification", "DM Verification Team on voice requests", True),
    _int("verification_request_cooldown", "verification", "Verification DM cooldown seconds", 300, 30, 3600),

    # ---- الترحيب ----
    _bool("welcome_enabled", "welcome", "تفعيل رسالة الترحيب", True),
    _text("welcome_channel", "welcome", "روم الترحيب"),
    _str("welcome_message", "welcome", "نص الترحيب ({mention} {username} {server} {member_count} {inviter})",
         "أهلاً بك {mention} في **{server}**! أنت العضو رقم {member_count}."),
    _bool("welcome_show_avatar", "welcome", "إظهار صورة العضو", True),
    _bool("welcome_show_inviter", "welcome", "إظهار من دعا العضو", True),
    _str("welcome_embed_title", "welcome", "عنوان بطاقة الترحيب", "{WELCOME_EMOJI_WELCOME} Welcome to {server}"),
    _int("welcome_embed_color", "welcome", "لون بطاقة الترحيب (رقم عشري)", 5793266, 0, 16777215),
    _str("welcome_embed_footer", "welcome", "تذييل بطاقة الترحيب", "Valyria • {member_count} members"),
    _bool("welcome_embed_timestamp", "welcome", "إظهار وقت الترحيب", True),
    _bool("welcome_use_server_banner", "welcome", "إظهار بانر السيرفر", True),
    SettingDef("welcome_fallback_image_url", Kind.STR, "welcome", "رابط صورة بديلة عند غياب البانر", None),
    _bool("welcome_use_member_avatar", "welcome", "إظهار صورة العضو في بطاقة الترحيب", True),
    _bool("welcome_use_server_icon", "welcome", "إظهار أيقونة السيرفر في التذييل", True),
    SettingDef("welcome_emoji_welcome", Kind.STR, "welcome", "إيموجي الترحيب المخصص", None),
    SettingDef("welcome_emoji_member", Kind.STR, "welcome", "إيموجي العضو المخصص", None),
    SettingDef("welcome_emoji_server", Kind.STR, "welcome", "إيموجي السيرفر المخصص", None),
    SettingDef("welcome_emoji_rules", Kind.STR, "welcome", "إيموجي القوانين المخصص", None),
    SettingDef("welcome_emoji_verification", Kind.STR, "welcome", "إيموجي التحقق المخصص", None),

    # ---- السجلات ----
    _text("log_member", "logs", "سجل الأعضاء"),
    _text("log_verification", "logs", "سجل التحقق"),
    _text("log_moderation", "logs", "سجل الإشراف"),
    _text("log_ticket", "logs", "سجل التذاكر"),
    _text("log_voice", "logs", "سجل الصوت"),
    _text("log_invite", "logs", "سجل الدعوات"),
    _text("log_xp", "logs", "سجل XP"),
    _text("log_message", "logs", "سجل الرسائل (حذف/تعديل)"),
    _text("log_security", "logs", "سجل الأمان"),
    _text("log_bot", "logs", "سجل البوت"),
    _text("log_channel", "logs", "سجل القنوات (إنشاء/حذف)"),
    _text("log_role", "logs", "سجل الرتب (إنشاء/حذف/تعديل)"),
    _bool("message_log_include_content", "logs", "تسجيل محتوى الرسالة المحذوفة", True),
    _bool("message_log_ignore_bots", "logs", "تجاهل رسائل البوتات", True),
    _bool("message_log_use_audit_log", "logs", "محاولة معرفة من حذف الرسالة (Audit Log)", True),

    # ---- XP الأعضاء ----
    _int("message_xp_min", "xp", "أقل XP للرسالة", 15, 1, 1000),
    _int("message_xp_max", "xp", "أكبر XP للرسالة", 25, 1, 1000),
    _int("message_xp_cooldown", "xp", "ثواني الانتظار بين رسالتين تعطيان XP", 60, 0, 3600),
    _int("voice_xp_interval", "xp", "كل كم ثانية يُحتسب XP الصوت", 60, 30, 3600),
    _int("voice_xp_amount", "xp", "XP الصوت في كل فترة (0 = تعطيل)", 10, 0, 1000),
    _bool("voice_xp_ignore_deafened", "xp", "لا XP للعضو الذي أغلق السماعة (AFK)", True),
    _voice("permanent_voice_channel", "voice", "الروم الصوتي الدائم للبوت"),
    _int("invite_xp", "xp", "XP لصاحب الدعوة", 50, 0, 10000),
    _bool("level_up_enabled", "xp", "إرسال رسالة عند رفع المستوى", True),
    _text("level_up_channel", "xp", "روم رسائل المستوى (فارغ = نفس روم الرسالة)"),
    SettingDef("level_roles", Kind.STR, "xp", "أدوار مستويات الأعضاء؛ تُدار عبر /config level-role", "{}"),
    SettingDef("level_up_banner", Kind.STR, "xp", "رابط صورة إعلان المستوى", None),
    SettingDef("level_up_emoji", Kind.STR, "xp", "إيموجي إعلان المستوى المخصص", None),

    # ---- XP الطاقم ----
    _int("staff_xp_verify", "staff_xp", "XP الطاقم عند التحقق من عضو", 10, 0, 1000),
    _int("staff_xp_invite", "staff_xp", "XP الطاقم عند دعوة عضو جديد", 1, 0, 1000),
    _int("staff_xp_ticket_close", "staff_xp", "XP الطاقم عند إغلاق تذكرة", 20, 0, 1000),
    _int("staff_xp_application_review", "staff_xp", "XP الطاقم عند مراجعة طلب", 15, 0, 1000),
    _int("staff_xp_cooldown", "staff_xp", "ثواني الانتظار بين إجراءين يعطيان XP", 30, 0, 3600),
    _int("staff_xp_daily_cap", "staff_xp", "أقصى XP يومي للإداري (0 = بلا حد)", 500, 0, 100000),
    _int("staff_warn_xp_penalty", "staff_xp", "خصم Staff XP لكل Staff Warn", 500, 0, 1000000),
    SettingDef("staff_rank_config", Kind.STR, "staff_xp", "رتب Staff XP؛ تُدار عبر /config staff-rank", "{}"),
    _text("staff_promotion_channel", "staff_xp", "قناة إعلانات ترقيات Staff"),
    _text("high_staff_promotion_channel", "staff_xp", "قناة إعلانات ترقيات High Staff"),
    _text("staff_demotion_channel", "staff_xp", "قناة إعلانات خفض رتب Staff"),

    # ---- ألعاب الطاقم ----
    *[
        item
        for game in GAMES.values()
        for item in (
            _int(game.xp_key, "staff_games", f"Staff XP لمضيف لعبة {game.label}", 0, 0, 1000000),
            _text(game.panel_channel_key, "staff_games", f"قناة لوحة لعبة {game.label}"),
            _text(game.results_channel_key, "staff_games", f"قناة نتائج لعبة {game.label}"),
        )
    ],

    # ---- الرومات المؤقتة ----
    _category("temp_voice_category", "temp_voice", "كاتيغوري الرومات المؤقتة"),
    _voice("temp_voice_create_channel", "temp_voice", "روم Create Voice"),
    _str("temp_voice_default_name", "temp_voice", "اسم الروم الافتراضي ({username}, {display_name}, {member})", "🔊・{display_name}'s Chamber"),
    _int("temp_voice_default_limit", "temp_voice", "حد الأعضاء الافتراضي (0 = بلا حد)", 0, 0, 99),
    _int("temp_voice_delete_delay", "temp_voice", "ثواني قبل حذف الروم الفارغ", 5, 0, 600),
    _bool("temp_voice_panel_enabled", "temp_voice", "إرسال لوحة التحكم لمالك الروم", True),
    _bool("temp_voice_panel_staff_override", "temp_voice", "السماح للطاقم بالتحكم في أي روم", True),
    _text("temp_voice_staff_alert", "temp_voice", "روم تنبيه الطاقم عند طلب المساعدة"),
    _int("temp_voice_staff_call_cooldown", "temp_voice", "مهلة تكرار طلب الطاقم بالثواني", 60, 0, 3600),
    SettingDef("temp_voice_banner", Kind.STR, "temp_voice", "رابط بانر لوحة الروم المؤقت", None),

    # ---- التذاكر ----
    _category("ticket_category", "tickets", "كاتيغوري التذاكر"),
    _text("ticket_panel_channel", "tickets", "روم لوحة التذاكر"),
    SettingDef("ticket_category_staff_apply", Kind.CATEGORY, "tickets", "Category for staff applications", None),
    SettingDef("ticket_category_manager_apply", Kind.CATEGORY, "tickets", "Category for manager applications", None),
    SettingDef("ticket_category_assistance", Kind.CATEGORY, "tickets", "Category for assistance tickets", None),
    SettingDef("ticket_category_response", Kind.CATEGORY, "tickets", "Category for response tickets", None),
    _role("role_ticket_staff", "tickets", "رتبة طاقم التذاكر"),
    _role("role_ticket_manager", "tickets", "Ticket manager role"),
    _text("ticket_log_channel", "tickets", "Ticket transcript and activity log channel"),
    SettingDef("ticket_banner_url", Kind.STR, "tickets", "Ticket panel banner URL", None),
    SettingDef("ticket_panel_title", Kind.STR, "tickets", "Ticket panel title", "VALYRIA SUPPORT CENTER"),
    SettingDef("ticket_panel_description", Kind.STR, "tickets", "Ticket panel description", "Need assistance, want to join our staff team, apply for management, or contact the administration?\n\nChoose the category that best describes your request below. A private ticket will be created for you and the appropriate staff members will assist you."),
    SettingDef("ticket_button_labels", Kind.STR, "tickets", "Ticket panel button labels and emojis as JSON", '{"staff_apply":"🛡️ Staff Apply","manager_apply":"👑 Manager Apply","assistance":"🆘 Assistance","response":"💬 Réponse"}'),
    _int("ticket_max_open", "tickets", "أقصى عدد تذاكر مفتوحة للعضو", 1, 1, 10),
    _int("ticket_close_delay", "tickets", "ثواني قبل حذف التذكرة بعد الإغلاق", 5, 0, 60),
    _bool("ticket_transcript_enabled", "tickets", "حفظ Transcript عند الإغلاق", True),

    # ---- الطلبات والاستئنافات ----
    _bool("application_enabled", "applications", "تفعيل التقديم على الطاقم", True),
    _text("application_channel", "applications", "روم مراجعة طلبات الطاقم"),
    _role("role_application_reviewer", "applications", "رتبة من يراجع الطلبات"),
    _int("application_cooldown_hours", "applications", "ساعات الانتظار قبل تقديم طلب جديد", 72, 0, 8760),
    _text("appeals_channel", "applications", "روم استئنافات الباند"),

    # ---- الإشراف ----
    _choice("warn_action_1", "moderation", "الإجراء عند التحذير الأول", "none", _WARN_ACTIONS),
    _choice("warn_action_2", "moderation", "الإجراء عند التحذير الثاني", "timeout", _WARN_ACTIONS),
    _choice("warn_action_3", "moderation", "الإجراء عند التحذير الثالث وما فوق", "jail", _WARN_ACTIONS),
    _int("warn_timeout_minutes", "moderation", "مدة Timeout التلقائي (دقائق)", 60, 1, 40320),
    _int("warn_expire_days", "moderation", "أيام صلاحية التحذير (0 = لا ينتهي)", 0, 0, 3650),
    _bool("mod_dm_user", "moderation", "إرسال رسالة خاصة للعضو عند العقوبة", True),
    _int("mod_clear_max", "moderation", "أقصى عدد رسائل في أمر /clear", 100, 1, 1000),
    SettingDef("empress_role", Kind.ROLE, "phase14", "Configured Empress authorization role", None),
    SettingDef("empress_gif", Kind.STR, "phase14", "GIF URL shown in Empress confirmations", None),
    _text("rules_channel", "phase14", "Rules message channel"),
    SettingDef("rules_message", Kind.STR, "phase14", "Custom rules message content", None),
    SettingDef("rules_emojis", Kind.STR, "phase14", "Comma-separated rules reactions", None),
    _text("reaction_role_logs", "phase14", "Reaction-role result channel"),
    _role("game_cleanup_role", "phase14", "Game role managed by cleanup panel"),
    _text("suggestion_panel_channel", "community", "Suggestion submission panel channel"),
    _text("suggestion_review_channel", "community", "Private staff suggestion review channel"),
    _text("suggestion_approved_channel", "community", "Public approved suggestions channel"),
    SettingDef("suggestion_banner", Kind.STR, "community", "Suggestion panel banner URL", None),
    SettingDef("suggestion_panel_title", Kind.STR, "community", "Suggestion panel title", "💡 VALYRIA SUGGESTIONS"),
    SettingDef("suggestion_panel_text", Kind.STR, "community", "Suggestion panel description", "Have an idea that could make Valyria better?\n\nUse the button below to submit your suggestion.\n\nYour idea will first be reviewed by the Staff Team. Approved suggestions will be published in the official Suggestions channel."),
    SettingDef("suggestion_button_label", Kind.STR, "community", "Suggestion submit button label", "Submit Suggestion"),
    SettingDef("suggestion_button_emoji", Kind.STR, "community", "Suggestion submit button emoji", "💡"),
    SettingDef("suggestion_reject_reason_required", Kind.BOOL, "community", "Require a reason when rejecting suggestions", True),
    _text("poll_default_channel", "community", "Default community poll channel"),
    _int("poll_default_duration_hours", "community", "Default community poll duration in hours", 24, 1, 720),
    _bool("poll_default_multiple_choices", "community", "Allow multiple poll choices by default", False),
    _text("confession_channel", "community", "Anonymous confession channel"),

    # ---- AutoMod ----
    _bool("automod_enabled", "automod", "تفعيل AutoMod", False),
    SettingDef("automod_words", Kind.STR, "automod", "Forbidden words/phrases managed with /automod", "[]"),
    _int("automod_warn_xp_penalty", "automod", "Member XP removed per AutoMod warning", 100, 0, 100000),
    _text("automod_log_channel", "automod", "AutoMod log channel"),
    _choice("automod_action", "automod", "الإجراء عند المخالفة", "warn", ("delete", "warn", "timeout", "jail")),
    _int("automod_spam_messages", "automod", "عدد الرسائل التي تعتبر Spam", 5, 2, 30),
    _int("automod_spam_seconds", "automod", "خلال كم ثانية", 5, 1, 60),
    _int("automod_mention_limit", "automod", "أقصى عدد منشنات في رسالة", 5, 2, 50),
    _bool("antispam_enabled", "automod", "Enable Anti-Spam checks", False),
    _int("antispam_xp_penalty", "automod", "Member XP removed per Anti-Spam warning", 100, 0, 100000),
    _int("antispam_duplicate_limit", "automod", "Repeated identical messages before warning", 3, 2, 10),
    _int("antispam_duplicate_seconds", "automod", "Duplicate message detection window in seconds", 15, 3, 120),
    _int("antispam_emoji_limit", "automod", "Maximum emoji characters in one message", 12, 4, 80),
    _int("antispam_repeat_chars", "automod", "Repeated characters before warning", 20, 10, 100),
    _bool("antispam_links_enabled", "automod", "Detect link spam", False),
    _int("antispam_link_limit", "automod", "Links in one message before warning", 5, 2, 30),
    _int("automod_caps_percent", "automod", "نسبة الحروف الكبيرة المسموحة %", 70, 30, 100),
    _int("automod_caps_min_length", "automod", "أقل طول رسالة لفحص الحروف الكبيرة", 12, 5, 200),
    _bool("automod_block_invites", "automod", "منع روابط دعوات Discord", True),
    _bool("automod_block_badwords", "automod", "منع الكلمات المحظورة", True),
    _bool("automod_ignore_staff", "automod", "تجاهل الطاقم", True),

    # ---- Clean Messages ----
    _bool("clean_enabled", "clean", "Enable the same-channel Clean Messages command", False),
    _int("clean_max_messages", "clean", "Maximum messages removable by /clean", 100, 1, 100),
    SettingDef("clean_role_ids", Kind.STR, "clean", "Authorized /clean role IDs stored as JSON", "[]"),

    # ---- الأمان ----
    _bool("antiraid_enabled", "security", "تفعيل Anti-Raid", False),
    _int("antiraid_join_threshold", "security", "عدد الدخول المشبوه", 10, 3, 100),
    _int("antiraid_join_seconds", "security", "خلال كم ثانية", 30, 3, 300),
    _choice("antiraid_action", "security", "الإجراء عند اكتشاف Raid", "alert", ("alert", "kick")),
    _choice("antiraid_mode", "security", "Raid response mode", "normal", ("normal", "strict")),
    _role("raid_restricted_role", "security", "Role applied to new joins during Strict Raid Mode"),
    _bool("antiraid_include_bots", "security", "Count bot joins in raid detection", False),

    # ---- Jail ----
    _role("role_jail", "jail", "رتبة Jail"),
    _category("jail_category", "jail", "كاتيغوري Jail"),
    _text("jail_chat", "jail", "روم Jail النصي"),
    _voice("jail_voice", "jail", "روم Jail الصوتي"),
    _int("jail_default_minutes", "jail", "مدة Jail الافتراضية (دقائق)", 30, 1, 43200),
    _int("jail_max_minutes", "jail", "أقصى مدة Jail (دقائق)", 10080, 1, 43200),
    _bool("jail_allow_chat", "jail", "السماح للعضو بالكتابة في روم Jail", True),
    _bool("jail_hide_tickets", "jail", "إخفاء التذاكر عن المسجون", True),
    _int("jail_max_per_staff_hour", "jail", "أقصى Jail لإداري واحد في الساعة (0 = بلا حد)", 10, 0, 100),
    _text("jail_log_channel", "jail", "Jail action log channel"),
]


def _build_registry(definitions: list[SettingDef]) -> dict[str, SettingDef]:
    """يبني القاموس ويتأكد من عدم وجود أخطاء إملائية أو تكرار (يفشل مبكرًا عند التشغيل)."""
    registry: dict[str, SettingDef] = {}
    for definition in definitions:
        if definition.key in registry:
            raise RuntimeError(f"مفتاح إعداد مكرر: {definition.key}")
        if len(definition.key) > 64:
            raise RuntimeError(f"مفتاح طويل جدًا (الحد 64): {definition.key}")
        if definition.group not in GROUPS:
            raise RuntimeError(f"مجموعة غير معروفة: {definition.group}")
        if definition.kind is Kind.CHOICE and definition.default not in definition.choices:
            raise RuntimeError(f"القيمة الافتراضية ليست ضمن الخيارات: {definition.key}")
        registry[definition.key] = definition
    return registry


SETTINGS: dict[str, SettingDef] = _build_registry(SETTING_LIST)


def get_definition(key: str) -> SettingDef:
    try:
        return SETTINGS[key]
    except KeyError:
        raise UnknownSettingError(f"الإعداد «{key}» غير موجود.") from None
