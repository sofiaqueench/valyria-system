from sqlalchemy.exc import IntegrityError
from bot.database.engine import async_session
from bot.database.models.verification import StaffXPHistory
async def add_verification_xp(*,guild_id:int,staff_id:int,target_id:int,verification_record_id:int,amount:int)->bool:
    """Persist a single staff XP award per verification record."""
    if amount<=0:return False
    async with async_session() as session:
        session.add(StaffXPHistory(guild_id=guild_id,staff_id=staff_id,target_id=target_id,
            verification_record_id=verification_record_id,action="verification",xp_amount=amount))
        try:await session.commit()
        except IntegrityError:
            await session.rollback();return False
    return True
