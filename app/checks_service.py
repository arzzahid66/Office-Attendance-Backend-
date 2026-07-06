import random
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import RandomCheck, User
from app.timeutil import DISPLAY_TZ

settings = get_settings()


def _parse_hhmm(value: str) -> tuple[int, int]:
    hh, mm = value.split(":")
    return int(hh), int(mm)


async def generate_checks_for_day(db: AsyncSession, day: date) -> int:
    """Creates N random check times per active employee within the configured daily
    window. Idempotent: skips employees who already have checks for that date."""
    start_h, start_m = _parse_hhmm(settings.random_check_window_start)
    end_h, end_m = _parse_hhmm(settings.random_check_window_end)

    window_start = datetime(day.year, day.month, day.day, start_h, start_m, tzinfo=DISPLAY_TZ)
    window_end = datetime(day.year, day.month, day.day, end_h, end_m, tzinfo=DISPLAY_TZ)
    window_seconds = int((window_end - window_start).total_seconds())

    employees_result = await db.execute(select(User).where(User.role == "employee", User.status == "active"))
    employees = employees_result.scalars().all()

    created = 0
    for emp in employees:
        existing = await db.execute(
            select(RandomCheck.id).where(RandomCheck.user_id == emp.id, RandomCheck.date == day).limit(1)
        )
        if existing.first() is not None:
            continue

        offsets = sorted(random.sample(range(window_seconds), k=min(settings.random_checks_per_day, window_seconds)))
        for offset in offsets:
            scheduled_at = window_start + timedelta(seconds=offset)
            db.add(RandomCheck(user_id=emp.id, date=day, scheduled_at=scheduled_at.astimezone(DISPLAY_TZ), result="pending"))
            created += 1

    await db.commit()
    return created
