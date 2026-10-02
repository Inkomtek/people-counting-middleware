import uuid

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

    class Meta:
        verbose_name = "device"

    def __str__(self):
        return self.name or self.id

    @property
    def label(self):
        return self.name or self.id


class SchedulerConfig(models.Model):
    """Singleton row holding the sync job settings editable from Admin."""

    interval_minutes = models.PositiveIntegerField(default=1)
    enabled = models.BooleanField(default=True)

    class Meta:
        verbose_name = "scheduler config"

    def __str__(self):
        return f"Every {self.interval_minutes} minute(s)"

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

    def __str__(self):
        return f"{self.response_status} - {self.time}"


class DailyRecap(EventLog):
    """Proxy used only to show the daily recap page in Admin (no table of its own)."""

    class Meta:
        proxy = True
        verbose_name = "daily recap"
        verbose_name_plural = "daily recap"
