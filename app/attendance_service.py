from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AttendanceDay, OfficeNetwork, WfhRequest


async def ip_matches_office(db: AsyncSession, ip: str) -> bool:
    result = await db.execute(select(OfficeNetwork).where(OfficeNetwork.active.is_(True)))
    networks = result.scalars().all()
    return any(n.public_ip == ip for n in networks)


async def has_approved_wfh(db: AsyncSession, user_id: int, day: date) -> bool:
    result = await db.execute(
        select(WfhRequest).where(
            WfhRequest.user_id == user_id,
            WfhRequest.date == day,
            WfhRequest.status == "approved",
        )
    )
    return result.scalar_one_or_none() is not None


async def get_attendance_day(db: AsyncSession, user_id: int, day: date) -> AttendanceDay | None:
    result = await db.execute(select(AttendanceDay).where(AttendanceDay.user_id == user_id, AttendanceDay.date == day))
    return result.scalar_one_or_none()


async def get_or_create_attendance_day(db: AsyncSession, user_id: int, day: date) -> AttendanceDay:
    existing = await get_attendance_day(db, user_id, day)
    if existing is not None:
        return existing
    row = AttendanceDay(user_id=user_id, date=day, mode="pending")
    db.add(row)
    await db.flush()
    return row
