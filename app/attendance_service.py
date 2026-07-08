from datetime import date, time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import AttendanceDay, OfficeNetwork, ScheduleRequest, WfhRequest

settings = get_settings()


def _parse_hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(int(hh), int(mm))


async def get_effective_check_window(db: AsyncSession, user_id: int, day: date) -> tuple[time, time]:
    """The working window used to place a user's random checks for a given day.

    If the user has an approved ScheduleRequest covering the day, use its window;
    otherwise fall back to the global default from settings. When multiple approved
    requests overlap the day, the most recently created one wins."""
    result = await db.execute(
        select(ScheduleRequest)
        .where(
            ScheduleRequest.user_id == user_id,
            ScheduleRequest.status == "approved",
            ScheduleRequest.start_date <= day,
            ScheduleRequest.end_date >= day,
        )
        .order_by(ScheduleRequest.id.desc())
        .limit(1)
    )
    approved = result.scalar_one_or_none()
    if approved is not None:
        return approved.start_time, approved.end_time
    return _parse_hhmm(settings.random_check_window_start), _parse_hhmm(settings.random_check_window_end)


async def get_matching_office_network(db: AsyncSession, ip: str) -> OfficeNetwork | None:
    """The active office network whose public IP equals the given IP, if any."""
    result = await db.execute(select(OfficeNetwork).where(OfficeNetwork.active.is_(True)))
    for n in result.scalars().all():
        if n.public_ip == ip:
            return n
    return None


async def ip_matches_office(db: AsyncSession, ip: str) -> bool:
    return await get_matching_office_network(db, ip) is not None


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
