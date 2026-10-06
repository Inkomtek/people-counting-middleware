from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core.models import Area, Client, DeviceList, Region, Scope, Site


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
        names = {level: options[level].strip() for level in ("client", "region", "site", "area", "scope")}
        empty = [level for level, name in names.items() if not name]
        if empty:
            raise CommandError(f"Empty value for: {', '.join('--' + level for level in empty)}")

        devices = list(DeviceList.objects.filter(id__in=options["device"]))
        missing = sorted(set(options["device"]) - {device.id for device in devices})
        if missing:
            raise CommandError(f"Unknown device ID(s): {', '.join(missing)}")

        client, client_new = Client.objects.get_or_create(name=names["client"])
        region, region_new = Region.objects.get_or_create(name=names["region"])
        site, site_new = Site.objects.get_or_create(client=client, region=region, name=names["site"])
        area, area_new = Area.objects.get_or_create(site=site, name=names["area"])
        scope, scope_new = Scope.objects.get_or_create(area=area, name=names["scope"])

        for label, obj, created in (("Client", client, client_new), ("Region", region, region_new),
                                    ("Site", site, site_new), ("Area", area, area_new), ("Scope", scope, scope_new)):
            self.stdout.write(f"{label:<7} {'created' if created else 'exists '}  {obj.name}")

        for device in devices:
            previous = device.scope
            device.scope = scope
            device.save(update_fields=["scope"])
            note = "" if previous in (None, scope) else f" (was: {previous})"
            self.stdout.write(f"Device  assigned {device.id}{note}")

        self.stdout.write(self.style.SUCCESS(f"Location ready: {scope}"))
