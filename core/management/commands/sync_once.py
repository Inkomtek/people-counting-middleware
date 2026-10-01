from django.core.management.base import BaseCommand

from core.services import sync_all


class Command(BaseCommand):
    help = "Run one sync cycle for all devices (fetch ZK events, count, dispatch Work Orders)."

    def handle(self, *args, **options):
        sync_all()
        self.stdout.write(self.style.SUCCESS("Sync cycle finished."))
