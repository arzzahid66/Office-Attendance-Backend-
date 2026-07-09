"""Scheduled attendance resolution. Both functions are COMMIT-FREE: the scheduler wraps
them in one transaction that also holds a pg_try_advisory_xact_lock, so the caller owns the
single commit (which releases the lock). Never commit here.

Every write is a compare-and-set or a guarded upsert, so these remain correct even if two
schedulers were somehow started simultaneously (the lock is layer 2, this is layer 3):

  * closing a day : UPDATE ... WHERE id = :id AND check_out IS NULL   (rowcount tells us
                    whether WE closed it; a racing run sees 0 rows and does not double-count)
  * flagging      : INSERT ... ON CONFLICT (user_id, date) DO UPDATE ... WHERE check_in IS NULL
  * off_day       : INSERT ... ON CONFLICT DO NOTHING
"""

from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models import AttendanceDay, Office, Roster, User
from app.presence import ensure_off_day
from app.timeutil import office_tz, shift_bounds

settings = get_settings()


async def _close_day(db: AsyncSession, day_id: int, check_out: datetime) -> bool:
    """Compare-and-set. Returns True only if THIS call closed the day.

    Under a concurrent run the second UPDATE blocks on the row lock, then re-evaluates
    `check_out IS NULL` against the committed value (READ COMMITTED), matches 0 rows, and
    reports False. No lost update, no double count, no exception."""
    result = await db.execute(
        update(AttendanceDay)
        .where(AttendanceDay.id == day_id, AttendanceDay.check_out.is_(None))
        .values(check_out=check_out)
    )
    return result.rowcount > 0


async def run_idle_checkout(db: AsyncSession, now: datetime) -> int:
    """Close out anyone whose last_seen is older than CHECKOUT_IDLE_MINUTES.
    check_out = min(last_seen, shift_end) — a forgotten open tab never exceeds end_time."""
    cutoff = now - timedelta(minutes=settings.checkout_idle_minutes)
    rows = (
        await db.execute(
            select(AttendanceDay, Roster, Office)
            .join(User, User.id == AttendanceDay.user_id)
            .join(Roster, Roster.id == User.assigned_roster_id)
            .join(Office, Office.id == Roster.office_id)
            .where(
                AttendanceDay.check_in.isnot(None),
                AttendanceDay.check_out.is_(None),
                AttendanceDay.last_seen.isnot(None),
                AttendanceDay.last_seen < cutoff,
            )
        )
    ).all()

    closed = 0
    for day, roster, office in rows:
        _, shift_end = shift_bounds(office, roster, day.date)
        if await _close_day(db, day.id, min(day.last_seen, shift_end)):
            closed += 1
    return closed


async def run_nightly_resolution(db: AsyncSession, now: datetime) -> dict[str, int]:
    """For each active employee, resolve TODAY (their office-local date):
      not a working day        -> off_day
      has check_in             -> ensure check_out = min(last_seen or check_in, shift_end)
      working day, no check_in -> flagged   (NEVER absent — only an admin sets absent/leave)
    """
    employees = (
        await db.execute(select(User).where(User.role == "employee", User.status == "active"))
    ).scalars().all()

    stats = {"off_day": 0, "closed": 0, "flagged": 0, "already": 0}
    for emp in employees:
        roster = emp.assigned_roster
        if roster is None:
            continue
        office = roster.office
        local_date = now.astimezone(office_tz(office)).date()
        _, shift_end = shift_bounds(office, roster, local_date)
        is_working = local_date.weekday() in roster.working_days

        if not is_working:
            await ensure_off_day(db, emp.id, local_date)
            stats["off_day"] += 1
            continue

        day = (
            await db.execute(
                select(AttendanceDay).where(AttendanceDay.user_id == emp.id, AttendanceDay.date == local_date)
            )
        ).scalar_one_or_none()

        if day is not None and day.check_in is not None:
            if day.check_out is None:
                if await _close_day(db, day.id, min(day.last_seen or day.check_in, shift_end)):
                    stats["closed"] += 1
                else:
                    stats["already"] += 1  # a racing run closed it first
            else:
                stats["already"] += 1
            continue

        # Working day, no check-in -> flagged. The WHERE clause means a racing run can never
        # clobber a row that has since acquired a check_in.
        stmt = (
            pg_insert(AttendanceDay)
            .values(user_id=emp.id, date=local_date, status="flagged", late_minutes=0)
            .on_conflict_do_update(
                index_elements=["user_id", "date"],
                set_={"status": "flagged"},
                where=AttendanceDay.check_in.is_(None),
            )
        )
        await db.execute(stmt)
        stats["flagged"] += 1

    return stats
