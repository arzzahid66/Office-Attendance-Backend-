from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.config import get_settings

settings = get_settings()
DISPLAY_TZ = ZoneInfo(settings.display_timezone)


def now_utc() -> datetime:
    from datetime import timezone

    return datetime.now(timezone.utc)


def now_local() -> datetime:
    return now_utc().astimezone(DISPLAY_TZ)


def today_local() -> date:
    return now_local().date()


def to_local(dt: datetime) -> datetime:
    return dt.astimezone(DISPLAY_TZ)
