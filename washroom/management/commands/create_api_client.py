from django.core.management.base import BaseCommand, CommandError

from washroom.models import ApiClient


class Command(BaseCommand):
    help = "Create an API client (or give an existing one a new key) and print its X-API-Key once."

    def add_arguments(self, parser):
        parser.add_argument("name", help="Name of the external system, e.g. sensor-gateway")
        parser.add_argument(
            "--regenerate", action="store_true",
            help="Give an existing client a new key (its old key stops working)",
        )

    def handle(self, *args, **options):
        name = options["name"]
        client = ApiClient.objects.filter(name=name).first()
        if client and not options["regenerate"]:
            raise CommandError(f"API client '{name}' already exists. Use --regenerate to issue a new key.")
        if client is None:
            client = ApiClient(name=name)
        raw_key = client.set_new_key()
        client.is_active = True
        client.save()
        self.stdout.write(self.style.SUCCESS(f"API client '{name}' ready. X-API-Key (shown only once):"))
        self.stdout.write(raw_key)
