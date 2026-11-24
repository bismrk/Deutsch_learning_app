"""Standalone APScheduler entrypoint (runs in dedicated `scheduler` container)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys
from datetime import UTC
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db.base import create_async_db_engine, create_session_factory
from app.scheduler.jobs import run_daily_reminders, run_weekly_reports
from app.scheduler.notifier import (
    TelegramConfigError,
    TelegramNotifier,
    TokenRedactionFilter,
)

logger = logging.getLogger(__name__)

DAILY_REMINDERS_JOB_ID = "daily_fsrs_reminders"
WEEKLY_REPORTS_JOB_ID = "weekly_progress_reports"
WEEKLY_REPORT_DAY_OF_WEEK = "sun"
WEEKLY_REPORT_HOUR_UTC = 18
WEEKLY_REPORT_MINUTE_UTC = 0


def configure_scheduler_logging(log_level: str = "INFO", bot_token: str | None = None) -> None:
    """Configure scheduler logging with token redaction and muted httpx URL logs."""
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    root_logger = logging.getLogger()

    if not root_logger.handlers:
        logging.basicConfig(
            level=numeric_level,
            format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        )
    else:
        root_logger.setLevel(numeric_level)

    redaction_filter = TokenRedactionFilter(bot_token=bot_token)
    for handler in root_logger.handlers:
        handler.addFilter(redaction_filter)

    # Prevent httpx/httpcore from logging raw Telegram Bot API URLs containing BOT_TOKEN.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def build_scheduler(
    session_factory: async_sessionmaker[AsyncSession],
    notifier: TelegramNotifier,
) -> AsyncIOScheduler:
    """Create a UTC AsyncIOScheduler with hourly FSRS reminders and Sunday weekly reports."""
    scheduler = AsyncIOScheduler(timezone=UTC)

    async def _daily_reminders_job() -> None:
        async with session_factory() as db:
            summary = await run_daily_reminders(db, notifier)
        logger.info(
            "Daily reminders finished: matched=%s sent=%s skipped_no_due=%s failed=%s",
            summary.matched_users,
            summary.sent,
            summary.skipped_no_due,
            summary.failed,
        )

    async def _weekly_reports_job() -> None:
        async with session_factory() as db:
            summary = await run_weekly_reports(db, notifier)
        logger.info(
            "Weekly reports finished: week_start=%s total=%s snapshots=%s sent=%s "
            "skipped_existing=%s failed=%s",
            summary.week_start.isoformat(),
            summary.total_users,
            summary.snapshots_created,
            summary.sent,
            summary.skipped_existing,
            summary.failed,
        )

    scheduler.add_job(
        _daily_reminders_job,
        trigger=CronTrigger(minute=0, timezone=UTC),
        id=DAILY_REMINDERS_JOB_ID,
        name="Hourly FSRS reminders",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=300,
    )
    scheduler.add_job(
        _weekly_reports_job,
        trigger=CronTrigger(
            day_of_week=WEEKLY_REPORT_DAY_OF_WEEK,
            hour=WEEKLY_REPORT_HOUR_UTC,
            minute=WEEKLY_REPORT_MINUTE_UTC,
            timezone=UTC,
        ),
        id=WEEKLY_REPORTS_JOB_ID,
        name="Sunday weekly progress reports",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=3600,
    )
    return scheduler


def _install_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    stop_event: asyncio.Event,
) -> list[tuple[signal.Signals, Any, bool]]:
    """Register SIGTERM/SIGINT handlers with fallback when add_signal_handler is unavailable."""
    installed: list[tuple[signal.Signals, Any, bool]] = []

    def _trigger_stop(signame: str) -> None:
        logger.info("Received %s; shutting down scheduler gracefully...", signame)
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _trigger_stop, sig.name)
            installed.append((sig, None, True))
        except (NotImplementedError, RuntimeError):
            try:
                previous = signal.getsignal(sig)

                def _sync_handler(
                    _signum: int,
                    _frame: Any,
                    *,
                    _sig_name: str = sig.name,
                ) -> None:
                    loop.call_soon_threadsafe(_trigger_stop, _sig_name)

                signal.signal(sig, _sync_handler)
                installed.append((sig, previous, False))
            except (ValueError, OSError):
                continue

    return installed


def _restore_signal_handlers(
    loop: asyncio.AbstractEventLoop,
    installed: list[tuple[signal.Signals, Any, bool]],
) -> None:
    for sig, previous, used_loop in installed:
        if used_loop:
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(sig)
        elif previous is not None:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, previous)


async def run_scheduler(
    settings: Settings | None = None,
    *,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Start the standalone APScheduler service and wait until SIGTERM/SIGINT."""
    resolved_settings = settings if settings is not None else get_settings()
    configure_scheduler_logging(
        log_level=resolved_settings.log_level,
        bot_token=resolved_settings.bot_token,
    )

    notifier = TelegramNotifier(bot_token=resolved_settings.bot_token)
    try:
        engine = create_async_db_engine(resolved_settings.database_url)
    except Exception:
        await notifier.aclose()
        raise
    session_factory = create_session_factory(engine)
    scheduler = build_scheduler(session_factory, notifier)

    shutdown_event = stop_event if stop_event is not None else asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_handlers = _install_signal_handlers(loop, shutdown_event)

    try:
        scheduler.start()
        logger.info(
            "Scheduler started in UTC with jobs: %s",
            ", ".join(job.id for job in scheduler.get_jobs()),
        )
        await shutdown_event.wait()
    finally:
        _restore_signal_handlers(loop, installed_handlers)
        if scheduler.running:
            scheduler.shutdown(wait=True)
        await notifier.aclose()
        await engine.dispose()
        logger.info("Scheduler stopped cleanly.")


def main() -> None:
    """CLI entrypoint for `python -m app.scheduler.main`."""
    try:
        asyncio.run(run_scheduler())
    except KeyboardInterrupt:
        return
    except (TelegramConfigError, ValueError) as exc:
        logger.error("Scheduler startup failed: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()


__all__ = [
    "DAILY_REMINDERS_JOB_ID",
    "WEEKLY_REPORTS_JOB_ID",
    "WEEKLY_REPORT_DAY_OF_WEEK",
    "WEEKLY_REPORT_HOUR_UTC",
    "WEEKLY_REPORT_MINUTE_UTC",
    "build_scheduler",
    "configure_scheduler_logging",
    "main",
    "run_scheduler",
]
