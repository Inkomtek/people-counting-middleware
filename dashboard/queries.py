"""Read-only queries behind the public dashboard pages."""

from datetime import datetime, time, timedelta

from django.db.models import Avg, Count, Q
from django.db.models.functions import ExtractHour, TruncDate, TruncMonth
from django.utils import timezone

from core.models import DeviceList, EventLog, NotificationLog, SensorLog
from core.services import COUNTED_EVENT_TYPES, COUNTED_RECOGNITION_TARGET
from washroom.models import SATISFACTION_TYPE, CustomerResponse, SensorReading

RECAP_DAYS = 30
RECAP_MONTHS = 12
RECAP_DAILY = "daily"
RECAP_MONTHLY = "monthly"
SUCCESS = Q(response_status__startswith="2")
# Same rule as core.services.is_countable, as a queryset filter.
COUNTED_EVENTS = Q(event_type__in=COUNTED_EVENT_TYPES, recognition_target=COUNTED_RECOGNITION_TARGET)


def day_bounds(day):
    start = timezone.make_aware(datetime.combine(day, time.min))
    return start, start + timedelta(days=1)


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
        "people_in": visitors_in(devices, day),
        "counters": counters(devices),
        "wo_sent": sent,
        "wo_success": success,
        "wo_failed": sent - success,
    }


def visitors_in(devices, day):
    start, end = day_bounds(day)
    return EventLog.objects.filter(device__in=devices, time__gte=start, time__lt=end).filter(COUNTED_EVENTS).count()


def last_successful_sync(devices):
    """Time of the newest successful ZK request for any of the devices, or None."""
    log = (SensorLog.objects.filter(device__in=devices, status=SensorLog.STATUS_ONLINE)
           .order_by("-time").first())
    return log.time if log else None


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
    busiest = min((hour for hour, n in counts.items() if n == peak), default=None) if peak else None
    return {
        "peak": peak,
        "busiest_hour": busiest,
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


def recap(devices, until, period=RECAP_DAILY):
    """People in and Work Orders sent per day (last RECAP_DAYS days) or per month (last RECAP_MONTHS
    months) up to `until`, newest first; only periods with activity are listed."""
    if period == RECAP_MONTHLY:
        first = until.replace(day=1)
        for _ in range(RECAP_MONTHS - 1):
            first = (first - timedelta(days=1)).replace(day=1)
        trunc = TruncMonth
    else:
        first = until - timedelta(days=RECAP_DAYS - 1)
        trunc = TruncDate
    since, end = day_bounds(first)[0], day_bounds(until)[1]
    tz = timezone.get_current_timezone()

    events = (
        EventLog.objects.filter(device__in=devices, time__gte=since, time__lt=end).filter(COUNTED_EVENTS)
        .annotate(period=trunc("time", tzinfo=tz)).values("period").annotate(n=Count("id"))
    )
    notifications = (
        NotificationLog.objects.filter(device__in=devices, time__gte=since, time__lt=end)
        .annotate(period=trunc("time", tzinfo=tz)).values("period").annotate(n=Count("id"))
    )
    rows = {}
    for row in events:
        rows.setdefault(_as_date(row["period"]), {"people_in": 0, "sent": 0})["people_in"] = row["n"]
    for row in notifications:
        rows.setdefault(_as_date(row["period"]), {"people_in": 0, "sent": 0})["sent"] = row["n"]
    return [{"period": key, **values} for key, values in sorted(rows.items(), reverse=True)]


def _as_date(value):
    # TruncMonth returns an aware datetime, TruncDate a date.
    return timezone.localtime(value).date() if hasattr(value, "hour") else value


STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"


def work_orders(devices, status="", time_from=None, time_to=None):
    logs = NotificationLog.objects.filter(device__in=devices).select_related("device").order_by("-time")
    if time_from:
        logs = logs.filter(time__gte=time_from)
    if time_to:
        logs = logs.filter(time__lte=time_to)
    if status == STATUS_SUCCESS:
        logs = logs.filter(SUCCESS)
    elif status == STATUS_FAILED:
        logs = logs.exclude(SUCCESS)
    return logs


def location_options():
    """Distinct buildings, floors and genders configured on devices, for the filter dropdowns."""
    devices = DeviceList.objects.exclude(building="")
    return {
        "buildings": sorted(set(devices.values_list("building", flat=True))),
        "floors": sorted(set(devices.exclude(floor="").values_list("floor", flat=True)), key=lambda f: (len(f), f)),
        "genders": [(value, label) for value, label in DeviceList.GENDER_CHOICES
                    if devices.filter(gender=value).exists()],
    }
