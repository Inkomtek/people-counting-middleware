from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.locations import LEVELS, assign_devices, ensure_location
from core.models import DeviceList


class Command(BaseCommand):
    help = (
        "Create (or reuse) a Client / Region / Site / Area / Scope location and optionally assign devices "
        "to the Scope. Safe to run repeatedly: existing names are reused, nothing is duplicated."
    )

    def add_arguments(self, parser):
        parser.add_argument("--client", required=True, help='e.g. "ISS"')
        parser.add_argument("--region", required=True, help='e.g. "Banten" (shared by all clients)')
        parser.add_argument("--site", required=True, help='e.g. "Bintaro"')
        parser.add_argument("--area", required=True, help='e.g. "Graha ISS"')
        parser.add_argument("--scope", required=True, help='e.g. "Floor 2 - Toilet Pria" (one toilet)')
        parser.add_argument(
            "--device", action="append", default=[], metavar="ID",
            help="Device ID to assign to this Scope; repeat for several devices",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        missing = sorted(set(options["device"]) - set(DeviceList.objects.filter(id__in=options["device"])
                                                       .values_list("id", flat=True)))
        if missing:
            raise CommandError(f"Unknown device ID(s): {', '.join(missing)}")
        try:
            scope, created = ensure_location(*(options[level] for level in LEVELS))
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        objects = {"scope": scope, "area": scope.area, "site": scope.area.site,
                   "region": scope.area.site.region, "client": scope.area.site.client}
        for level in LEVELS:
            label = level.capitalize()
            self.stdout.write(f"{label:<7} {'created' if created[level] else 'exists '}  {objects[level].name}")

        assigned, _ = assign_devices(scope, options["device"])
        for device, previous in assigned:
            note = "" if previous in (None, scope) else f" (was: {previous})"
            self.stdout.write(f"Device  assigned {device.id}{note}")

        self.stdout.write(self.style.SUCCESS(f"Location ready: {scope}"))
