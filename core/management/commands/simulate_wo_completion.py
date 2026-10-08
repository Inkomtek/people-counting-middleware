import random
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import NotificationLog

# Only notifications sent to the local dummy API are touched, so real Algospection Work Orders are never faked.
DUMMY_URL_PART = "/dummy/"


class Command(BaseCommand):
    help = (
        "Dev only: pretend Algospection finished the Work Orders of successful dummy notifications, so the "
        "dashboard's completion chart has data. Each gets a random completion time; the newest --open stay "
        "unfinished. Real Algospection notifications are never changed. --reset clears the simulated times."
    )

    def add_arguments(self, parser):
        parser.add_argument("--open", type=int, default=3, help="Newest successful notifications left unfinished")
        parser.add_argument("--min", type=int, default=8, help="Shortest completion time in minutes")
        parser.add_argument("--max", type=int, default=65, help="Longest completion time in minutes")
        parser.add_argument("--seed", type=int, help="Random seed, for a repeatable result")
        parser.add_argument("--reset", action="store_true", help="Clear completed_at of every dummy notification")

    def handle(self, *args, **options):
        logs = NotificationLog.objects.filter(endpoint_url__contains=DUMMY_URL_PART)
        if options["reset"]:
            cleared = logs.exclude(completed_at=None).update(completed_at=None)
            self.stdout.write(self.style.SUCCESS(f"Cleared the completion time of {cleared} dummy notification(s)."))
            return

        rng = random.Random(options["seed"])
        now = timezone.now()
        successful = list(logs.filter(success=True).order_by("-time"))
        left_open = successful[:max(options["open"], 0)]
        finished = 0
        for log in successful[len(left_open):]:
            completed = log.time + timedelta(minutes=rng.randint(options["min"], options["max"]))
            # A Work Order cannot finish in the future.
            NotificationLog.objects.filter(pk=log.pk).update(completed_at=min(completed, now))
            finished += 1
        if left_open:
            NotificationLog.objects.filter(pk__in=[log.pk for log in left_open]).update(completed_at=None)
        self.stdout.write(self.style.SUCCESS(
            f"Simulated {finished} finished and {len(left_open)} open Work Order(s) "
            f"({options['min']}-{options['max']} min). Undo with --reset."
        ))
