"""
محرك الاتصال بقاعدة البيانات + دالة لإنشاء جلسة (session) في كل مرة نحتاجها.
"""
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """كل الـ Models (Guild, User, ...) سترث من هذا الكلاس."""
    pass


# aiosqlite هو الـ driver غير المتزامن (async) لـ SQLite
engine = create_async_engine("sqlite+aiosqlite:///bot_database.db", echo=False)

# مصنع الجلسات: كل مرة نحتاج نتكلم مع القاعدة، نطلب جلسة جديدة من هنا
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db():
    """ينشئ كل الجداول المعرّفة في الـ Models، إذا لم تكن موجودة مسبقًا."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)