import random
import socket
import uuid

import requests
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core.models import DeviceList, Scope
from washroom.models import PPM_TYPES, READING_TYPES, SATISFACTION_TYPE, ApiClient, StatusRule
from washroom.services import evaluate_condition

DUMMY_PREFIX = "DUMMY-"
DUMMY_CLIENT = "dummy-tester"

# Level range per scenario: (dispensers %, trash %, ammonia ppm).
SCENARIOS = {
    "normal": {"dispenser": (60, 100), "trash": (0, 50), "ammonia": (0, 8)},
    "warning": {"dispenser": (5, 30), "trash": (70, 89), "ammonia": (10, 24)},
    "critical": {"dispenser": (0, 0), "trash": (90, 100), "ammonia": (25, 60)},
}
RATINGS = {"normal": (4, 5), "warning": (3, 4), "critical": (1, 2)}
# Inside Docker the web container is reachable as web.internal, which every server must list in
# DJANGO_ALLOWED_HOSTS (production does not allow 127.0.0.1); without Docker fall back to runserver.
DOCKER_BASE_URL = "http://web.internal:8000"
LOCAL_BASE_URL = "http://127.0.0.1:8000"


def default_base_url():
    try:
        socket.gethostbyname("web.internal")
    except OSError:
        return LOCAL_BASE_URL
    return DOCKER_BASE_URL


class Command(BaseCommand):
    help = (
        "Test the washroom API end to end: register DUMMY-* devices on a toilet (a Scope, like the dashboard; "
        "or building / floor / gender, "
        "like the dashboard), then POST readings (in the sensor team's raw data format) and ratings over HTTP like they would. "
        "Use --cleanup to remove all dummy data."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--base-url",
            help=f"Server running the API. Default: {DOCKER_BASE_URL} inside Docker, else {LOCAL_BASE_URL}",
        )
        parser.add_argument(
            "--scope", type=int,
            help="Scope id of the toilet. Default: the Scope of the first people counter that has one",
        )
        parser.add_argument("--building", help="Default: the toilet of the first located people counter")
        parser.add_argument("--floor")
        parser.add_argument("--gender", choices=[value for value, _ in DeviceList.GENDER_CHOICES])
        parser.add_argument(
            "--scenario", choices=["random", *SCENARIOS], default="random",
            help="normal = all green, warning = orange, critical = red, random = mixed (default)",
        )
        parser.add_argument("--ratings", type=int, default=10, help="Number of customer rating presses to send")
        parser.add_argument("--cleanup", action="store_true", help="Delete all dummy devices, data and the test key")

    def handle(self, *args, **options):
        if options["cleanup"]:
            return self.cleanup()
        options["base_url"] = options["base_url"] or default_base_url()

        toilet = self.toilet(options)
        client, _ = ApiClient.objects.get_or_create(name=DUMMY_CLIENT, defaults={"key_hash": "", "key_prefix": ""})
        api_key = client.set_new_key()
        client.is_active = True
        client.save()

        # Pre-register the devices with a location so the dashboard can place them; real devices get
        # their location in Admin.
        for device_type in [*READING_TYPES, SATISFACTION_TYPE]:
            DeviceList.objects.update_or_create(
                device_id=self.device_id(device_type, toilet),
                defaults={"type": device_type, "name": f"Dummy {device_type}", **toilet},
            )

        self.stdout.write(f"Toilet : {toilet['scope'] or ''} "
                          f"({toilet['building'] or '-'} / lantai {toilet['floor'] or '-'} / {toilet['gender'] or '-'})")
        self.stdout.write(f"API    : {options['base_url']}/api/v1/  (key '{DUMMY_CLIENT}' regenerated)\n")

        rules = list(StatusRule.objects.all())
        readings = [self.reading(t, toilet, options["scenario"], rules) for t in READING_TYPES]
        if ok := self.post(options["base_url"], api_key, "readings/", readings):
            for item in ok["data"]:
                unit = "ppm" if item["type"] in PPM_TYPES else "%"
                self.stdout.write(
                    f"  {item['type']:<13} value {item['value']:>5}{unit:<4} battery {item['battery']:>3}%  "
                    f"-> {item['status'] or '-'} ({item['severity'] or '-'})"
                )

        ratings = [
            {"device_id": self.device_id(SATISFACTION_TYPE, toilet), "rating": self.rating(options["scenario"])}
            for _ in range(options["ratings"])
        ]
        if ratings and (ok := self.post(options["base_url"], api_key, "customer-responses/", ratings)):
            values = [item["rating"] for item in ok["data"]]
            self.stdout.write(f"  satisfaction  {len(values)} ratings, average {sum(values) / len(values):.1f}")

        self.stdout.write(self.style.SUCCESS(
            "\nDone. Check the stored data in Admin -> Sensor readings / Customer responses."
        ))
        self.stdout.write("Remove the dummy data later with: python manage.py send_dummy_data --cleanup")

    def toilet(self, options):
        """The DeviceList location fields for the dummy devices: a Scope (what the dashboard filters by)
        and/or building / floor / gender, taken from the first located people counter by default."""
        people = DeviceList.objects.filter(type=DeviceList.TYPE_PEOPLE).order_by("id")
        if options["scope"]:
            scope = Scope.objects.filter(pk=options["scope"]).first()
            if scope is None:
                raise CommandError(f"Scope {options['scope']} does not exist.")
            anchor = people.filter(scope=scope).first()
        else:
            anchor = people.exclude(scope=None).first() or people.exclude(building="").first()
            scope = anchor.scope if anchor else None
        toilet = {
            "scope": scope,
            "building": options["building"] or (anchor.building if anchor else ""),
            "floor": options["floor"] or (anchor.floor if anchor else ""),
            "gender": options["gender"] or (anchor.gender if anchor else ""),
        }
        if scope is None and not all(toilet[field] for field in ("building", "floor", "gender")):
            raise CommandError("No toilet location found. Pass --scope, or --building, --floor and --gender.")
        return toilet

    @staticmethod
    def device_id(device_type, toilet):
        if toilet["scope"]:
            return f"{DUMMY_PREFIX}{device_type.upper()}-S{toilet['scope'].pk}"
        return f"{DUMMY_PREFIX}{device_type.upper()}-{toilet['floor']}-{toilet['gender']}".replace(" ", "")

    def reading(self, device_type, toilet, scenario, rules):
        """A reading as the sensor team sends it; they compute the status, here from our Status rules."""
        level_scenario = random.choice(list(SCENARIOS)) if scenario == "random" else scenario
        key = device_type if device_type in ("trash", "ammonia") else "dispenser"
        low, high = SCENARIOS[level_scenario][key]
        value = round(random.uniform(low, high), 1) if device_type in PPM_TYPES else random.randint(low, high)
        now = timezone.now().isoformat(timespec="seconds")
        return {
            "id": uuid.uuid4().hex[:12],
            "inputDate": now,
            "deviceId": self.device_id(device_type, toilet),
            "value": value,
            "battery": random.randint(20, 100),
            "lastOnline": now,
            "status": evaluate_condition(device_type, value, rules)[0],
        }

    @staticmethod
    def rating(scenario):
        low, high = RATINGS[random.choice(list(RATINGS))] if scenario == "random" else RATINGS[scenario]
        return random.randint(low, high)

    def post(self, base_url, api_key, path, payload):
        url = f"{base_url.rstrip('/')}/api/v1/{path}"
        try:
            response = requests.post(url, json=payload, headers={"X-API-Key": api_key}, timeout=15)
        except requests.RequestException as exc:
            raise CommandError(f"Cannot reach {url}: {exc}. Is the server running?")
        self.stdout.write(f"POST {path:<20} -> HTTP {response.status_code}")
        if response.status_code != 201:
            self.stderr.write(response.text[:2000])
            if response.status_code == 400 and "status" not in response.text[:200]:
                # Not the API's own JSON error: Django rejected the Host (DJANGO_ALLOWED_HOSTS).
                host = url.split("/")[2].split(":")[0]
                self.stderr.write(
                    f"The server rejected the host '{host}'. Add it to DJANGO_ALLOWED_HOSTS or pass "
                    f"--base-url with an allowed host (in Docker: {DOCKER_BASE_URL})."
                )
            return None
        return response.json()

    def cleanup(self):
        # Readings and ratings cascade with their devices.
        devices = DeviceList.objects.filter(device_id__startswith=DUMMY_PREFIX)
        count = devices.count()
        devices.delete()
        clients, _ = ApiClient.objects.filter(name=DUMMY_CLIENT).delete()
        self.stdout.write(self.style.SUCCESS(
            f"Deleted {count} dummy devices (with their readings and ratings) and {clients} test key."
        ))
