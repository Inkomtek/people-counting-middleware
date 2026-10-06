import hashlib
import secrets

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from core.models import DeviceList

# DeviceList.type values (defined by the dashboard app) that send level readings through this API.
READING_TYPES = ["soap", "toilet-paper", "tissue", "trash", "ammonia"]
# DeviceList.type of the customer rating button.
SATISFACTION_TYPE = "satisfaction"
# Ammonia level is a concentration in ppm; every other reading type is a fill level in %.
PPM_TYPES = {"ammonia"}


def hash_key(raw_key):
    return hashlib.sha256(raw_key.encode()).hexdigest()


class ApiClient(models.Model):
    """An external system allowed to call the washroom API with an X-API-Key header."""

    name = models.CharField(max_length=100, unique=True)
    # Only the SHA-256 hash is stored; the raw key is shown once in Admin when the client is created.
    key_hash = models.CharField(max_length=64, unique=True, editable=False)
    key_prefix = models.CharField(max_length=8, editable=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = "API client"

    def __str__(self):
        return self.name

    def set_new_key(self):
        """Generate a new key, store its hash and return the raw key (not retrievable later)."""
        raw_key = secrets.token_urlsafe(32)
        self.key_hash = hash_key(raw_key)
        self.key_prefix = raw_key[:8]
        return raw_key

    @property
    def is_authenticated(self):
        # Lets DRF's IsAuthenticated check treat an ApiClient like a logged-in principal.
        return True


class Severity(models.TextChoices):
    NORMAL = "normal", "Normal (green)"
    WARNING = "warning", "Warning (orange)"
    CRITICAL = "critical", "Critical (red)"


class StatusRule(models.Model):
    """Maps a sensor level to a condition label: matches when min_level <= level < max_level."""

    sensor_type = models.CharField(
        max_length=20, choices=[c for c in DeviceList.TYPE_CHOICES if c[0] in READING_TYPES]
    )
    condition = models.CharField(max_length=50)
    severity = models.CharField(max_length=10, choices=Severity.choices)
    min_level = models.FloatField(null=True, blank=True, help_text="Inclusive. Empty = no lower bound.")
    max_level = models.FloatField(null=True, blank=True, help_text="Exclusive. Empty = no upper bound.")

    class Meta:
        ordering = ("sensor_type", "min_level")

    def __str__(self):
        return f"{self.get_sensor_type_display()}: {self.condition}"

    def matches(self, level):
        return (self.min_level is None or level >= self.min_level) and (
            self.max_level is None or level < self.max_level
        )


class SensorReading(models.Model):
    device = models.ForeignKey(DeviceList, on_delete=models.CASCADE, related_name="sensor_readings")
    # The sender's own data id ("id" in the payload); unique per device, so a resent reading is skipped.
    external_id = models.CharField(max_length=100, blank=True)
    # "inputDate" in the payload.
    time = models.DateTimeField(db_index=True)
    last_online = models.DateTimeField(null=True, blank=True)
    battery = models.IntegerField(
        null=True, blank=True, validators=[MinValueValidator(0), MaxValueValidator(100)]
    )
    # "value" in the payload, stored as sent: % for soap, toilet-paper, tissue, trash; ppm for ammonia.
    level = models.FloatField(null=True, blank=True)
    # "status" sent by the sensor side (e.g. "Terisi"); severity comes from a StatusRule with that name.
    condition = models.CharField(max_length=50, blank=True)
    severity = models.CharField(max_length=10, choices=Severity.choices, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    # Kept when the client is deleted, so revoking a key never removes data.
    client = models.ForeignKey(ApiClient, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["device", "-time"])]
        constraints = [
            models.UniqueConstraint(
                fields=["device", "external_id"], condition=~models.Q(external_id=""),
                name="unique_reading_external_id_per_device",
            ),
        ]

    def __str__(self):
        return f"{self.device_id} - {self.time}"


class CustomerResponse(models.Model):
    """One press on a customer satisfaction (rating) device."""

    device = models.ForeignKey(DeviceList, on_delete=models.CASCADE, related_name="customer_responses")
    time = models.DateTimeField(db_index=True)
    rating = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    comment = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    client = models.ForeignKey(ApiClient, on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return f"{self.rating} - {self.time}"
