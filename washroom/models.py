import hashlib
import secrets

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


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
        # Lets DRF's IsAuthenticated-style checks treat an ApiClient like a logged-in principal.
        return True


class WashroomConfig(models.Model):
    """Singleton row with dashboard settings editable from Admin."""

    offline_after_minutes = models.PositiveIntegerField(
        default=30, help_text="A device with no reading for longer than this is shown as Offline."
    )

    class Meta:
        verbose_name = "washroom config"

    def __str__(self):
        return f"Offline after {self.offline_after_minutes} minute(s)"

    @classmethod
    def get(cls):
        return cls.objects.get_or_create(pk=1)[0]


class SensorType(models.TextChoices):
    AMONIA = "amonia", "Amonia"
    SOAP = "soap", "Soap"
    TISSUE = "tissue", "Tissue Roll"
    TOILET_PAPER = "toilet_paper", "Toilet Paper"
    TRASH = "trash", "Trash Bin"
    # Customer rating button; registered automatically by POST /customer-responses/.
    FEEDBACK = "feedback", "Customer Satisfaction"


# Types that send level readings (everything except the feedback button), in dashboard order.
READING_TYPES = [SensorType.SOAP, SensorType.TOILET_PAPER, SensorType.TISSUE, SensorType.TRASH, SensorType.AMONIA]


class Gender(models.TextChoices):
    PRIA = "pria", "Pria"
    WANITA = "wanita", "Wanita"
    DIFABEL = "difabel", "Difabel"


class Washroom(models.Model):
    """One toilet area shown as one dashboard: building + floor + gender."""

    building = models.CharField(max_length=100)
    floor = models.CharField(max_length=50, help_text='Label shown in the filter, e.g. "Lantai 2"')
    gender = models.CharField(max_length=10, choices=Gender.choices)
    # ZK people counters (core.DeviceList) installed at this washroom's entrance.
    people_counters = models.ManyToManyField("core.DeviceList", blank=True, related_name="washrooms")

    class Meta:
        ordering = ("building", "floor", "gender")
        constraints = [
            models.UniqueConstraint(fields=("building", "floor", "gender"), name="unique_washroom"),
        ]

    def __str__(self):
        return f"{self.building} - {self.floor} - {self.get_gender_display()}"


class Severity(models.TextChoices):
    NORMAL = "normal", "Normal (green)"
    WARNING = "warning", "Warning (orange)"
    CRITICAL = "critical", "Critical (red)"


class StatusRule(models.Model):
    """Maps a sensor level to a condition label: matches when min_level <= level < max_level."""

    sensor_type = models.CharField(max_length=20, choices=SensorType.choices)
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


class SensorDevice(models.Model):
    id = models.CharField(primary_key=True, max_length=100, help_text="Device serial, e.g. 588C819FAF3E")
    type = models.CharField(max_length=20, choices=SensorType.choices)
    name = models.CharField(max_length=100, blank=True)
    location = models.CharField(max_length=255, blank=True)
    # Assigned in Admin; unassigned devices are not shown on any dashboard.
    washroom = models.ForeignKey(
        Washroom, on_delete=models.SET_NULL, null=True, blank=True, related_name="devices"
    )
    # Latest reading, denormalized so the dashboard needs one query.
    last_seen = models.DateTimeField(null=True, blank=True)
    last_battery = models.IntegerField(null=True, blank=True)
    last_level = models.FloatField(null=True, blank=True)
    last_condition = models.CharField(max_length=50, blank=True)
    last_severity = models.CharField(max_length=10, choices=Severity.choices, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "sensor device"

    def __str__(self):
        return f"{self.name or self.get_type_display()} ({self.id})"


class SensorReading(models.Model):
    device = models.ForeignKey(SensorDevice, on_delete=models.CASCADE, related_name="readings")
    time = models.DateTimeField(db_index=True)
    battery = models.IntegerField(
        null=True, blank=True, validators=[MinValueValidator(0), MaxValueValidator(100)]
    )
    # Fill level in % for soap/tissue/toilet_paper/trash, ammonia concentration (ppm) for amonia.
    level = models.FloatField(null=True, blank=True)
    condition = models.CharField(max_length=50, blank=True)
    severity = models.CharField(max_length=10, choices=Severity.choices, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    # Kept when the client is deleted, so revoking a key never removes data.
    client = models.ForeignKey(ApiClient, on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return f"{self.device_id} - {self.time}"


class CustomerResponse(models.Model):
    """One press on a customer feedback (rating) device."""

    device_id = models.CharField(max_length=100, help_text="Feedback device serial")
    location = models.CharField(max_length=255, blank=True)
    time = models.DateTimeField(db_index=True)
    rating = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(5)])
    comment = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True)
    received_at = models.DateTimeField(auto_now_add=True)
    # Kept when the client is deleted, so revoking a key never removes data.
    client = models.ForeignKey(ApiClient, on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return f"{self.rating} - {self.time}"
