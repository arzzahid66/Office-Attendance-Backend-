import random
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.attendance_service import get_effective_check_window
from app.config import get_settings
from app.models import RandomCheck, User
from app.timeutil import DISPLAY_TZ

settings = get_settings()


async def generate_checks_for_day(db: AsyncSession, day: date) -> int:
    """Creates N random check times per active employee within that employee's effective
    working window (their approved schedule if any, else the global default). Idempotent:
    skips employees who already have checks for that date."""
    employees_result = await db.execute(select(User).where(User.role == "employee", User.status == "active"))
    employees = employees_result.scalars().all()

    created = 0
    for emp in employees:
        existing = await db.execute(
            select(RandomCheck.id).where(RandomCheck.user_id == emp.id, RandomCheck.date == day).limit(1)
        )
        if existing.first() is not None:
            continue

        start_t, end_t = await get_effective_check_window(db, emp.id, day)
        window_start = datetime(day.year, day.month, day.day, start_t.hour, start_t.minute, tzinfo=DISPLAY_TZ)
        window_end = datetime(day.year, day.month, day.day, end_t.hour, end_t.minute, tzinfo=DISPLAY_TZ)
        window_seconds = int((window_end - window_start).total_seconds())
        if window_seconds <= 0:
            continue

        offsets = sorted(random.sample(range(window_seconds), k=min(settings.random_checks_per_day, window_seconds)))
        for offset in offsets:
            scheduled_at = window_start + timedelta(seconds=offset)
            db.add(RandomCheck(user_id=emp.id, date=day, scheduled_at=scheduled_at.astimezone(DISPLAY_TZ), result="pending"))
            created += 1

    await db.commit()
    return created
