from django.apps import AppConfig


class WashroomConfig(AppConfig):
    name = "washroom"
    verbose_name = "Washroom API"

    def ready(self):
        # Registers the X-API-Key scheme with drf-spectacular.
        from . import authentication  # noqa: F401
