"""
محرك الاتصال بقاعدة البيانات + دالة إنشاء جلسة (session) لكل مهمة + تشغيل الـ Migrations.
"""
import asyncio

from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from bot.config.settings import PROJECT_ROOT, settings

# أسماء ثابتة للقيود (PK/FK/...). بدونها لا تستطيع Alembic تعديل القيود لاحقًا في SQLite.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """كل الـ Models (Guild, User, ...) ترث من هذا الكلاس."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# aiosqlite هو الـ driver غير المتزامن (async) لـ SQLite
engine = create_async_engine(settings.database_url, echo=False)

# SQLite يتجاهل الـ Foreign Keys افتراضيًا! هذا السطر يفعّلها مع كل اتصال جديد.
if engine.sync_engine.dialect.name == "sqlite":

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


# مصنع الجلسات: كل مهمة تنشئ جلسة جديدة من هنا
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _upgrade_to_head() -> None:
    """يشغّل كل الـ Migrations الناقصة (نفس أمر: alembic upgrade head)."""
    alembic_config = Config(str(PROJECT_ROOT / "alembic.ini"))
    # نمنع Alembic من إعادة ضبط الـ logging الخاص بالبوت
    alembic_config.attributes["configure_logger"] = False
    command.upgrade(alembic_config, "head")


async def init_db() -> None:
    """يجهّز قاعدة البيانات عند تشغيل البوت عبر الـ Migrations (بدل create_all)."""
    # Alembic كود متزامن، فنشغّله في thread حتى لا يوقف البوت
    await asyncio.to_thread(_upgrade_to_head)
