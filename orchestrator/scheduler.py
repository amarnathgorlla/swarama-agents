"""
SWARAMA Agent Scheduler
=======================
APScheduler-based runner for continuous (Docker) mode.
Three jobs:
  1. Every 2 hours  — monitoring agents + email report
  2. On startup     — testing agents once
  3. Every 24 hours — trend_agent + postmortem_agent
"""

import asyncio
import logging
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger("scheduler")


def _run_async(coro):
    """Run an async coroutine from a synchronous APScheduler job thread."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(coro)
    finally:
        loop.close()


def _monitoring_job():
    logger.info("[SCHEDULER] Firing monitoring pass job")
    from orchestrator.orchestrator import run_monitoring_pass
    _run_async(run_monitoring_pass())


def _testing_job():
    logger.info("[SCHEDULER] Firing testing agents (startup) job")
    from orchestrator.orchestrator import run_push_trigger
    _run_async(run_push_trigger())


def _daily_job():
    logger.info("[SCHEDULER] Firing daily trend + postmortem job")
    from orchestrator.orchestrator import run_daily_pass
    _run_async(run_daily_pass())


def start_scheduler():
    """Start APScheduler and block the main thread."""
    scheduler = BackgroundScheduler(timezone="UTC")

    # Job 2: On startup — run testing agents once
    scheduler.add_job(
        _testing_job,
        trigger="date",  # fire once immediately
        id="startup_testing",
        name="Startup testing agents",
        replace_existing=True,
    )

    # Job 3: Every 24 hours at midnight UTC — trend + postmortem
    scheduler.add_job(
        _daily_job,
        trigger=CronTrigger(hour=0, minute=0),
        id="daily_reports",
        name="Daily trend and postmortem",
        replace_existing=True,
        max_instances=1,
    )

    scheduler.start()
    logger.info("APScheduler started. Jobs registered: testing (startup), daily (midnight)")

    # Keep the main thread alive
    stop_event = threading.Event()
    try:
        stop_event.wait()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopping...")
        scheduler.shutdown(wait=False)
