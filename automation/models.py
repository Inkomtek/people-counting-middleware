"""Automation: where notifications go (Channel / Subchannel / Recipient) and when they are sent (Rule).

A Rule reacts to an event (people counter reached its threshold, a washroom reading, a rating, or the
scheduler's offline check) for the devices it targets, renders its message and delivers it to its
subchannels and/or to the staff on duty at the device's location (cleaners of its Scope and supervisors
of its Area, at the same time). Every attempt is logged
in AutomationLog; failed sends are retried per channel (never for Algospection, to avoid duplicate
Work Orders).
"""

from django.core.exceptions import ValidationError
from django.db import models

from core.models import Area, DeviceList, Scope


class Channel(models.Model):
    TYPE_ALGOSPECTION = "algospection"
    TYPE_TELEGRAM = "telegram"
    TYPE_WHATSAPP = "whatsapp"
    TYPE_EMAIL = "email"
    TYPE_WEBHOOK = "webhook"
    TYPE_CHOICES = [
        (TYPE_ALGOSPECTION, "Algospection (Work Order)"),
        (TYPE_TELEGRAM, "Telegram (Bot API)"),
        (TYPE_WHATSAPP, "WhatsApp (Evolution API)"),
        (TYPE_EMAIL, "Email (SMTP)"),
        (TYPE_WEBHOOK, "Webhook"),
    ]
    METHOD_CHOICES = [("POST", "POST"), ("PUT", "PUT"), ("GET", "GET")]

    name = models.CharField(max_length=100, unique=True)
    type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    is_active = models.BooleanField(default=True)
    config = models.JSONField(
        default=dict, blank=True,
        help_text=(
            "Non-secret settings plus the NAMES of .env variables, never the secret itself. "
            "Telegram: {\"token_env\": \"TELEGRAM_BOT_TOKEN\"} (optional \"base_url\"). "
            "WhatsApp: {\"base_url\": \"http://evolution:8080\", \"instance\": \"washroom\"}. "
            "Algospection / Webhook / Email: usually empty."
        ),
    )
    method = models.CharField(max_length=6, choices=METHOD_CHOICES, default="POST", help_text="HTTP method (webhook).")
    head = models.JSONField(default=dict, blank=True, help_text="Default HTTP headers (JSON object). Placeholders {{…}}.")
    body = models.JSONField(default=dict, blank=True, help_text="Default HTTP body (JSON object). Placeholders {{…}}.")
    email_subject = models.CharField(max_length=200, blank=True, help_text="Email only. Placeholders {{…}}.")
    email_body = models.TextField(blank=True, help_text="Email only. Placeholders {{…}}.")
    retry_count = models.PositiveSmallIntegerField(default=0, help_text="Extra attempts after a failed send (Algospection: always 0).")
    retry_delay_minutes = models.PositiveSmallIntegerField(default=5)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.get_type_display()})"

    def clean(self):
        if self.type == self.TYPE_ALGOSPECTION and self.retry_count:
            raise ValidationError({"retry_count": "Algospection is never retried, so Work Orders are not created twice."})
        for field in ("head", "body"):
            if not isinstance(getattr(self, field), dict):
                raise ValidationError({field: "Must be a JSON object, e.g. {\"key\": \"value\"}."})

    def save(self, *args, **kwargs):
        from .channels import DEFAULTS

        if self.type == self.TYPE_ALGOSPECTION:
            self.retry_count = 0
        default = DEFAULTS.get(self.type, {})
        if self._state.adding:
            for field in ("head", "body", "email_subject", "email_body", "config"):
                if not getattr(self, field) and default.get(field):
                    setattr(self, field, default[field])
        super().save(*args, **kwargs)


class Subchannel(models.Model):
    """One destination of a channel: a Telegram group, a WhatsApp group/number, email addresses, a URL."""

    channel = models.ForeignKey(Channel, on_delete=models.CASCADE, related_name="subchannels")
    name = models.CharField(max_length=100)
    target = models.CharField(
        max_length=500,
        help_text="Telegram: chat_id · WhatsApp: number (62812…) or group JID (…@g.us) · Email: addresses · "
                  "Webhook / Algospection: URL. Several numbers or addresses separated by commas.",
    )
    is_active = models.BooleanField(default=True)
    head = models.JSONField(null=True, blank=True, help_text="Empty = use the channel's headers.")
    body = models.JSONField(null=True, blank=True, help_text="Empty = use the channel's body.")
    email_subject = models.CharField(max_length=200, blank=True, help_text="Empty = use the channel's subject.")
    email_body = models.TextField(blank=True, help_text="Empty = use the channel's email body.")

    class Meta:
        ordering = ["channel__name", "name"]

    def __str__(self):
        return f"{self.channel.name} › {self.name}"

    def clean(self):
        for field in ("head", "body"):
            value = getattr(self, field)
            if value is not None and not isinstance(value, dict):
                raise ValidationError({field: "Must be a JSON object, or empty to follow the channel."})

    @property
    def overrides(self):
        return self.head is not None or self.body is not None or bool(self.email_subject or self.email_body)

    def effective(self):
        """Head/body/email of this destination: its own override, else the channel's."""
        channel = self.channel
        return {
            "method": channel.method,
            "head": self.head if self.head is not None else channel.head,
            "body": self.body if self.body is not None else channel.body,
            "email_subject": self.email_subject or channel.email_subject,
            "email_body": self.email_body or channel.email_body,
        }


class Recipient(models.Model):
    """A staff member. Assigned to Scopes = cleaner, to Areas = supervisor; both are told at the same time."""

    CONTACTS = ("whatsapp", "telegram", "email")

    name = models.CharField(max_length=100)
    is_active = models.BooleanField(default=True)
    telegram_chat_id = models.CharField(max_length=50, blank=True)
    whatsapp_number = models.CharField(max_length=50, blank=True, help_text="62812… (country code, no +).")
    email = models.EmailField(blank=True)
    contact_priority = models.JSONField(
        default=list, blank=True,
        help_text="Order to try, e.g. [\"whatsapp\", \"telegram\", \"email\"]. Empty = that order.",
    )
    scopes = models.ManyToManyField(Scope, blank=True, related_name="recipients", help_text="Cleaner of these toilets.")
    areas = models.ManyToManyField(Area, blank=True, related_name="recipients", help_text="Supervisor of these areas.")

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def clean(self):
        unknown = [c for c in self.contact_priority or [] if c not in self.CONTACTS]
        if unknown:
            raise ValidationError({"contact_priority": f"Unknown contact type(s): {', '.join(unknown)}."})

    def is_on_duty(self, at=None):
        # Shift schedules are not defined yet (2026-10-09): an active recipient is always on duty.
        return self.is_active

    def contacts(self):
        """[(kind, address)] for the contacts this person has, in their priority order."""
        address = {"whatsapp": self.whatsapp_number, "telegram": self.telegram_chat_id, "email": self.email}
        order = [c for c in (self.contact_priority or []) if c in self.CONTACTS]
        order += [c for c in self.CONTACTS if c not in order]
        return [(kind, address[kind].strip()) for kind in order if address[kind].strip()]


class Rule(models.Model):
    PEOPLE_THRESHOLD = "people_threshold"
    SENSOR_STATUS = "sensor_status"
    LOW_BATTERY = "low_battery"
    OFFLINE = "offline"
    BAD_RATING = "bad_rating"
    CONDITION_CHOICES = [
        (PEOPLE_THRESHOLD, "Pengunjung mencapai batas"),
        (SENSOR_STATUS, "Status sensor"),
        (LOW_BATTERY, "Baterai lemah"),
        (OFFLINE, "Sensor offline / tidak ada data"),
        (BAD_RATING, "Rating buruk"),
    ]
    # Events that happen once: sent every time (no "still a problem" state to wait for).
    ONE_SHOT = (PEOPLE_THRESHOLD, BAD_RATING)
    CONTACT_PRIORITY = "priority"
    CONTACT_ALL = "all"
    CONTACT_CHOICES = [
        (CONTACT_PRIORITY, "Satu kontak sesuai prioritas petugas (pindah ke berikutnya kalau gagal)"),
        (CONTACT_ALL, "Semua kontak yang dimiliki petugas"),
    ]

    name = models.CharField(max_length=150, unique=True)
    is_active = models.BooleanField(default=True)
    condition = models.CharField(max_length=30, choices=CONDITION_CHOICES)
    severities = models.JSONField(default=list, blank=True, help_text="Status sensor: e.g. [\"critical\"] or [\"critical\", \"warning\"].")
    conditions = models.JSONField(default=list, blank=True, help_text="Status sensor: status names, e.g. [\"Habis\", \"Penuh\"] (case-insensitive).")
    battery_below = models.PositiveSmallIntegerField(null=True, blank=True, help_text="Baterai lemah: below this %.")
    offline_minutes = models.PositiveIntegerField(default=60, help_text="Offline: no data for this many minutes.")

    devices = models.ManyToManyField(DeviceList, blank=True, related_name="automation_rules")
    areas = models.ManyToManyField(Area, blank=True, related_name="automation_rules")
    scopes = models.ManyToManyField(Scope, blank=True, related_name="automation_rules")
    device_types = models.JSONField(default=list, blank=True, help_text="e.g. [\"soap\", \"trash\"]. Empty = every type.")

    subchannels = models.ManyToManyField(Subchannel, blank=True, related_name="rules", help_text="Groups / addresses always notified.")
    notify_on_duty = models.BooleanField(default=False, help_text="Also notify staff on duty: cleaners of the Scope and supervisors of the Area, together.")
    contact_mode = models.CharField(max_length=10, choices=CONTACT_CHOICES, default=CONTACT_PRIORITY)
    person_channels = models.ManyToManyField(
        Channel, blank=True, related_name="person_rules",
        help_text="Channels used to reach staff (one Telegram, one WhatsApp, one Email channel).",
    )
    message_template = models.TextField(blank=True, help_text="Empty = the default text for the condition. Placeholders {{…}}.")
    cooldown_minutes = models.PositiveIntegerField(default=0, help_text="0 = once per occurrence (until it returns to normal).")

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def matching_devices(self):
        """Devices this rule watches: picked ones OR under its Areas/Scopes (all when none), by type."""
        qs = DeviceList.objects.all()
        picked = list(self.devices.values_list("pk", flat=True))
        areas = list(self.areas.values_list("pk", flat=True))
        scopes = list(self.scopes.values_list("pk", flat=True))
        if picked or areas or scopes:
            qs = qs.filter(models.Q(pk__in=picked) | models.Q(scope__in=scopes) | models.Q(scope__area__in=areas))
        types = [t for t in self.device_types or [] if t]
        if self.condition == self.PEOPLE_THRESHOLD:
            types = [DeviceList.TYPE_PEOPLE]
        elif self.condition == self.BAD_RATING:
            types = ["satisfaction"]
        if types:
            qs = qs.filter(type__in=types)
        return qs.distinct()

    def watches(self, device):
        return self.matching_devices().filter(pk=device.pk).exists()


class RuleState(models.Model):
    """Per rule and device: is the condition currently met, and when was it last sent."""

    rule = models.ForeignKey(Rule, on_delete=models.CASCADE, related_name="states")
    device = models.ForeignKey(DeviceList, on_delete=models.CASCADE, related_name="automation_states")
    active = models.BooleanField(default=False)
    last_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["rule", "device"], name="unique_rule_device_state")]


class AutomationLog(models.Model):
    STATUS_SUCCESS = "success"
    STATUS_FAILED = "failed"
    STATUS_RETRY = "retry_scheduled"
    STATUS_SKIPPED = "skipped"
    STATUS_CHOICES = [
        (STATUS_SUCCESS, "Berhasil"),
        (STATUS_FAILED, "Gagal"),
        (STATUS_RETRY, "Retry dijadwalkan"),
        (STATUS_SKIPPED, "Dilewati"),
    ]
    TIER_CHOICES = [
        ("subchannel", "Subchannel"),
        ("cleaner", "Cleaner (Scope)"),
        ("supervisor", "Supervisor (Area)"),
        ("test", "Pesan uji"),
    ]

    time = models.DateTimeField(auto_now_add=True, db_index=True)
    rule = models.ForeignKey(Rule, null=True, blank=True, on_delete=models.SET_NULL, related_name="logs")
    device = models.ForeignKey(DeviceList, null=True, blank=True, on_delete=models.CASCADE, related_name="automation_logs")
    channel = models.ForeignKey(Channel, null=True, blank=True, on_delete=models.SET_NULL, related_name="logs")
    subchannel = models.ForeignKey(Subchannel, null=True, blank=True, on_delete=models.SET_NULL, related_name="logs")
    recipient = models.ForeignKey(Recipient, null=True, blank=True, on_delete=models.SET_NULL, related_name="logs")
    channel_type = models.CharField(max_length=20, blank=True)
    tier = models.CharField(max_length=12, choices=TIER_CHOICES, default="subchannel")
    attempt = models.PositiveSmallIntegerField(default=1)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    next_retry_at = models.DateTimeField(null=True, blank=True)
    target = models.CharField(max_length=500, blank=True)
    message = models.TextField(blank=True)
    # What was sent, with {{placeholders}} still in place: secrets are never stored.
    request = models.JSONField(null=True, blank=True)
    # Event data, kept so a retry can rebuild the same request.
    context = models.JSONField(default=dict, blank=True)
    response = models.JSONField(null=True, blank=True)
    response_status = models.CharField(max_length=50, blank=True)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-time"]

    def __str__(self):
        return f"{self.time:%Y-%m-%d %H:%M} {self.channel_type} {self.status}"
