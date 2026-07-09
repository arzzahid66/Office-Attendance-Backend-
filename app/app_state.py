"""Cross-process key/value state (Postgres `app_state`). Currently holds only the DEV_MODE
time override, so /debug/set-time in the API process is visible to the separate scheduler
process (an in-memory global would not be)."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AppState

_TIME_KEY = "time_override"


async def get_time_override(db: AsyncSession) -> datetime | None:
    row = (await db.execute(select(AppState.value).where(AppState.key == _TIME_KEY))).scalar_one_or_none()
    if not row:
        return None
    iso = row.get("iso")
    return datetime.fromisoformat(iso) if iso else None


async def set_time_override_db(db: AsyncSession, dt: datetime | None) -> None:
    # Atomic upsert — safe under concurrent /debug/set-time (a read-then-write would
    # StaleDataError if two callers raced on the single row).
    value = {"iso": dt.isoformat()} if dt is not None else None
    stmt = (
        pg_insert(AppState)
        .values(key=_TIME_KEY, value=value)
        .on_conflict_do_update(index_elements=["key"], set_={"value": value})
    )
    await db.execute(stmt)
