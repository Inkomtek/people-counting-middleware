from django.contrib import admin
from django.db.models import Count, Q
from django.db.models.functions import TruncDate
from django.template.response import TemplateResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import DailyRecap, DeviceList, Endpoint, EventLog, NotificationLog, SchedulerConfig, SensorLog

RECAP_DAYS = 30


class TimeRangeFilter(admin.ListFilter):
    """Sidebar filter with "from" / "to" datetime inputs on the `time` field (Asia/Jakarta)."""

    title = "time range"
    template = "admin/core/time_range_filter.html"
    parameter_from = "time_from"
    parameter_to = "time_to"

    def __init__(self, request, params, model, model_admin):
        super().__init__(request, params, model, model_admin)
        self.values = {}
        for name in (self.parameter_from, self.parameter_to):
            value = params.pop(name, None)
            # Django 5+ passes list values; keep the last one.
            if isinstance(value, list):
                value = value[-1]
            parsed = parse_datetime(value) if value else None
            if parsed is not None:
                self.values[name] = (value, timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed)

    def has_output(self):
        return True

    def expected_parameters(self):
        return [self.parameter_from, self.parameter_to]

    def queryset(self, request, queryset):
        if self.parameter_from in self.values:
            queryset = queryset.filter(time__gte=self.values[self.parameter_from][1])
        if self.parameter_to in self.values:
            queryset = queryset.filter(time__lte=self.values[self.parameter_to][1])
        return queryset

    def choices(self, changelist):
        # Rendered by the custom template; one dummy entry keeps the admin sidebar happy.
        return [{
            "from": self.values.get(self.parameter_from, ("",))[0],
            "to": self.values.get(self.parameter_to, ("",))[0],
            "other_params": {
                k: v for k, v in changelist.get_filters_params().items()
                if k not in self.expected_parameters()
            },
            "clear_url": changelist.get_query_string(remove=self.expected_parameters()),
        }]


@admin.register(Endpoint)
class EndpointAdmin(admin.ModelAdmin):
    list_display = ("id", "type", "url", "is_active")
    list_editable = ("is_active",)
    list_filter = ("type", "is_active")


@admin.register(DeviceList)
class DeviceListAdmin(admin.ModelAdmin):
    list_display = ("id", "type", "current_count", "maximum_trigger", "baseline_done")
    list_editable = ("maximum_trigger",)


@admin.register(SchedulerConfig)
class SchedulerConfigAdmin(admin.ModelAdmin):
    list_display = ("__str__", "interval_minutes", "enabled")

    def has_add_permission(self, request):
        return not SchedulerConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(EventLog)
class EventLogAdmin(ReadOnlyAdmin):
    list_display = ("id", "time", "device", "event_type", "recognition_target", "track_id", "height", "counted")
    list_filter = (TimeRangeFilter, "event_type", "recognition_target", "counted", "device")
    date_hierarchy = "time"
    search_fields = ("id", "track_id")
    ordering = ("-time",)


@admin.register(SensorLog)
class SensorLogAdmin(ReadOnlyAdmin):
    list_display = ("time", "status", "endpoint_url", "device")
    list_filter = ("status", "device")
    ordering = ("-time",)


@admin.register(NotificationLog)
class NotificationLogAdmin(ReadOnlyAdmin):
    list_display = ("time", "device", "endpoint_url", "response_status")
    list_filter = ("response_status", "device")
    ordering = ("-time",)


@admin.register(DailyRecap)
class DailyRecapAdmin(ReadOnlyAdmin):
    def has_delete_permission(self, request, obj=None):
        return False

    def changelist_view(self, request, extra_context=None):
        device_id = request.GET.get("device") or ""
        events = EventLog.objects.all()
        notifications = NotificationLog.objects.all()
        if device_id:
            events = events.filter(device_id=device_id)
            notifications = notifications.filter(device_id=device_id)

        # Dates are truncated in the project timezone (Asia/Jakarta).
        event_rows = (
            events.annotate(day=TruncDate("time")).values("day")
            .annotate(total=Count("id"), counted=Count("id", filter=Q(counted=True)))
        )
        notification_rows = (
            notifications.annotate(day=TruncDate("time")).values("day")
            .annotate(
                sent=Count("id"),
                success=Count("id", filter=Q(response_status__startswith="2")),
            )
        )
        days = {}
        for row in event_rows:
            days.setdefault(row["day"], {}).update(total=row["total"], counted=row["counted"])
        for row in notification_rows:
            days.setdefault(row["day"], {}).update(sent=row["sent"], success=row["success"])
        rows = [
            {"day": day, "total": 0, "counted": 0, "sent": 0, "success": 0, **values}
            for day, values in sorted(days.items(), reverse=True)[:RECAP_DAYS]
        ]
        for row in rows:
            row["failed"] = row["sent"] - row["success"]

        context = {
            **self.admin_site.each_context(request),
            "title": "Daily recap",
            "opts": self.model._meta,
            "rows": rows,
            "recap_days": RECAP_DAYS,
            "devices": DeviceList.objects.values_list("id", flat=True),
            "device_id": device_id,
            **(extra_context or {}),
        }
        return TemplateResponse(request, "admin/core/dailyrecap/change_list.html", context)
