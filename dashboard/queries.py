"""Read-only queries behind the public dashboard pages.

Every period-based query takes `start` and `end` as WIB dates (inclusive) so pages can show today,
the last 7/30 days or any custom range.
"""

from datetime import datetime, time, timedelta
from urllib.parse import urlparse

from django.db.models import Avg, Count, Q
from django.db.models.functions import ExtractHour, TruncDate, TruncMonth, TruncTime
from django.utils import timezone

from core.models import DeviceList, EventLog, NotificationLog, SensorLog
from core.services import COUNTED_EVENT_TYPES, COUNTED_RECOGNITION_TARGET
from washroom.models import SATISFACTION_TYPE, CustomerResponse, SensorReading

RECAP_DAILY = "daily"
RECAP_MONTHLY = "monthly"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
SUCCESS = Q(response_status__startswith="2")
# Same rule as core.services.is_countable, as a queryset filter.
COUNTED_EVENTS = Q(event_type__in=COUNTED_EVENT_TYPES, recognition_target=COUNTED_RECOGNITION_TARGET)
# The Event Log only shows people going in or out (passby, turnback, ... are hidden).
EVENT_LOG_TYPES = ["in", "out"]


def range_bounds(start, end):
    """Aware datetimes covering the WIB dates start..end inclusive."""
    since = timezone.make_aware(datetime.combine(start, time.min))
    until = timezone.make_aware(datetime.combine(end, time.min)) + timedelta(days=1)
    return since, until


def wo_number(log):
    response = log.response if isinstance(log.response, dict) else {}
    return response.get("wo_id") or "–"


def is_success(log):
    return log.response_status.startswith("2")


def destination(log):
    """Path part of the Work Order URL, e.g. /dummy/api_iot.php."""
    return urlparse(log.endpoint_url).path or log.endpoint_url


# ---------- sensors ----------

def sensor_statuses(devices):
    """Per device: ONLINE unless its latest ZK request failed, plus when it last synced successfully."""
    rows = []
    for device in devices:
        latest = SensorLog.objects.filter(device=device).order_by("-time").first()
        last_ok = (SensorLog.objects.filter(device=device, status=SensorLog.STATUS_ONLINE)
                   .order_by("-time").values_list("time", flat=True).first())
        trigger = max(device.maximum_trigger, 1)
        rows.append({
            "device": device,
            "online": latest is None or latest.status == SensorLog.STATUS_ONLINE,
            "last_sync": last_ok,
            "current_count": device.current_count,
            "maximum_trigger": device.maximum_trigger,
            "progress": min(round(device.current_count * 100 / trigger), 100),
            "remaining": max(device.maximum_trigger - device.current_count, 0),
        })
    return rows


def last_successful_sync(devices):
    """Time of the newest successful ZK request for any of the devices, or None."""
    log = (SensorLog.objects.filter(device__in=devices, status=SensorLog.STATUS_ONLINE)
           .order_by("-time").first())
    return log.time if log else None


# ---------- KPIs & chart ----------

def visitors_in(devices, start, end):
    since, until = range_bounds(start, end)
    return (EventLog.objects.filter(device__in=devices, time__gte=since, time__lt=until)
            .filter(COUNTED_EVENTS).count())


def kpis(devices, start, end):
    since, until = range_bounds(start, end)
    notifications = NotificationLog.objects.filter(device__in=devices, time__gte=since, time__lt=until)
    sent = notifications.count()
    success = notifications.filter(SUCCESS).count()
    return {
        "people_in": visitors_in(devices, start, end),
        "wo_sent": sent,
        "wo_success": success,
        "wo_failed": sent - success,
    }


def hourly(devices, start, end):
    """IN events per hour of day (0-23) summed over the range, and whether a Work Order fired then."""
    since, until = range_bounds(start, end)
    tz = timezone.get_current_timezone()
    counts = dict(
        EventLog.objects.filter(device__in=devices, time__gte=since, time__lt=until).filter(COUNTED_EVENTS)
        .annotate(hour=ExtractHour("time", tzinfo=tz)).values("hour").annotate(n=Count("id"))
        .values_list("hour", "n")
    )
    wo_hours = set(
        NotificationLog.objects.filter(device__in=devices, time__gte=since, time__lt=until)
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


# ---------- recap ----------

def recap(devices, start, end, period=RECAP_DAILY):
    """Per day or month in the range, newest first, only periods with activity:
    events received, visitors in, Work Orders sent / succeeded / failed. Also returns the totals."""
    since, until = range_bounds(start, end)
    tz = timezone.get_current_timezone()
    trunc = TruncMonth if period == RECAP_MONTHLY else TruncDate

    events = (
        EventLog.objects.filter(device__in=devices, time__gte=since, time__lt=until)
        .annotate(period=trunc("time", tzinfo=tz)).values("period")
        .annotate(total=Count("id"), people_in=Count("id", filter=COUNTED_EVENTS))
    )
    notifications = (
        NotificationLog.objects.filter(device__in=devices, time__gte=since, time__lt=until)
        .annotate(period=trunc("time", tzinfo=tz)).values("period")
        .annotate(sent=Count("id"), success=Count("id", filter=SUCCESS))
    )
    blank = {"total": 0, "people_in": 0, "sent": 0, "success": 0}
    rows = {}
    for row in events:
        rows.setdefault(_as_date(row["period"]), dict(blank)).update(total=row["total"], people_in=row["people_in"])
    for row in notifications:
        rows.setdefault(_as_date(row["period"]), dict(blank)).update(sent=row["sent"], success=row["success"])
    result = []
    for key, values in sorted(rows.items(), reverse=True):
        result.append({"period": key, **values, "failed": values["sent"] - values["success"]})
    totals = {field: sum(row[field] for row in result) for field in ("total", "people_in", "sent", "success", "failed")}
    return result, totals


def period_traffic(recap_rows, start, end, period=RECAP_DAILY):
    """Visitors in and notifications (Work Orders) sent per day (or month) for every period in the
    range, including empty ones, built from recap() rows. Both bars share one scale (the busiest
    value of either), so their heights compare honestly."""
    by_period = {row["period"]: row for row in recap_rows}
    periods = []
    if period == RECAP_MONTHLY:
        current = start.replace(day=1)
        while current <= end:
            periods.append(current)
            current = (current + timedelta(days=32)).replace(day=1)
    else:
        periods = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    counts = [(p, by_period.get(p, {}).get("people_in", 0), by_period.get(p, {}).get("sent", 0)) for p in periods]
    peak = max((n for _, n, _ in counts), default=0)
    busiest = next((p for p, n, _ in counts if n == peak), None) if peak else None
    scale = max([peak] + [sent for _, _, sent in counts])
    return {
        "peak": peak,
        "scale": scale,
        "busiest": busiest,
        "bars": [
            {
                "period": p,
                "count": n,
                "pct": round(n * 100 / scale) if scale else 0,
                "sent": sent,
                "sent_pct": round(sent * 100 / scale) if scale else 0,
            }
            for p, n, sent in counts
        ],
    }


def _as_date(value):
    # TruncMonth returns an aware datetime, TruncDate a date.
    return timezone.localtime(value).date() if hasattr(value, "hour") else value


# ---------- Work Orders ----------

def work_orders(devices, start, end, status="", query=""):
    since, until = range_bounds(start, end)
    logs = (NotificationLog.objects.filter(device__in=devices, time__gte=since, time__lt=until)
            .select_related("device").order_by("-time"))
    if status == STATUS_SUCCESS:
        logs = logs.filter(SUCCESS)
    elif status == STATUS_FAILED:
        logs = logs.exclude(SUCCESS)
    if query:
        logs = logs.filter(Q(response__wo_id__icontains=query) | Q(response_status__icontains=query))
    return logs


def work_order_row(log):
    return {"log": log, "wo_number": wo_number(log), "success": is_success(log), "destination": destination(log)}


# ---------- events ----------

def events(devices, start, end, time_from=None, time_to=None, event_type=""):
    """Event log in the range, newest first; time_from/time_to filter the time of day."""
    since, until = range_bounds(start, end)
    logs = (EventLog.objects.filter(device__in=devices, time__gte=since, time__lt=until,
                                    event_type__in=EVENT_LOG_TYPES)
            .select_related("device").order_by("-time", "-id"))
    if time_from or time_to:
        tz = timezone.get_current_timezone()
        logs = logs.annotate(local_time=TruncTime("time", tzinfo=tz))
        if time_from:
            logs = logs.filter(local_time__gte=time_from)
        if time_to:
            logs = logs.filter(local_time__lte=time_to)
    if event_type:
        logs = logs.filter(event_type=event_type)
    return logs


# ---------- other modules (washroom API data) ----------

def module_summary(module, device_filter, start, end):
    """Latest sensor reading, or the satisfaction average, for one module at one toilet in the range.
    `device_filter` selects the toilet's devices (see locations.resolve)."""
    devices = DeviceList.objects.filter(type=module, **device_filter)
    since, until = range_bounds(start, end)
    summary = {"has_data": False, "device_count": devices.count()}

    if module == SATISFACTION_TYPE:
        responses = CustomerResponse.objects.filter(device__in=devices, time__gte=since, time__lt=until)
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
        SensorReading.objects.filter(device__in=devices, time__gte=since, time__lt=until)
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
