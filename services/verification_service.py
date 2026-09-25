from datetime import timedelta
import asyncio
import discord
from sqlalchemy import select
from bot.database.engine import async_session
from bot.database.models.verification import VerificationRecord
from bot.services.config_service import get_or_create_guild, get_setting
ACTIVE=("waiting","in_progress")
_request_locks: dict[tuple[int,int],asyncio.Lock]={}
async def ensure_unverified(member:discord.Member)->bool:
    role_id=await get_setting(member.guild.id,"role_unverified")
    role=member.guild.get_role(role_id) if role_id else None
    if role is None or role in member.roles:return False
    me=member.guild.me
    if not me or not me.guild_permissions.manage_roles or me.top_role<=role:
        raise RuntimeError("Bot cannot manage configured Unverified role; check Manage Roles and hierarchy.")
    await member.add_roles(role,reason="New member verification");return True
async def list_active(guild_id:int,member_id:int|None=None)->list[VerificationRecord]:
    async with async_session() as s:
        q=select(VerificationRecord).where(VerificationRecord.guild_id==guild_id,VerificationRecord.status.in_(ACTIVE))
        if member_id is not None:q=q.where(VerificationRecord.member_id==member_id)
        return list((await s.scalars(q.order_by(VerificationRecord.created_at))).all())
async def request_dm_allowed(guild_id:int,member_id:int,cooldown:int)->bool:
    async with async_session() as s:
        rows=list((await s.scalars(select(VerificationRecord).where(VerificationRecord.guild_id==guild_id,
            VerificationRecord.member_id==member_id,VerificationRecord.status.in_(ACTIVE)).order_by(VerificationRecord.id.desc()))).all())
        if any(row.status=="in_progress" for row in rows):return False
        row=next((item for item in rows if item.status=="waiting"),None)
        if row and row.created_at:
            created=row.created_at
            if created.tzinfo is None:created=created.replace(tzinfo=discord.utils.utcnow().tzinfo)
            if discord.utils.utcnow()-created<timedelta(seconds=cooldown):return False
    return True
async def create_waiting_if_due(guild_id:int,member_id:int,cooldown:int)->int|None:
    lock=_request_locks.setdefault((guild_id,member_id),asyncio.Lock())
    async with lock:
        if not await request_dm_allowed(guild_id,member_id,cooldown):return None
        return await create_waiting(guild_id,member_id)

async def create_waiting(guild_id:int,member_id:int)->int:
    await get_or_create_guild(guild_id)
    async with async_session() as s:
        row=await s.scalar(select(VerificationRecord).where(VerificationRecord.guild_id==guild_id,
            VerificationRecord.member_id==member_id,VerificationRecord.status=="waiting").order_by(VerificationRecord.id.desc()))
        if row is None:
            row=VerificationRecord(guild_id=guild_id,member_id=member_id,status="waiting");s.add(row)
        else:
            row.created_at=discord.utils.utcnow()
        await s.commit();return row.id
async def start(guild_id:int,member_id:int,staff_id:int)->VerificationRecord:
    async with async_session() as s:
        row=await s.scalar(select(VerificationRecord).where(VerificationRecord.guild_id==guild_id,
            VerificationRecord.member_id==member_id,VerificationRecord.status=="waiting").order_by(VerificationRecord.id.desc()))
        if row is None:row=VerificationRecord(guild_id=guild_id,member_id=member_id,status="in_progress");s.add(row)
        else:row.status="in_progress"
        row.staff_id=staff_id;await s.commit();await s.refresh(row);return row
async def get_record(record_id:int)->VerificationRecord|None:
    async with async_session() as s:return await s.get(VerificationRecord,record_id)
async def set_answer(record_id:int,*,gender:str|None=None,other_staff:bool|None=None,server_name:str|None=None)->None:
    async with async_session() as s:
        row=await s.get(VerificationRecord,record_id)
        if row and row.status=="in_progress":
            if gender is not None:row.gender=gender
            if other_staff is not None:row.is_other_server_staff=other_staff;row.other_server_name=server_name if other_staff else None
            await s.commit()
async def finish(record_id:int,status:str,reason:str|None=None,*,staff_id:int|None=None)->VerificationRecord|None:
    async with async_session() as s:
        row=await s.get(VerificationRecord,record_id)
        if row is None or row.status!="in_progress":return None
        row.status=status;row.reason=reason;row.completed_at=discord.utils.utcnow()
        if staff_id is not None:row.staff_id=staff_id
        await s.commit();await s.refresh(row);return row
