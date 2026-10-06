"""django-import-export resources for exporting logs from Admin (export only, no import)."""

import json

from django.utils import timezone
from import_export import fields, resources

from .models import EventLog, NotificationLog, SensorLog

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def format_time(value):
    return timezone.localtime(value).strftime(TIME_FORMAT) if value else ""


def format_json(value):
    return "" if value is None else json.dumps(value, ensure_ascii=False)


class LogResource(resources.ModelResource):
    """Times in Asia/Jakarta, device as its ID and JSON fields as full JSON text."""

    time = fields.Field(attribute="time", column_name="time")
    device = fields.Field(attribute="device_id", column_name="device")

    json_fields = ()

    def dehydrate_time(self, obj):
        return format_time(obj.time)

    def export_field(self, field, instance, **kwargs):
        if field.attribute in self.json_fields:
            return format_json(getattr(instance, field.attribute))
        return super().export_field(field, instance, **kwargs)


class EventLogResource(LogResource):
    class Meta:
        model = EventLog
        fields = ("id", "time", "device", "event_type", "recognition_target", "track_id", "height", "counted")
        export_order = fields


class SensorLogResource(LogResource):
    json_fields = ("response",)

    class Meta:
        model = SensorLog
        fields = ("id", "time", "status", "endpoint_url", "device", "response")
        export_order = fields


class NotificationLogResource(LogResource):
    json_fields = ("head", "body", "response")

    class Meta:
        model = NotificationLog
        fields = ("id", "time", "device", "endpoint_url", "response_status", "success", "wo_number", "head", "body",
                  "response")
        export_order = fields
