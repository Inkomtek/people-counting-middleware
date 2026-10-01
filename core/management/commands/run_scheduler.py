import logging

from apscheduler.schedulers.blocking import BlockingScheduler
from django.core.management.base import BaseCommand
from django.db import close_old_connections

from core.models import SchedulerConfig
from core.services import sync_all

logger = logging.getLogger(__name__)

SYNC_JOB_ID = "sync_all"
# How often the scheduler re-reads SchedulerConfig to pick up interval changes made in Admin.
CONFIG_CHECK_SECONDS = 30


def run_sync():
    close_old_connections()
    if not SchedulerConfig.get().enabled:
        return
    try:
        sync_all()
    except Exception:
        logger.exception("Sync cycle crashed")


class Command(BaseCommand):
    help = "Run the APScheduler loop that syncs all devices on the interval set in Admin."

    def handle(self, *args, **options):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        scheduler = BlockingScheduler(timezone="Asia/Jakarta")
        state = {"interval": SchedulerConfig.get().interval_minutes}

        scheduler.add_job(
            run_sync, "interval", minutes=state["interval"], id=SYNC_JOB_ID,
            max_instances=1, coalesce=True,
        )

        def check_config():
            close_old_connections()
            interval = SchedulerConfig.get().interval_minutes
            if interval != state["interval"]:
                scheduler.reschedule_job(SYNC_JOB_ID, trigger="interval", minutes=interval)
                state["interval"] = interval
                logger.info("Sync interval changed to %s minute(s)", interval)

        scheduler.add_job(check_config, "interval", seconds=CONFIG_CHECK_SECONDS, id="check_config")

        self.stdout.write(f"Scheduler started: sync every {state['interval']} minute(s). Ctrl+C to stop.")
        run_sync()
        try:
            scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            pass
