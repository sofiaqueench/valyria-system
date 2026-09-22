"""
استيراد كل الـ Models هنا يضمن أن SQLAlchemy "يراها" جميعًا عند استدعاء init_db(),
حتى لو لم تُستخدم مباشرة في هذا الملف.
"""
from bot.database.models.guild import Guild
from bot.database.models.user import User

__all__ = ["Guild", "User"]