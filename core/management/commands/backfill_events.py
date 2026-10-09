from datetime import datetime, time

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import DeviceList
from core.services import ZKClient, ZKError, backfill_events


class Command(BaseCommand):
    help = (
        "Pull ZK events from the start of a date (00:00 WIB) until now into EventLog, for history and "
        "the daily recap only. Does not change current_count or send Work Orders."
    )

    def add_arguments(self, parser):
        parser.add_argument("--date", help="Start date YYYY-MM-DD (default: today)")
        parser.add_argument("--device", help="Device ID (default: all devices)")

    def handle(self, *args, **options):
        try:
            day = datetime.strptime(options["date"], "%Y-%m-%d").date() if options["date"] else timezone.localdate()
        except ValueError:
            raise CommandError("--date must be YYYY-MM-DD")
        start = timezone.make_aware(datetime.combine(day, time.min))

        devices = DeviceList.objects.filter(type=DeviceList.TYPE_PEOPLE)
        if options["device"]:
            devices = devices.filter(device_id=options["device"])
            if not devices:
                raise CommandError(f"Device {options['device']} not found")

        client = ZKClient()
        for device in devices:
            try:
                stored, relabeled = backfill_events(client, device, start)
            except ZKError as exc:
                self.stderr.write(f"Device {device.device_id}: backfill failed: {exc}")
                continue
            self.stdout.write(self.style.SUCCESS(
                f"Device {device.device_id}: {stored} events stored since {start:%Y-%m-%d %H:%M}, "
                f"{relabeled} existing events relabeled."
            ))
