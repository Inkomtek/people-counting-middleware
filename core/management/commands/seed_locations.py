import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from django.utils import timezone

from core.locations import LEVELS, assign_devices, ensure_location
from core.models import Client, DeviceList, Region
from washroom.models import READING_TYPES, SATISFACTION_TYPE, CustomerResponse, SensorReading
from washroom.services import evaluate_condition

DEFAULT_FILE = Path(settings.BASE_DIR) / "core" / "data" / "locations.json"
DEMO_PREFIX = "DEMO-"
# Demo readings cycle through these per toilet so the cards show normal, warning and critical rows.
DEMO_LEVELS = {
    "soap": [80, 25, 5], "toilet-paper": [90, 30, 0], "tissue": [70, 20, 3],
    "trash": [15, 70, 95], "ammonia": [3, 12, 30],
}
DEMO_BATTERY = [92, 55, 14]
DEMO_RATINGS = [5, 4, 4, 3, 5, 2, 4, 5, 3, 4]


class Command(BaseCommand):
    help = (
        "Load the location hierarchy from core/data/locations.json and attach its devices. "
        "Real locations always; --demo adds the demo set plus DEMO-* sensors with readings and ratings, "
        "--cleanup-demo removes them. Idempotent (each --demo run adds one more reading per demo sensor)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--file", default=str(DEFAULT_FILE), help="Locations JSON (default: core/data/locations.json)")
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--demo", action="store_true", help="Also load the demo locations")
        group.add_argument("--cleanup-demo", action="store_true", help="Delete the demo-only clients and regions")

    def handle(self, *args, **options):
        data = self._read(options["file"])
        real, demo = data.get("locations", []), data.get("demo", [])
        with transaction.atomic():
            if options["cleanup_demo"]:
                self._cleanup_demo(real, demo)
                return
            self._load("Locations", real)
            if options["demo"]:
                scopes = self._load("Demo", demo)
                self._demo_sensors(scopes)

    def _read(self, path):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CommandError(f"Cannot read {path}: {exc}") from exc
        for entry in data.get("locations", []) + data.get("demo", []):
            missing = [level for level in LEVELS if not str(entry.get(level, "")).strip()]
            if missing:
                raise CommandError(f"Entry {entry} is missing: {', '.join(missing)}")
        return data

    def _load(self, label, entries):
        self.stdout.write(self.style.MIGRATE_HEADING(f"{label} ({len(entries)})"))
        scopes = []
        for entry in entries:
            scope, created = ensure_location(*(entry[level] for level in LEVELS))
            scopes.append(scope)
            new = [level for level in LEVELS if created[level]]
            self.stdout.write(f"  {scope}  [{'created: ' + ', '.join(new) if new else 'exists'}]")
            assigned, missing = assign_devices(scope, entry.get("devices", []))
            for device, previous in assigned:
                note = f" (was: {previous})" if previous not in (None, scope) else ""
                self.stdout.write(f"    device {device.id} assigned{note}")
            for device_id in missing:
                self.stdout.write(self.style.WARNING(f"    device {device_id} not on this server, skipped"))
        self.stdout.write(self.style.SUCCESS(f"{label}: done"))
        return scopes

    def _demo_sensors(self, scopes):
        """One device per washroom sensor type in each demo toilet, with a reading for today and,
        for satisfaction, ten ratings. Device ids start with DEMO- so --cleanup-demo can remove them.
        Written straight to the database (no HTTP, no API key)."""
        now = timezone.now()
        for index, scope in enumerate(scopes):
            for sensor_type in READING_TYPES:
                device = self._demo_device(scope, sensor_type)
                level = DEMO_LEVELS[sensor_type][index % 3]
                condition, severity = evaluate_condition(sensor_type, level)
                SensorReading.objects.create(
                    device=device, time=now, level=level, battery=DEMO_BATTERY[index % 3],
                    condition=condition, severity=severity, payload={"demo": True},
                )
            device = self._demo_device(scope, SATISFACTION_TYPE)
            CustomerResponse.objects.bulk_create([
                CustomerResponse(device=device, time=now, rating=rating, payload={"demo": True})
                for rating in DEMO_RATINGS[index % 3:] + DEMO_RATINGS[:index % 3]
            ])
            self.stdout.write(f"  demo sensors + readings: {scope}")

    def _demo_device(self, scope, sensor_type):
        device, _ = DeviceList.objects.update_or_create(
            id=f"{DEMO_PREFIX}{sensor_type.upper()}-{scope.pk}",
            defaults={"type": sensor_type, "name": f"Demo {sensor_type}", "scope": scope},
        )
        return device

    def _cleanup_demo(self, real, demo):
        """Delete demo clients (with their sites/areas/scopes) and demo regions that are not used by
        a real location. Devices on removed scopes fall back to "unassigned" (scope is SET_NULL)."""
        real_clients = {entry["client"] for entry in real}
        real_regions = {entry["region"] for entry in real}
        clients = {entry["client"] for entry in demo} - real_clients
        demo_devices, _ = DeviceList.objects.filter(id__startswith=DEMO_PREFIX).delete()
        deleted, _ = Client.objects.filter(name__in=clients).delete()
        regions = Region.objects.filter(name__in={entry["region"] for entry in demo} - real_regions, sites__isnull=True)
        region_count = regions.count()
        regions.delete()
        self.stdout.write(self.style.SUCCESS(
            f"Demo removed: clients {sorted(clients) or '-'}, {deleted} rows; {region_count} unused region(s); "
            f"demo devices with their data: {demo_devices} rows."
        ))
