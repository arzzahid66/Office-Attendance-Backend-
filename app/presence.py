"""Presence detection seam.

Every way of proving a user is present (WiFi IP today, GPS today, a MikroTik/UniFi/RADIUS
agent tomorrow) produces the same normalized PresenceEvent. apply_presence_event() is the
ONLY function that writes a check-in into attendance_days, via an atomic upsert that is
safe against the double-tap race. Attendance rules therefore never need to change when a
new detection method is added — a future POST /api/agent/presence just builds a
PresenceEvent and calls the same writer.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal, Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.geo import haversine_meters
from app.models import AttendanceDay, Office, Roster
from app.timeutil import office_tz, shift_bounds

Method = Literal["wifi", "gps", "manual", "agent"]


@dataclass(frozen=True)
class PresenceEvent:
    user_id: int
    timestamp: datetime  # UTC, aware
    method: Method
    source_ip: str | None = None
    lat: float | None = None
    lng: float | None = None
    office_id: int | None = None
    confidence: float = 1.0  # wifi=1.0 -> 'verified'; gps=0.6 -> 'gps_pending'


class PresenceSource(Protocol):
    async def resolve(self, db: AsyncSession, user, ctx) -> PresenceEvent | None: ...


@dataclass(frozen=True)
class ShiftEval:
    local_date: date
    weekday: int
    is_working_day: bool
    shift_start: datetime  # aware, office tz
    shift_end: datetime  # aware, office tz
    grace_end: datetime  # shift_start + grace_minutes
    phase: Literal["off_day", "before_shift", "in_shift", "after_shift"]
    minutes_to_start: int


def evaluate_shift(office: Office, roster: Roster, now: datetime) -> ShiftEval:
    """Where `now` falls relative to the roster's shift for the OFFICE-LOCAL date.
    Client timing is never trusted — this is always computed server-side from now_utc()."""
    local_date = now.astimezone(office_tz(office)).date()
    weekday = local_date.weekday()  # 0=Mon .. 6=Sun, matches roster.working_days encoding
    shift_start, shift_end = shift_bounds(office, roster, local_date)
    grace_end = shift_start + timedelta(minutes=roster.grace_minutes)

    is_working_day = weekday in roster.working_days
    if not is_working_day:
        phase = "off_day"
    elif now < shift_start:
        phase = "before_shift"
    elif now > shift_end:
        phase = "after_shift"
    else:
        phase = "in_shift"

    minutes_to_start = max(0, int((shift_start - now).total_seconds() // 60))
    return ShiftEval(local_date, weekday, is_working_day, shift_start, shift_end, grace_end, phase, minutes_to_start)


async def find_office_by_ip(db: AsyncSession, ip: str) -> Office | None:
    """The active office whose public_ips contains this IP (any branch, not just the
    user's own — a user may be physically at another office)."""
    offices = (await db.execute(select(Office).where(Office.active.is_(True)))).scalars().all()
    for office in offices:
        if ip in (office.public_ips or []):
            return office
    return None


async def nearest_office(db: AsyncSession, lat: float, lng: float) -> tuple[Office, float] | None:
    """Nearest active office (that has coordinates) and its Haversine distance in metres,
    or None if no active office has coordinates configured yet."""
    offices = (
        await db.execute(
            select(Office).where(
                Office.active.is_(True), Office.latitude.isnot(None), Office.longitude.isnot(None)
            )
        )
    ).scalars().all()
    best: tuple[Office, float] | None = None
    for office in offices:
        dist = haversine_meters(lat, lng, office.latitude, office.longitude)
        if best is None or dist < best[1]:
            best = (office, dist)
    return best


def _classify(ev: ShiftEval, roster: Roster, now: datetime) -> tuple[str, int]:
    """(status, late_minutes). Late is triggered past start+grace; late_minutes is measured
    from the scheduled start (so 10:00 start, arrive 10:20 -> late by 20, not 5)."""
    if now > ev.grace_end:
        return "late", max(0, int((now - ev.shift_start).total_seconds() // 60))
    return "present", 0


async def ensure_off_day(db: AsyncSession, user_id: int, local_date: date) -> None:
    stmt = (
        pg_insert(AttendanceDay)
        .values(user_id=user_id, date=local_date, status="off_day", late_minutes=0)
        .on_conflict_do_nothing(index_elements=["user_id", "date"])
    )
    await db.execute(stmt)


async def apply_presence_event(
    db: AsyncSession, event: PresenceEvent, ev: ShiftEval, roster: Roster
) -> tuple[AttendanceDay, bool]:
    """The ONLY writer of a check-in. Atomic upsert guarded by `check_in IS NULL`:

      * no row            -> INSERT           -> (day, True)   checked in
      * row, check_in NULL-> UPDATE           -> (day, True)   checked in
      * row already in    -> guard false, 0 rows returned -> (existing_day, False) already checked in

    The guard makes a concurrent double-tap safe: the second request blocks on the row
    lock the upsert takes, re-reads the committed check_in, sees it non-null, and returns
    zero rows -> already_checked_in. No duplicate rows (UNIQUE), no double check-in, no
    IntegrityError (ON CONFLICT absorbs it). Correct under READ COMMITTED.
    """
    now = event.timestamp
    status, late_minutes = _classify(ev, roster, now)
    verification = "verified" if event.method == "wifi" else "gps_pending"

    write = dict(
        status=status,
        method=event.method,
        verification=verification,
        check_in=now,
        last_seen=now,
        late_minutes=late_minutes,
    )
    stmt = (
        pg_insert(AttendanceDay)
        .values(user_id=event.user_id, date=ev.local_date, **write)
        .on_conflict_do_update(
            index_elements=["user_id", "date"],
            set_=write,
            where=AttendanceDay.check_in.is_(None),
        )
        .returning(AttendanceDay.id)
    )
    new_id = (await db.execute(stmt)).scalar_one_or_none()

    if new_id is None:
        existing = (
            await db.execute(
                select(AttendanceDay).where(
                    AttendanceDay.user_id == event.user_id, AttendanceDay.date == ev.local_date
                )
            )
        ).scalar_one()
        return existing, False

    return await db.get(AttendanceDay, new_id), True
