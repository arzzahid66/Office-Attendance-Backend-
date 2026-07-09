from contextvars import ContextVar
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app.config import get_settings

settings = get_settings()
DISPLAY_TZ = ZoneInfo(settings.display_timezone)

# DEV_MODE clock override. In production this is always None (the middleware that populates
# it is only mounted when DEV_MODE=true — see Step 5). It is a ContextVar, not a module
# global, so a request/job populates it once and every now_utc() within that unit of work
# (across the separate scheduler process too, via app_state) sees the same value.
_time_override: ContextVar[datetime | None] = ContextVar("time_override", default=None)


def set_time_override(dt: datetime | None) -> None:
    """dt MUST be timezone-aware UTC (or None to clear). Set per request/job, never globally."""
    _time_override.set(dt)


def get_time_override() -> datetime | None:
    return _time_override.get()


def now_utc() -> datetime:
    override = _time_override.get()
    return override if override is not None else datetime.now(timezone.utc)


def now_local() -> datetime:
    return now_utc().astimezone(DISPLAY_TZ)


def today_local() -> date:
    return now_local().date()


def to_local(dt: datetime) -> datetime:
    return dt.astimezone(DISPLAY_TZ)


# --------------------------------------------------------------------------- office-local
def office_tz(office) -> ZoneInfo:
    return ZoneInfo(office.timezone)


def office_now(office) -> datetime:
    return now_utc().astimezone(office_tz(office))


def office_today(office) -> date:
    """THE day key for attendance_days — the office-local date, never the UTC date."""
    return office_now(office).date()


def shift_bounds(office, roster, local_date: date) -> tuple[datetime, datetime]:
    """Aware start/end datetimes for `local_date` in the office's timezone. Compared
    against now_utc() (also aware), so there is never a naive/UTC mix."""
    tz = office_tz(office)
    start = datetime.combine(local_date, roster.start_time, tzinfo=tz)
    end = datetime.combine(local_date, roster.end_time, tzinfo=tz)
    return start, end
