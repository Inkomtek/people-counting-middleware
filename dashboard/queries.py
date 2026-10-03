"""Read-only queries behind the public dashboard pages."""

import json
from datetime import datetime, time, timedelta

from django.db.models import Avg, Count, Q
from django.db.models.functions import ExtractHour, TruncDate
from django.utils import timezone

from core.models import DeviceList, EventLog, NotificationLog
from core.services import COUNTED_EVENT_TYPES, COUNTED_RECOGNITION_TARGET
from washroom.models import SATISFACTION_TYPE, CustomerResponse, SensorReading

RECAP_DAYS = 30
SUCCESS = Q(response_status__startswith="2")
# Same rule as core.services.is_countable, as a queryset filter.
COUNTED_EVENTS = Q(event_type__in=COUNTED_EVENT_TYPES, recognition_target=COUNTED_RECOGNITION_TARGET)
SECRET_KEYS = {"token", "client_secret", "access_token"}


def day_bounds(day):
    start = timezone.make_aware(datetime.combine(day, time.min))
    return start, start + timedelta(days=1)


def mask_secrets(value):
    """Return a copy of a JSON value with token-like fields shortened to their first 7 characters."""
    if isinstance(value, dict):
        return {
            key: (str(val)[:7] + "•••••" if key in SECRET_KEYS and val else mask_secrets(val))
            for key, val in value.items()
        }
    if isinstance(value, list):
        return [mask_secrets(item) for item in value]
    return value


def wo_number(log):
    response = log.response if isinstance(log.response, dict) else {}
    return response.get("wo_id") or "–"


def is_success(log):
    return log.response_status.startswith("2")


def counters(devices):
    """current_count / maximum_trigger per device: each device has its own Work Order threshold."""
    rows = []
    for device in devices:
        trigger = max(device.maximum_trigger, 1)
        rows.append({
            "label": device.label,
            "current_count": device.current_count,
            "maximum_trigger": device.maximum_trigger,
            "progress": min(round(device.current_count * 100 / trigger), 100),
            "remaining": max(device.maximum_trigger - device.current_count, 0),
        })
    return rows


def kpis(devices, day):
    start, end = day_bounds(day)
    notifications = NotificationLog.objects.filter(device__in=devices, time__gte=start, time__lt=end)
    sent = notifications.count()
    success = notifications.filter(SUCCESS).count()
    return {
        "people_in": EventLog.objects.filter(device__in=devices, time__gte=start, time__lt=end)
        .filter(COUNTED_EVENTS).count(),
        "counters": counters(devices),
        "wo_sent": sent,
        "wo_success": success,
        "wo_failed": sent - success,
    }


def module_summary(module, toilet, day):
    """Latest sensor reading or satisfaction average for one module at one toilet and date."""
    devices = DeviceList.objects.filter(
        type=module,
        building=toilet["building"],
        floor=toilet["floor"],
        gender=toilet["gender"],
    )
    start, end = day_bounds(day)
    summary = {"has_data": False, "device_count": devices.count()}

    if module == SATISFACTION_TYPE:
        responses = CustomerResponse.objects.filter(device__in=devices, time__gte=start, time__lt=end)
        stats = responses.aggregate(average=Avg("rating"), count=Count("id"))
        if stats["count"]:
            latest = responses.select_related("device").order_by("-time", "-id").first()
            summary.update(
                has_data=True,
                average_rating=stats["average"],
                rating_progress=round(stats["average"] * 20),
                response_count=stats["count"],
                latest=latest,
            )
        return summary

    latest = (
        SensorReading.objects.filter(device__in=devices, time__gte=start, time__lt=end)
        .select_related("device").order_by("-time", "-id").first()
    )
    if latest:
        summary.update(
            has_data=True,
            latest=latest,
            level_progress=(max(0, min(round(latest.level), 100))
                            if latest.level is not None and module != "ammonia" else None),
        )
    return summary


def hourly(devices, day):
    """IN events per hour (0-23) and whether a Work Order fired in that hour."""
    start, end = day_bounds(day)
    tz = timezone.get_current_timezone()
    counts = dict(
        EventLog.objects.filter(device__in=devices, time__gte=start, time__lt=end).filter(COUNTED_EVENTS)
        .annotate(hour=ExtractHour("time", tzinfo=tz)).values("hour").annotate(n=Count("id"))
        .values_list("hour", "n")
    )
    wo_hours = set(
        NotificationLog.objects.filter(device__in=devices, time__gte=start, time__lt=end)
        .annotate(hour=ExtractHour("time", tzinfo=tz)).values_list("hour", flat=True)
    )
    peak = max(counts.values(), default=0)
    return {
        "peak": peak,
        "bars": [
            {
                "hour": hour,
                "count": counts.get(hour, 0),
                "pct": round(counts.get(hour, 0) * 100 / peak) if peak else 0,
                "wo": hour in wo_hours,
            }
            for hour in range(24)
        ],
    }


def daily_recap(devices, until):
    """One row per day with activity, newest first, for the RECAP_DAYS days up to `until`."""
    since, end = day_bounds(until - timedelta(days=RECAP_DAYS - 1))[0], day_bounds(until)[1]
    events = (
        EventLog.objects.filter(device__in=devices, time__gte=since, time__lt=end)
        .annotate(day=TruncDate("time")).values("day")
        .annotate(total=Count("id"), people_in=Count("id", filter=COUNTED_EVENTS))
    )
    notifications = (
        NotificationLog.objects.filter(device__in=devices, time__gte=since, time__lt=end)
        .annotate(day=TruncDate("time")).values("day")
        .annotate(sent=Count("id"), success=Count("id", filter=SUCCESS))
    )
    days = {}
    for row in events:
        days.setdefault(row["day"], {}).update(total=row["total"], people_in=row["people_in"])
    for row in notifications:
        days.setdefault(row["day"], {}).update(sent=row["sent"], success=row["success"])
    rows = []
    for day, values in sorted(days.items(), reverse=True):
        row = {"day": day, "total": 0, "people_in": 0, "sent": 0, "success": 0, **values}
        row["failed"] = row["sent"] - row["success"]
        rows.append(row)
    return rows


def work_orders(devices, query="", time_from=None, time_to=None):
    logs = NotificationLog.objects.filter(device__in=devices).select_related("device").order_by("-time")
    if time_from:
        logs = logs.filter(time__gte=time_from)
    if time_to:
        logs = logs.filter(time__lte=time_to)
    if query:
        logs = logs.filter(Q(response__wo_id__icontains=query) | Q(response_status__icontains=query))
    return logs


def work_order_detail(log):
    return json.dumps({
        "wo": wo_number(log),
        "time": timezone.localtime(log.time).strftime("%d %b %Y %H:%M:%S WIB"),
        "device": f"{log.device.label} ({log.device_id})" if log.device.name else log.device_id,
        "endpoint": log.endpoint_url,
        "status": log.response_status,
        "success": is_success(log),
        "request": mask_secrets(log.body),
        "response": mask_secrets(log.response),
    }, ensure_ascii=False)


def location_options():
    """Distinct buildings, floors and genders configured on devices, for the filter dropdowns."""
    devices = DeviceList.objects.exclude(building="")
    return {
        "buildings": sorted(set(devices.values_list("building", flat=True))),
        "floors": sorted(set(devices.exclude(floor="").values_list("floor", flat=True)), key=lambda f: (len(f), f)),
        "genders": [(value, label) for value, label in DeviceList.GENDER_CHOICES
                    if devices.filter(gender=value).exists()],
    }
