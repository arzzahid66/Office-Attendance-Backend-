"""In-process job registration for the DEDICATED scheduler process
(app/scheduler_main.py, gated by RUN_SCHEDULER=true). API/gunicorn workers must NOT start it.

Concurrency safety — three independent layers:

  1. Exactly ONE scheduler process runs (systemd single unit; RUN_SCHEDULER gates it).

  2. `pg_try_advisory_xact_lock(key)` taken as the first statement of the job's transaction.
     It is TRANSACTION-scoped, so it auto-releases on COMMIT/ROLLBACK — there is no
     pg_advisory_unlock to leak, even if the process crashes mid-job. Verified to give real
     mutual exclusion through SQLAlchemy-async + asyncpg on Neon's POOLED endpoint (a
     transaction pins its server backend, so xact-scoped advisory locks work there; a
     SESSION-scoped lock would not).

  3. The job bodies are idempotent regardless: every write is a compare-and-set
     (`... WHERE check_out IS NULL`) or a guarded upsert, so even a simultaneous double-run
     converges on the same rows and values.
"""

import logging
from typing import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import text

from app.app_state import get_time_override
from app.database import AsyncSessionLocal
from app.nightly_service import run_idle_checkout, run_nightly_resolution
from app.timeutil import DISPLAY_TZ, now_utc, set_time_override

logger = logging.getLogger("scheduler")

# Distinct advisory-lock keys per job (arbitrary but stable bigints).
LOCK_IDLE_CHECKOUT = 911_001
LOCK_NIGHTLY = 911_002

scheduler = AsyncIOScheduler(timezone=DISPLAY_TZ)


async def run_locked(lock_key: int, name: str, body: Callable[..., Awaitable]) -> None:
    """Run `body(db, now)` inside ONE transaction that holds an advisory xact lock.
    If another process holds the lock, skip silently. The lock is released by the
    COMMIT/ROLLBACK that ends the transaction — never by an explicit unlock."""
    async with AsyncSessionLocal() as db:
        acquired = (await db.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": lock_key})).scalar()
        if not acquired:
            await db.rollback()
            logger.info("job %s skipped: advisory lock %s held by another process", name, lock_key)
            return
        # Honour the DEV_MODE clock override (written via /debug/set-time into app_state),
        # so a job fired here behaves identically to one fired via /debug/run-*.
        set_time_override(await get_time_override(db))
        try:
            result = await body(db, now_utc())
            await db.commit()  # releases the advisory xact lock
            logger.info("job %s done: %s", name, result)
        except Exception:
            await db.rollback()  # also releases the lock
            logger.exception("job %s failed", name)
            raise


async def job_idle_checkout() -> None:
    await run_locked(LOCK_IDLE_CHECKOUT, "idle_checkout", run_idle_checkout)


async def job_nightly() -> None:
    await run_locked(LOCK_NIGHTLY, "nightly", run_nightly_resolution)


def start_scheduler() -> None:
    scheduler.add_job(job_idle_checkout, CronTrigger(minute="*/15"), id="idle_checkout", replace_existing=True)
    scheduler.add_job(job_nightly, CronTrigger(hour=23, minute=55), id="nightly", replace_existing=True)
    scheduler.start()
    logger.info("scheduler started (idle_checkout every 15m, nightly 23:55 %s)", DISPLAY_TZ)


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
