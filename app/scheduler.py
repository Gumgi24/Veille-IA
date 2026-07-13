"""Campaign scheduling.

Each active campaign with a schedule_time gets one daily cron job at that
time (server local time). The job itself decides whether today matches the
campaign's interval (every N days from start_date) and end date — this keeps
"every 2 days", "every week", etc. trivial and restart-safe.
"""
from __future__ import annotations

import datetime as dt
import json

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from . import db, runner

scheduler = AsyncIOScheduler()


def _job_id(campaign_id: int) -> str:
    return f"campaign-{campaign_id}"


def _should_fire(campaign) -> tuple[bool, str]:
    today = dt.date.today()
    start = dt.date.fromisoformat(campaign["start_date"]) if campaign["start_date"] else today
    if today < start:
        return False, f"before start date {start}"
    if campaign["end_date"] and today > dt.date.fromisoformat(campaign["end_date"]):
        return False, f"past end date {campaign['end_date']}"
    interval = max(1, campaign["interval_days"] or 1)
    if (today - start).days % interval != 0:
        return False, f"not an interval day (every {interval} days from {start})"
    return True, ""


async def _fire(campaign_id: int) -> None:
    with db.connect() as conn:
        campaign = conn.execute(
            "SELECT * FROM campaigns WHERE id=?", (campaign_id,)
        ).fetchone()
    if campaign is None or campaign["status"] != "active":
        return
    ok, reason = _should_fire(campaign)
    if not ok:
        db.log_event(campaign_id, None, "info", "scheduler", f"Skipped scheduled run: {reason}")
        return
    try:
        run_id = runner.start_run(campaign_id, trigger="schedule")
        db.log_event(campaign_id, run_id, "info", "scheduler", "Scheduled run started")
    except RuntimeError as exc:
        db.log_event(campaign_id, None, "warning", "scheduler", str(exc))


def sync_campaign(campaign_id: int) -> None:
    """(Re)create or remove the cron job to match the campaign row."""
    job_id = _job_id(campaign_id)
    existing = scheduler.get_job(job_id)
    if existing:
        existing.remove()

    with db.connect() as conn:
        campaign = conn.execute(
            "SELECT * FROM campaigns WHERE id=?", (campaign_id,)
        ).fetchone()
    if campaign is None or campaign["status"] != "active" or not campaign["schedule_time"]:
        return

    try:
        hour, minute = (int(x) for x in campaign["schedule_time"].split(":"))
    except ValueError:
        db.log_event(campaign_id, None, "error", "scheduler",
                     f"Invalid schedule_time: {campaign['schedule_time']!r}")
        return

    scheduler.add_job(
        _fire,
        CronTrigger(hour=hour, minute=minute),
        id=job_id,
        args=[campaign_id],
        replace_existing=True,
        misfire_grace_time=3600,
    )


def next_run_time(campaign_id: int):
    job = scheduler.get_job(_job_id(campaign_id))
    return job.next_run_time.isoformat(timespec="seconds") if job and job.next_run_time else None


def start() -> None:
    with db.connect() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM campaigns").fetchall()]
    for campaign_id in ids:
        sync_campaign(campaign_id)
    scheduler.start()


def shutdown() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
