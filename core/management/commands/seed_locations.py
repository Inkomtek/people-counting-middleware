import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.locations import LEVELS, assign_devices, ensure_location
from core.models import Client, Region

DEFAULT_FILE = Path(settings.BASE_DIR) / "core" / "data" / "locations.json"


class Command(BaseCommand):
    help = (
        "Load the location hierarchy from core/data/locations.json and attach its devices. "
        "Real locations always; --demo adds the demo set, --cleanup-demo removes it. Idempotent."
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
                self._load("Demo", demo)

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
        for entry in entries:
            scope, created = ensure_location(*(entry[level] for level in LEVELS))
            new = [level for level in LEVELS if created[level]]
            self.stdout.write(f"  {scope}  [{'created: ' + ', '.join(new) if new else 'exists'}]")
            assigned, missing = assign_devices(scope, entry.get("devices", []))
            for device, previous in assigned:
                note = f" (was: {previous})" if previous not in (None, scope) else ""
                self.stdout.write(f"    device {device.id} assigned{note}")
            for device_id in missing:
                self.stdout.write(self.style.WARNING(f"    device {device_id} not on this server, skipped"))
        self.stdout.write(self.style.SUCCESS(f"{label}: done"))

    def _cleanup_demo(self, real, demo):
        """Delete demo clients (with their sites/areas/scopes) and demo regions that are not used by
        a real location. Devices on removed scopes fall back to "unassigned" (scope is SET_NULL)."""
        real_clients = {entry["client"] for entry in real}
        real_regions = {entry["region"] for entry in real}
        clients = {entry["client"] for entry in demo} - real_clients
        deleted, _ = Client.objects.filter(name__in=clients).delete()
        regions = Region.objects.filter(name__in={entry["region"] for entry in demo} - real_regions, sites__isnull=True)
        region_count = regions.count()
        regions.delete()
        self.stdout.write(self.style.SUCCESS(
            f"Demo removed: clients {sorted(clients) or '-'}, {deleted} rows; {region_count} unused region(s)."
        ))
