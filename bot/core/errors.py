"""
أخطاء البوت المخصّصة.
الفكرة: بدل ValueError/KeyError العامة، نستخدم أخطاء بأسماء واضحة
حتى نعرف بالضبط ما الذي حصل ونعرض رسالة مناسبة للمستخدم.
"""


class BotError(Exception):
    """الأب لكل أخطاء البوت المخصّصة."""


class ConfigError(BotError):
    """خطأ في الإعدادات (قيمة غير صالحة، تعارض بين قيمتين...)."""


class UnknownSettingError(ConfigError):
    """طلب إعداد غير موجود في السجل (schema.py)."""
