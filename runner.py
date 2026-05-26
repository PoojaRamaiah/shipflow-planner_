"""
scheduler/runner.py
APScheduler-based pipeline runner.
Runs all jobs on configured cron schedules.

Usage:
    python -m scheduler.runner             # Start in foreground
    python -m scheduler.runner --once      # Run all jobs once and exit (CI/testing)
    python -m scheduler.runner --job inv   # Run a specific job once

Jobs:
    inv      → inventory_snapshot  (daily 06:00)
    ships    → shipment_sync       (every 30 min)
    plan     → planning_engine     (daily 07:00)
    report   → daily email report  (weekdays 08:00)
"""

import argparse
import logging
import sys
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from etl.db import load_config, test_connection
from etl import inventory_snapshot, shipment_sync
from models import planning_engine, alerts

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("scheduler")


# ---------------------------------------------------------------------------
# Job functions
# ---------------------------------------------------------------------------

def job_inventory_snapshot():
    logger.info("▶ JOB: inventory_snapshot")
    result = inventory_snapshot.run()
    if result["status"] != "success":
        logger.error("inventory_snapshot FAILED: %s", result.get("error"))
    else:
        logger.info("inventory_snapshot OK — %d SKUs", result.get("total_skus", 0))
    return result


def job_shipment_sync():
    logger.info("▶ JOB: shipment_sync")
    result = shipment_sync.run()
    if result["status"] != "success":
        logger.error("shipment_sync FAILED: %s", result.get("error"))
    else:
        logger.info("shipment_sync OK — %d ships, %d lines",
                    result.get("shipments",0), result.get("lines",0))
    return result


def job_planning_refresh():
    logger.info("▶ JOB: planning_refresh")
    result = planning_engine.run()
    if result["status"] != "success":
        logger.error("planning_refresh FAILED: %s", result.get("error"))
        return result

    # Fire alerts if thresholds breached
    planning_df = result.get("_planning_df")
    non_buy_df  = result.get("_non_buy_df")
    if planning_df is not None:
        alerts.send_critical_alert(planning_df)
    if non_buy_df is not None:
        alerts.send_non_buyability_alert(non_buy_df)

    logger.info("planning_refresh OK — kpis=%s", result.get("kpis", {}))
    return result


def job_daily_report():
    logger.info("▶ JOB: daily_report")
    result = planning_engine.run()
    if result["status"] != "success":
        logger.error("daily_report planning run FAILED: %s", result.get("error"))
        return
    alerts.send_daily_summary(
        result["kpis"],
        result["_planning_df"],
        result["_non_buy_df"],
    )
    logger.info("daily_report sent")


# ---------------------------------------------------------------------------
# Job registry
# ---------------------------------------------------------------------------

JOBS = {
    "inv":    job_inventory_snapshot,
    "ships":  job_shipment_sync,
    "plan":   job_planning_refresh,
    "report": job_daily_report,
}


# ---------------------------------------------------------------------------
# Scheduler setup
# ---------------------------------------------------------------------------

def build_scheduler() -> BlockingScheduler:
    cfg = load_config()
    sched_cfg = cfg["scheduling"]
    tz = sched_cfg.get("timezone", "Asia/Kolkata")

    scheduler = BlockingScheduler(timezone=tz)

    scheduler.add_job(
        job_inventory_snapshot,
        CronTrigger.from_crontab(sched_cfg["inventory_snapshot"], timezone=tz),
        id="inventory_snapshot",
        name="Daily inventory snapshot",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        job_shipment_sync,
        CronTrigger.from_crontab(sched_cfg["shipment_sync"], timezone=tz),
        id="shipment_sync",
        name="Shipment sync (30-min)",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        job_planning_refresh,
        CronTrigger.from_crontab(sched_cfg["planning_refresh"], timezone=tz),
        id="planning_refresh",
        name="Planning model refresh",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        job_daily_report,
        CronTrigger.from_crontab(sched_cfg["kpi_email_report"], timezone=tz),
        id="daily_report",
        name="Daily email report",
        max_instances=1,
        coalesce=True,
    )
    return scheduler


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Inventory Planning Scheduler")
    parser.add_argument("--once",  action="store_true", help="Run all jobs once and exit")
    parser.add_argument("--job",   choices=list(JOBS.keys()), help="Run a specific job once")
    args = parser.parse_args()

    logger.info("Checking database connectivity …")
    if not test_connection():
        logger.critical("Cannot connect to database. Check config/settings.yaml and .env")
        sys.exit(1)
    logger.info("Database OK")

    if args.job:
        logger.info("Running single job: %s", args.job)
        JOBS[args.job]()
        return

    if args.once:
        logger.info("Running all jobs once …")
        for name, fn in JOBS.items():
            try:
                fn()
            except Exception as e:
                logger.error("Job %s failed: %s", name, e)
        return

    # Normal scheduler mode
    scheduler = build_scheduler()
    logger.info("Scheduler starting — %d jobs registered", len(scheduler.get_jobs()))
    for job in scheduler.get_jobs():
        logger.info("  %-25s next run: %s", job.name, job.next_run_time)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")


if __name__ == "__main__":
    main()
