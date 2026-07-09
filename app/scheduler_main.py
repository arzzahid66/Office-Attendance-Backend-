"""Dedicated scheduler process. Run EXACTLY ONE of these across the whole deployment:

    RUN_SCHEDULER=true python -m app.scheduler_main

API/gunicorn workers must run with RUN_SCHEDULER unset/false so they never start the jobs.
See deploy/attendance-scheduler.service.
"""

import asyncio
import logging
import signal

from app.config import get_settings
from app.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("scheduler_main")


async def main() -> None:
    settings = get_settings()
    if not settings.run_scheduler:
        logger.error("RUN_SCHEDULER is not true; refusing to start. Set RUN_SCHEDULER=true for this process only.")
        raise SystemExit(1)

    start_scheduler()
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows: add_signal_handler isn't supported; Ctrl-C still raises KeyboardInterrupt.
            pass

    logger.info("scheduler process running; waiting for shutdown signal")
    try:
        await stop.wait()
    finally:
        stop_scheduler()
        logger.info("scheduler process stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
