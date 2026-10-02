import random

import requests
from django.core.management.base import BaseCommand, CommandError

from washroom.models import READING_TYPES, ApiClient, CustomerResponse, SensorDevice, SensorType, Washroom

DUMMY_PREFIX = "DUMMY-"
DUMMY_CLIENT = "dummy-tester"

# Level per scenario: (dispensers %, trash %, amonia ppm).
SCENARIOS = {
    "normal": {"dispenser": (60, 100), "trash": (0, 50), "amonia": (0, 8)},
    "warning": {"dispenser": (5, 30), "trash": (70, 89), "amonia": (10, 24)},
    "critical": {"dispenser": (0, 0), "trash": (90, 100), "amonia": (25, 60)},
}
RATINGS = {"normal": (4, 5), "warning": (3, 4), "critical": (1, 2)}


class Command(BaseCommand):
    help = (
        "Test the washroom API end to end: register dummy devices on a washroom, then POST readings and "
        "ratings over HTTP like the ZK side would. Use --cleanup to remove all dummy data."
    )

    def add_arguments(self, parser):
        parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Server running the API")
        parser.add_argument("--washroom", type=int, help="Washroom id (default: the first one)")
        parser.add_argument(
            "--scenario", choices=["random", *SCENARIOS], default="random",
            help="normal = all green, warning = orange, critical = red, random = mixed (default)",
        )
        parser.add_argument("--ratings", type=int, default=10, help="Number of customer rating presses to send")
        parser.add_argument("--cleanup", action="store_true", help="Delete all dummy devices, data and the test key")

    def handle(self, *args, **options):
        if options["cleanup"]:
            return self.cleanup()

        washroom = (
            Washroom.objects.filter(pk=options["washroom"]).first() if options["washroom"]
            else Washroom.objects.first()
        )
        if washroom is None:
            raise CommandError("Washroom not found. Create one in Admin -> Washrooms.")

        client, _ = ApiClient.objects.get_or_create(name=DUMMY_CLIENT, defaults={"key_hash": "", "key_prefix": ""})
        api_key = client.set_new_key()
        client.is_active = True
        client.save()

        # Devices must be assigned to a washroom to appear on its dashboard; real ones are assigned in Admin.
        types = [*READING_TYPES, SensorType.FEEDBACK]
        for sensor_type in types:
            SensorDevice.objects.update_or_create(
                id=self.device_id(sensor_type, washroom),
                defaults={"type": sensor_type, "washroom": washroom, "name": f"Dummy {sensor_type.label}"},
            )

        self.stdout.write(f"Washroom : {washroom}")
        self.stdout.write(f"API      : {options['base_url']}/api/v1/  (key '{DUMMY_CLIENT}' regenerated)\n")

        readings = [self.reading(t, washroom, options["scenario"]) for t in READING_TYPES]
        ok = self.post(options["base_url"], api_key, "readings/", readings)
        if ok:
            for item in ok["data"]:
                unit = "ppm" if item["type"] == SensorType.AMONIA else "%"
                self.stdout.write(
                    f"  {item['type']:<13} level {item['level']:>5}{unit:<4} battery {item['battery']:>3}%  "
                    f"-> {item['condition'] or '-'} ({item['severity'] or '-'})"
                )

        ratings = [
            {"device_id": self.device_id(SensorType.FEEDBACK, washroom), "rating": self.rating(options["scenario"])}
            for _ in range(options["ratings"])
        ]
        if ratings and (ok := self.post(options["base_url"], api_key, "customer-responses/", ratings)):
            values = [item["rating"] for item in ok["data"]]
            self.stdout.write(f"  feedback      {len(values)} ratings, average {sum(values) / len(values):.1f}")

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. Open {options['base_url']}/dashboard/?washroom={washroom.pk} to check the cards."
        ))
        self.stdout.write("Remove the dummy data later with: python manage.py send_dummy_data --cleanup")

    @staticmethod
    def device_id(sensor_type, washroom):
        return f"{DUMMY_PREFIX}{sensor_type.value.upper()}-{washroom.pk}"

    def reading(self, sensor_type, washroom, scenario):
        level_scenario = random.choice(list(SCENARIOS)) if scenario == "random" else scenario
        key = "trash" if sensor_type == SensorType.TRASH else "amonia" if sensor_type == SensorType.AMONIA else "dispenser"
        low, high = SCENARIOS[level_scenario][key]
        level = round(random.uniform(low, high), 1) if sensor_type == SensorType.AMONIA else random.randint(low, high)
        return {
            "device_id": self.device_id(sensor_type, washroom),
            "type": sensor_type.value,
            "battery": random.randint(20, 100),
            "level": level,
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
            return None
        return response.json()

    def cleanup(self):
        devices, _ = SensorDevice.objects.filter(id__startswith=DUMMY_PREFIX).delete()
        responses, _ = CustomerResponse.objects.filter(device_id__startswith=DUMMY_PREFIX).delete()
        clients, _ = ApiClient.objects.filter(name=DUMMY_CLIENT).delete()
        self.stdout.write(self.style.SUCCESS(
            f"Deleted dummy devices + readings ({devices} rows), {responses} ratings, {clients} test key."
        ))
