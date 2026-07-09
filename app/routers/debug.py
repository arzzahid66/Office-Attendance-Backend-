"""DEV_MODE-only helpers (mounted only when DEV_MODE=true). No user auth — dev machines only.

/set-time writes the override to app_state so BOTH the API process and the separate
scheduler process see it. /run-nightly and /run-idle-checkout invoke the job BODIES in this
process — they exercise the logic and honour the override, but do NOT test APScheduler's
cron trigger or the advisory lock (that needs two real scheduler processes; see README).
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.app_state import get_time_override, set_time_override_db
from app.database import get_db
from app.deps import require_dev_mode
from app.ip_utils import get_client_ip
from app.nightly_service import run_idle_checkout, run_nightly_resolution
from app.schemas import SetTimeIn
from app.timeutil import now_utc, set_time_override

router = APIRouter(dependencies=[Depends(require_dev_mode)])


@router.get("/my-ip")
async def my_ip(request: Request):
    return {"detected_ip": get_client_ip(request)}


@router.post("/set-time")
async def set_time(payload: SetTimeIn, db: AsyncSession = Depends(get_db)):
    dt: datetime | None = None
    if payload.iso:
        try:
            dt = datetime.fromisoformat(payload.iso)
        except ValueError:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "iso must be an ISO-8601 datetime")
        if dt.tzinfo is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "iso must be timezone-aware (include an offset)")
    await set_time_override_db(db, dt)
    await db.commit()
    # Reflect it immediately for this response (the middleware will load it on later requests).
    set_time_override(dt)
    return {"time_override": dt.isoformat() if dt else None, "effective_now": now_utc().isoformat()}


@router.get("/time")
async def get_time(db: AsyncSession = Depends(get_db)):
    dt = await get_time_override(db)
    return {"time_override": dt.isoformat() if dt else None, "effective_now": now_utc().isoformat()}


@router.post("/run-nightly")
async def run_nightly(db: AsyncSession = Depends(get_db)):
    stats = await run_nightly_resolution(db, now_utc())
    await db.commit()
    return {"ran": "nightly_resolution", "stats": stats}


@router.post("/run-idle-checkout")
async def run_idle(db: AsyncSession = Depends(get_db)):
    closed = await run_idle_checkout(db, now_utc())
    await db.commit()
    return {"ran": "idle_checkout", "closed": closed}
