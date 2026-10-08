import uuid

from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


def generate_id():
    return uuid.uuid4().hex


class Endpoint(models.Model):
    TYPE_TOKEN = "token"
    TYPE_EVENT = "event"
    TYPE_NOTIFICATION = "notification"
    TYPE_CHOICES = [
        (TYPE_TOKEN, "ZK token"),
        (TYPE_EVENT, "ZK event"),
        (TYPE_NOTIFICATION, "Work Order notification"),
    ]

    id = models.CharField(primary_key=True, max_length=100)
    type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    url = models.URLField(max_length=200)
    head = models.JSONField(default=dict, blank=True)
    body = models.JSONField(default=dict, blank=True)
    # Only active notification endpoints receive Work Order POSTs.
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.id} ({self.type})"


# ---------- Location hierarchy: Client + Region -> Site -> Area -> Scope (one Scope = one toilet) ----------

class Client(models.Model):
    name = models.CharField(max_length=200, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Region(models.Model):
    """Shared by every client, e.g. Jakarta."""

    name = models.CharField(max_length=200, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Site(models.Model):
    client = models.ForeignKey(Client, on_delete=models.CASCADE, related_name="sites")
    region = models.ForeignKey(Region, on_delete=models.CASCADE, related_name="sites")
    name = models.CharField(max_length=200)

    class Meta:
        ordering = ["client__name", "region__name", "name"]
        constraints = [models.UniqueConstraint(fields=["client", "region", "name"], name="unique_site")]

    def __str__(self):
        return f"{self.client} · {self.region} · {self.name}"


class Area(models.Model):
    site = models.ForeignKey(Site, on_delete=models.CASCADE, related_name="areas")
    name = models.CharField(max_length=200)

    class Meta:
        ordering = ["site__client__name", "site__region__name", "site__name", "name"]
        constraints = [models.UniqueConstraint(fields=["site", "name"], name="unique_area")]

    def __str__(self):
        return f"{self.site} · {self.name}"


class Scope(models.Model):
    area = models.ForeignKey(Area, on_delete=models.CASCADE, related_name="scopes")
    name = models.CharField(max_length=200)

    class Meta:
        ordering = ["area__site__client__name", "area__site__region__name", "area__site__name", "area__name", "name"]
        constraints = [models.UniqueConstraint(fields=["area", "name"], name="unique_scope")]

    def __str__(self):
        return f"{self.area} · {self.name}"


class DeviceList(models.Model):
    # Device type = the dashboard module the device belongs to. Only TYPE_PEOPLE is synced from ZK so far.
    TYPE_PEOPLE = "people"
    TYPE_CHOICES = [
        (TYPE_PEOPLE, "People Counting"),
        ("satisfaction", "Customer Satisfaction"),
        ("soap", "Soap"),
        ("toilet-paper", "Toilet Paper"),
        ("tissue", "Tissue Roll"),
        ("trash", "Trash Bin"),
        ("ammonia", "Amonia"),
    ]
    GENDER_MALE = "male"
    GENDER_FEMALE = "female"
    GENDER_CHOICES = [(GENDER_MALE, "Pria"), (GENDER_FEMALE, "Wanita")]

    id = models.CharField(primary_key=True, max_length=100)
    type = models.CharField(max_length=100, choices=TYPE_CHOICES, default=TYPE_PEOPLE)
    # Readable label for the dashboard's device dropdown, e.g. "Pintu Utama"; falls back to the id.
    name = models.CharField(max_length=100, blank=True)
    current_count = models.IntegerField(default=0)
    maximum_trigger = models.IntegerField(default=10)
    # False until the first sync stores existing ZK events as a baseline without counting them.
    baseline_done = models.BooleanField(default=False)
    # WIB date (from event time) that current_count belongs to; a counted event on a newer date resets it.
    count_date = models.DateField(null=True, blank=True)
    # Location of the toilet this sensor covers (one device = one toilet); used by the dashboard filters.
    building = models.CharField(max_length=200, blank=True)
    floor = models.CharField(max_length=50, blank=True)
    gender = models.CharField(max_length=10, choices=GENDER_CHOICES, blank=True)
    # The toilet in the client/region/site/area/scope hierarchy; the dashboard filters by it.
    # SET_NULL (not CASCADE): deleting a location must never delete a device and its history.
    scope = models.ForeignKey(Scope, on_delete=models.SET_NULL, null=True, blank=True, related_name="devices")

    class Meta:
        verbose_name = "device"

    def __str__(self):
        return self.name or self.id

    @property
    def label(self):
        return self.name or self.id


class SchedulerConfig(models.Model):
    """Singleton row holding the sync job settings editable from Admin."""

    interval_seconds = models.PositiveIntegerField(
        default=60,
        validators=[MinValueValidator(10), MaxValueValidator(86400)],
        help_text="How often the scheduler pulls new events from ZK (10-86400 seconds).",
    )
    enabled = models.BooleanField(default=True)
    dashboard_refresh_seconds = models.PositiveIntegerField(
        default=60,
        validators=[MinValueValidator(10), MaxValueValidator(3600)],
        help_text="How often open dashboard pages reload their data (10-3600 seconds).",
    )

    class Meta:
        verbose_name = "scheduler config"

    def __str__(self):
        return f"Every {self.interval_seconds} second(s)"

    @classmethod
    def get(cls):
        return cls.objects.get_or_create(pk=1)[0]


class EventLog(models.Model):
    id = models.CharField(primary_key=True, max_length=100)
    metadata_id = models.CharField(max_length=255, blank=True)
    time = models.DateTimeField()
    track_id = models.CharField(max_length=255, blank=True)
    event_type = models.CharField(max_length=100, blank=True)
    recognition_target = models.CharField(max_length=100, blank=True)
    height = models.IntegerField(null=True, blank=True)
    device = models.ForeignKey(DeviceList, on_delete=models.CASCADE)
    # Whether this event was added to current_count (False for baseline and ignored event types).
    counted = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.event_type} - {self.time}"


class SensorLog(models.Model):
    """Log of every request made to the ZK API (token and event fetch)."""

    STATUS_ONLINE = "ONLINE"
    STATUS_OFFLINE = "OFFLINE"

    id = models.CharField(primary_key=True, max_length=100, default=generate_id, editable=False)
    status = models.CharField(max_length=100)
    response = models.JSONField(null=True, blank=True)
    time = models.DateTimeField(auto_now_add=True)
    endpoint_url = models.CharField(max_length=500)
    # Null for token requests, which are not tied to a device.
    device = models.ForeignKey(DeviceList, on_delete=models.CASCADE, null=True, blank=True)

    def __str__(self):
        return f"{self.status} - {self.time}"


def work_order_outcome(response_status, response):
    """(succeeded, Work Order number) from a Work Order POST's HTTP status and JSON reply.

    Algospection (and the dummy, which mirrors it) replies `[{"error": 0, "results": [{"WO_NO": ...}]}]`;
    a 2xx with a non-zero "error" is a failure. Older dummy replies `{"status": "success", "wo_id": ...}`
    are still understood so existing logs keep their numbers; anything else falls back to the HTTP status.
    """
    http_ok = str(response_status).startswith("2")
    item = response[0] if isinstance(response, list) and response and isinstance(response[0], dict) else response
    if not isinstance(item, dict):
        return http_ok, ""  # no recognisable body: fall back to the HTTP status
    if "error" in item:
        results = item.get("results") or [{}]
        number = results[0].get("WO_NO") if isinstance(results[0], dict) else None
        return http_ok and item.get("error") in (0, "0"), str(number or "")
    return http_ok and item.get("status", "success") == "success", str(item.get("wo_id") or "")


class NotificationLog(models.Model):
    """Log of every Work Order POST."""

    id = models.CharField(primary_key=True, max_length=100, default=generate_id, editable=False)
    time = models.DateTimeField(auto_now_add=True)
    device = models.ForeignKey(DeviceList, on_delete=models.CASCADE)
    endpoint_url = models.CharField(max_length=500)
    body = models.JSONField(null=True, blank=True)
    head = models.JSONField(null=True, blank=True)
    response = models.JSONField(null=True, blank=True)
    response_status = models.CharField(max_length=50)
    # Derived from response_status + response on every save (see work_order_outcome). The database
    # defaults let a scheduler still running older code insert rows during a deploy instead of crashing.
    success = models.BooleanField(default=False, db_default=False, editable=False)
    wo_number = models.CharField(max_length=100, blank=True, default="", db_default="", editable=False)
    # When the Work Order was finished in Algospection (null = not finished yet). Algospection does not send
    # this yet; on dev it is filled by `simulate_wo_completion`.
    completed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.response_status} - {self.time}"

    def save(self, *args, **kwargs):
        self.success, self.wo_number = work_order_outcome(self.response_status, self.response)
        super().save(*args, **kwargs)

    @property
    def completion_minutes(self):
        """Minutes from the notification to the finished Work Order, or None while it is open."""
        if self.completed_at is None:
            return None
        return max(round((self.completed_at - self.time).total_seconds() / 60), 0)


class DailyRecap(EventLog):
    """Proxy used only to show the daily recap page in Admin (no table of its own)."""

    class Meta:
        proxy = True
        verbose_name = "daily recap"
        verbose_name_plural = "daily recap"
