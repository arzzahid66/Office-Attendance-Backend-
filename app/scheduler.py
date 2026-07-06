import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.checks_service import generate_checks_for_day
from app.database import AsyncSessionLocal
from app.nightly_service import apply_checkout_fallback, resolve_day, sweep_missed_checks
from app.timeutil import DISPLAY_TZ, today_local

logger = logging.getLogger("scheduler")

scheduler = AsyncIOScheduler(timezone=DISPLAY_TZ)


async def job_generate_random_checks() -> None:
    async with AsyncSessionLocal() as db:
        created = await generate_checks_for_day(db, today_local())
        logger.info("Generated %d random checks for %s", created, today_local())


async def job_sweep_and_resolve() -> None:
    async with AsyncSessionLocal() as db:
        missed = await sweep_missed_checks(db, today_local())
        await apply_checkout_fallback(db, today_local())
        resolved = await resolve_day(db, today_local())
        logger.info("Swept %d checks to missed; resolved %d attendance days for %s", missed, resolved, today_local())


def start_scheduler() -> None:
    scheduler.add_job(job_generate_random_checks, CronTrigger(hour=0, minute=5), id="generate_random_checks", replace_existing=True)
    scheduler.add_job(job_sweep_and_resolve, CronTrigger(hour=23, minute=55), id="nightly_resolution", replace_existing=True)
    scheduler.start()


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
