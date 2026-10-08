"""Read-only queries behind the public dashboard pages.

Every period-based query takes `start` and `end` as WIB dates (inclusive) so pages can show today,
the last 7/30 days or any custom range.
"""

from datetime import datetime, time, timedelta
from urllib.parse import urlparse

from django.db.models import Avg, Count, Q
from django.db.models.functions import ExtractHour, TruncDate, TruncMonth, TruncTime
from django.utils import timezone

from core.models import Area, Client, DeviceList, EventLog, NotificationLog, Scope, SensorLog, Site
from core.services import COUNTED_EVENT_TYPES, COUNTED_RECOGNITION_TARGET
from washroom.models import SATISFACTION_TYPE, CustomerResponse, SensorReading, StatusRule

RECAP_DAILY = "daily"
RECAP_MONTHLY = "monthly"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
# NotificationLog.success already combines the HTTP status and Algospection's "error" flag.
SUCCESS = Q(success=True)
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
    return log.wo_number or "–"


def is_success(log):
    return log.success


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


NEAR_THRESHOLD = 80  # % of maximum_trigger: "hampir batas"
SENSOR_SORTS = ("attention", "name", "count", "visitors", "notifications")
DONUT_R = 15.915  # circumference 100


def sensor_table(devices, start, end, status="", query="", sort="attention"):
    """People counting sensors as table rows: sensor_statuses plus location, visitors in and
    notifications sent / failed in the range. Filtered by `status` (online / offline / near = at least
    NEAR_THRESHOLD % of the trigger) and `query` (name or ID), sorted by `sort` (SENSOR_SORTS;
    "attention" = offline first, then closest to the trigger)."""
    since, until = range_bounds(start, end)
    visitors = dict(EventLog.objects.filter(device__in=devices, time__gte=since, time__lt=until)
                    .filter(COUNTED_EVENTS).values("device").annotate(n=Count("id")).values_list("device", "n"))
    sent = {row["device"]: row for row in NotificationLog.objects.filter(device__in=devices, time__gte=since,
                                                                       time__lt=until)
            .values("device").annotate(n=Count("id"), ok=Count("id", filter=SUCCESS))}
    rows = []
    for row in sensor_statuses(devices):
        device = row["device"]
        notified = sent.get(device.id, {"n": 0, "ok": 0})
        row.update(location=_location_label(device), visitors=visitors.get(device.id, 0),
                   notifications=notified["n"], failed=notified["n"] - notified["ok"],
                   near=row["progress"] >= NEAR_THRESHOLD)
        rows.append(row)
    if status == "online":
        rows = [r for r in rows if r["online"]]
    elif status == "offline":
        rows = [r for r in rows if not r["online"]]
    elif status == "near":
        rows = [r for r in rows if r["near"]]
    if query:
        needle = query.lower()
        rows = [r for r in rows if needle in r["device"].id.lower() or needle in (r["device"].name or "").lower()]
    keys = {
        "attention": lambda r: (r["online"], -r["progress"], r["device"].label),
        "name": lambda r: r["device"].label.lower(),
        "count": lambda r: (-r["current_count"], r["device"].label),
        "visitors": lambda r: (-r["visitors"], r["device"].label),
        "notifications": lambda r: (-r["notifications"], r["device"].label),
    }
    return sorted(rows, key=keys.get(sort, keys["attention"]))


def online_donut(online, total):
    """Ring segments (circumference 100, from 12 o'clock) for online (green) / offline (red) sensors."""
    if not total:
        return []
    share = online * 100 / total
    segments = []
    for kind, part, offset in (("online", share, 25), ("offline", 100 - share, 25 - share)):
        if part:
            length = max(part - (0.6 if part < 100 else 0), 0.5)
            segments.append({"kind": kind, "dash": f"{length:.2f} {100 - length:.2f}", "offset": f"{offset:.2f}"})
    return segments


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


CHART_TOP = 88
CHART_TICKS = 4


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
        # Axis labels split evenly from 0 to the peak, placed where the bars put those values
        # (bars use at most CHART_TOP % of the height so the notification dot fits above the tallest).
        "ticks": [
            # pct as a string: templates localize floats ("22,0" in Indonesian), which is invalid CSS.
            {"value": round(peak * i / CHART_TICKS), "pct": f"{CHART_TOP * i / CHART_TICKS:g}"}
            for i in range(CHART_TICKS + 1)
        ] if peak else [],
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


TIMELINE_LABEL_MIN_PCT = 4.5  # gaps narrower than this (% of a day) only show their length on hover


def notification_timeline(devices, start, end):
    """Notification traffic as one 00-24 timeline per WIB day (newest first, only days with
    notifications): each notification is a dot at its time of day, and consecutive dots on the same day
    are joined by a segment labelled with the gap. All devices are merged and failed notifications are
    included. Stats (average, fastest, slowest) cover the gaps within a day; the first notification of a
    day has no gap, since the count resets daily."""
    since, until = range_bounds(start, end)
    logs = NotificationLog.objects.filter(device__in=devices, time__gte=since, time__lt=until).order_by("time")
    days, gaps = {}, []
    for log in logs.only("time", "success"):
        moment = timezone.localtime(log.time)
        dots = days.setdefault(moment.date(), [])
        minute = moment.hour * 60 + moment.minute + moment.second / 60
        dot = {"time": moment, "success": log.success, "pct": f"{minute * 100 / 1440:.3f}", "gap": None}
        if dots:
            previous = dots[-1]
            gap = round((moment - previous["time"]).total_seconds() / 60)
            width = (moment - previous["time"]).total_seconds() * 100 / 86400
            dot.update(gap=gap, previous=previous["time"], gap_from=previous["pct"], gap_width=f"{width:.3f}",
                       gap_label=width >= TIMELINE_LABEL_MIN_PCT)
            gaps.append(dot)
        dots.append(dot)
    rows = [{"day": day, "dots": dots, "count": len(dots)} for day, dots in sorted(days.items(), reverse=True)]
    return {
        "days": rows,
        "total": sum(row["count"] for row in rows),
        "count": len(gaps),
        "average": round(sum(g["gap"] for g in gaps) / len(gaps)) if gaps else None,
        "fastest": min(gaps, key=lambda g: g["gap"], default=None),
        "slowest": max(gaps, key=lambda g: g["gap"], default=None),
        "hours": [{"label": f"{h:02d}", "pct": f"{h * 100 / 24:g}"} for h in range(0, 25, 3)],
    }


BUBBLE_MIN_PX, BUBBLE_MAX_PX = 8, 30
GAP_FAST, GAP_SLOW = 30, 60  # minutes: < FAST = very frequent, > SLOW = relaxed


def _gap_level(minutes):
    if minutes is None:
        return "none"
    return "fast" if minutes < GAP_FAST else "slow" if minutes > GAP_SLOW else "mid"


def notification_bubbles(devices, start, end):
    """Notification traffic for several devices: per device (row) and hour of day (column) the number
    of notifications in the range (bubble size, area proportional) and the average gap since that
    device's previous notification on the same WIB day (bubble colour). Also a summary per device."""
    since, until = range_bounds(start, end)
    logs = (NotificationLog.objects.filter(device__in=devices, time__gte=since, time__lt=until)
            .order_by("time").only("time", "device_id"))
    cells, gaps, last = {}, {}, {}
    for log in logs:
        moment = timezone.localtime(log.time)
        cell = cells.setdefault((log.device_id, moment.hour), {"count": 0, "gaps": []})
        cell["count"] += 1
        previous = last.get(log.device_id)
        if previous and previous.date() == moment.date():
            gap = round((moment - previous).total_seconds() / 60)
            cell["gaps"].append(gap)
            gaps.setdefault(log.device_id, []).append({"gap": gap, "previous": previous, "time": moment})
        last[log.device_id] = moment
    peak = max((c["count"] for c in cells.values()), default=0)
    rows = []
    for device in devices:
        bubbles = []
        for hour in range(24):
            cell = cells.get((device.id, hour))
            if not cell:
                bubbles.append(None)
                continue
            average = round(sum(cell["gaps"]) / len(cell["gaps"])) if cell["gaps"] else None
            size = BUBBLE_MIN_PX + (BUBBLE_MAX_PX - BUBBLE_MIN_PX) * (cell["count"] / peak) ** 0.5
            bubbles.append({"hour": hour, "count": cell["count"], "average": average,
                            "level": _gap_level(average), "size": f"{size:.0f}"})
        device_gaps = gaps.get(device.id, [])
        rows.append({
            "device": device,
            "location": _location_label(device),
            "bubbles": bubbles,
            "total": sum(b["count"] for b in bubbles if b),
            "average": round(sum(g["gap"] for g in device_gaps) / len(device_gaps)) if device_gaps else None,
            "fastest": min(device_gaps, key=lambda g: g["gap"], default=None),
            "slowest": max(device_gaps, key=lambda g: g["gap"], default=None),
        })
    return {"rows": rows, "peak": peak, "hours": list(range(24))}


# ---------- recap ----------

def recap(devices, start, end, period=RECAP_DAILY):
    """Per day or month in the range, newest first, only periods with activity:
    events received (in/out only, like the Event Log), visitors in, Work Orders sent / succeeded /
    failed. Also returns the totals."""
    since, until = range_bounds(start, end)
    tz = timezone.get_current_timezone()
    trunc = TruncMonth if period == RECAP_MONTHLY else TruncDate

    events = (
        EventLog.objects.filter(device__in=devices, time__gte=since, time__lt=until, event_type__in=EVENT_LOG_TYPES)
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
        logs = logs.filter(Q(wo_number__icontains=query) | Q(response_status__icontains=query))
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

def device_cards(device_filter, start, end, types):
    """One overview card per washroom sensor (every type in `types` except people counting), ordered by
    type then name: latest reading in the range, or the rating summary for satisfaction devices.
    `device_filter` selects the location's devices (see locations.resolve)."""
    order = {key: index for index, key in enumerate(types)}
    devices = sorted(
        DeviceList.objects.filter(type__in=types, **device_filter).exclude(type=DeviceList.TYPE_PEOPLE)
        .select_related("scope__area"),
        key=lambda d: (order[d.type], d.name or d.id, d.id),
    )
    since, until = range_bounds(start, end)
    readings = SensorReading.objects.filter(device__in=devices, time__gte=since, time__lt=until)
    # Latest reading / response per device in the range (PostgreSQL DISTINCT ON).
    last_reading = {r.device_id: r for r in readings.order_by("device_id", "-time", "-id").distinct("device_id")}
    responses = CustomerResponse.objects.filter(device__in=devices, time__gte=since, time__lt=until)
    last_response = {r.device_id: r for r in responses.order_by("device_id", "-time", "-id").distinct("device_id")}
    ratings = {row.pop("device"): row for row in responses.values("device")
               .annotate(average=Avg("rating"), count=Count("id"))}

    cards = []
    for device in devices:
        card = {"device": device, "type": device.type, "location": _location_label(device), "has_data": False}
        if device.type == SATISFACTION_TYPE:
            stats = ratings.get(device.id)
            if stats:
                card.update(has_data=True, average_rating=stats["average"], response_count=stats["count"],
                            rating_progress=round(stats["average"] * 20), latest=last_response[device.id])
        else:
            reading = last_reading.get(device.id)
            if reading:
                card.update(
                    has_data=True, latest=reading,
                    level_progress=(max(0, min(round(reading.level), 100))
                                    if reading.level is not None and device.type != "ammonia" else None),
                )
        cards.append(card)
    return cards


# ---------- overview: sensor summary ----------

READING_TYPES = ["soap", "toilet-paper", "tissue", "trash", "ammonia"]
STATUSES = ["critical", "warning", "normal", "nodata"]  # display order
LOW_BATTERY = 20  # %, below this a sensor is listed under "Baterai lemah"
NO_DATA_AFTER = timedelta(hours=2)  # sensors send every 30 min; older than this (today) = no data
SUMMARY_LIST_LIMIT = 5
DONUT_GAP = 0.6  # % of the ring left blank between segments
AMMONIA_SCALE = 40  # ppm at the right end of the ammonia scale


def latest_readings(devices, until):
    """Latest SensorReading per device before `until` (PostgreSQL DISTINCT ON)."""
    return {r.device_id: r for r in SensorReading.objects.filter(device__in=devices, time__lt=until)
            .order_by("device_id", "-time", "-id").distinct("device_id")}


def reading_status(reading, now, live):
    """critical / warning / normal from the reading's severity; nodata when there is none, its status
    is unknown, or (when looking at today) it is older than NO_DATA_AFTER."""
    if reading is None or reading.severity not in ("critical", "warning", "normal"):
        return "nodata"
    if live and now - reading.time > NO_DATA_AFTER:
        return "nodata"
    return reading.severity


def _status_labels(sensor_type, rules):
    """Status names per severity for a type, from the StatusRules (e.g. soap: Habis / Hampir Habis / Terisi)."""
    labels = {}
    for rule in rules:
        if rule.sensor_type == sensor_type:
            labels.setdefault(rule.severity, rule.condition)
    return labels


def _donut(counts, total):
    """SVG ring segments (circumference 100, starting at 12 o'clock) for the counts, in STATUSES order."""
    segments, offset = [], 0.0
    for status in STATUSES:
        share = counts[status] * 100 / total if total else 0
        if share:
            length = max(share - (DONUT_GAP if share < 100 else 0), 0.5)
            segments.append({"status": status, "dash": f"{length:.2f} {100 - length:.2f}",
                             "offset": f"{25 - offset:.2f}"})
        offset += share
    return segments


def _critical_since(device, reading):
    """When the device's current critical streak started: first critical reading after its last
    non-critical one."""
    last_ok = (SensorReading.objects.filter(device=device, time__lte=reading.time).exclude(severity="critical")
               .order_by("-time").values_list("time", flat=True).first())
    first = SensorReading.objects.filter(device=device, severity="critical", time__lte=reading.time)
    if last_ok:
        first = first.filter(time__gt=last_ok)
    return first.order_by("time").values_list("time", flat=True).first() or reading.time


def visitor_trend(devices, start, end):
    """IN visitors over the selected range for the summary sparkline: per hour for a single day,
    otherwise per day. SVG polyline points use viewBox 0 0 100 32."""
    if start == end:
        values = [bar["count"] for bar in hourly(devices, start, end)["bars"]]
        if end == timezone.localdate():
            values = values[:timezone.localtime().hour + 1]  # the rest of today hasn't happened yet
        unit = "hour"
    else:
        since, until = range_bounds(start, end)
        tz = timezone.get_current_timezone()
        counts = dict(
            EventLog.objects.filter(device__in=devices, time__gte=since, time__lt=until).filter(COUNTED_EVENTS)
            .annotate(day=TruncDate("time", tzinfo=tz)).values("day").annotate(n=Count("id"))
            .values_list("day", "n")
        )
        values = [counts.get(start + timedelta(days=i), 0) for i in range((end - start).days + 1)]
        unit = "day"
    peak = max(values, default=0) or 1
    step = 100 / max(len(values) - 1, 1)
    points = " ".join(f"{i * step:.1f},{30 - v * 26 / peak:.1f}" for i, v in enumerate(values))
    return {"values": values, "points": points, "unit": unit}


def sensor_summary(device_filter, start, end):
    """Analytics above the overview's device cards, for the location and date range only (the type
    filter and search don't apply). Washroom sensors use their latest reading up to the end of the
    range; people counting and satisfaction use the range. Returns per-type blocks, overall KPIs, the
    critical ("perlu tindakan") and low-battery lists, and `status_by_device` for the card filter."""
    since, until = range_bounds(start, end)
    now = timezone.now()
    live = end >= timezone.localdate()
    devices = list(DeviceList.objects.filter(**device_filter).select_related("scope__area"))
    rules = list(StatusRule.objects.all())
    reading_devices = [d for d in devices if d.type in READING_TYPES]
    latest = latest_readings(reading_devices, min(until, now))

    status_by_device, blocks, critical, low_battery = {}, {}, [], []
    for device in reading_devices:
        reading = latest.get(device.id)
        status = reading_status(reading, now, live)
        status_by_device[device.id] = status
        block = blocks.setdefault(device.type, {"type": device.type, "total": 0, "levels": [],
                                                "counts": dict.fromkeys(STATUSES, 0), "worst": None})
        block["total"] += 1
        block["counts"][status] += 1
        if reading is not None and reading.level is not None and status != "nodata":
            block["levels"].append(reading.level)
            if block["worst"] is None or reading.level > block["worst"]["level"]:
                block["worst"] = {"level": reading.level, "device": device, "location": _location_label(device)}
        if status == "critical":
            critical.append({"device": device, "reading": reading, "location": _location_label(device),
                             "since": _critical_since(device, reading)})
        if reading is not None and reading.battery is not None and reading.battery < LOW_BATTERY:
            low_battery.append({"device": device, "reading": reading, "location": _location_label(device)})

    for block in blocks.values():
        labels = _status_labels(block["type"], rules)
        block["statuses"] = [
            {"status": s, "label": labels.get(s), "count": block["counts"][s],
             "pct": f"{block['counts'][s] * 100 / block['total']:.2f}"}
            for s in STATUSES
        ]
        block["average_level"] = round(sum(block["levels"]) / len(block["levels"])) if block["levels"] else None
        block["donut"] = _donut(block["counts"], block["total"])
        if block["average_level"] is not None:
            block["gauge"] = f"{max(0, min(block['average_level'], 100)) / 2:g}"  # a half ring is 50 of 100
        if block["worst"] is not None:
            # Ammonia: marker for the highest ppm and the Normal / Bau / Bahaya zone bounds from the rules.
            block["marker"] = f"{min(block['worst']['level'], AMMONIA_SCALE) * 100 / AMMONIA_SCALE:g}"
            bounds = sorted({r.max_level for r in rules if r.sensor_type == block["type"] and r.max_level is not None})
            block["zones"] = [f"{min(b, AMMONIA_SCALE) * 100 / AMMONIA_SCALE:g}" for b in bounds]

    satisfaction = None
    rating_devices = [d for d in devices if d.type == SATISFACTION_TYPE]
    if rating_devices:
        responses = CustomerResponse.objects.filter(device__in=rating_devices, time__gte=since, time__lt=until)
        stats = responses.aggregate(average=Avg("rating"), count=Count("id"))
        per_star = dict(responses.values("rating").annotate(n=Count("id")).values_list("rating", "n"))
        top = max(per_star.values(), default=0) or 1
        satisfaction = {
            "devices": len(rating_devices), "average": stats["average"], "count": stats["count"],
            "stars": [{"stars": s, "count": per_star.get(s, 0), "pct": f"{per_star.get(s, 0) * 100 / top:.1f}"}
                      for s in range(5, 0, -1)],
        }

    people = None
    people_devices = [d for d in devices if d.type == DeviceList.TYPE_PEOPLE]
    if people_devices:
        statuses = sensor_statuses(people_devices)
        people = {**kpis(people_devices, start, end), "devices": len(people_devices),
                  "online": sum(s["online"] for s in statuses), "trend": visitor_trend(people_devices, start, end)}

    critical.sort(key=lambda row: row["since"])
    low_battery.sort(key=lambda row: row["reading"].battery)
    counts = {s: sum(b["counts"][s] for b in blocks.values()) for s in STATUSES}
    return {
        "blocks": blocks,
        "people": people,
        "satisfaction": satisfaction,
        "kpis": {"devices": len(devices), "critical": counts["critical"], "warning": counts["warning"],
                 "nodata": counts["nodata"], "low_battery": len(low_battery)},
        "critical": critical[:SUMMARY_LIST_LIMIT],
        "critical_total": len(critical),
        "low_battery": low_battery[:SUMMARY_LIST_LIMIT],
        "low_battery_total": len(low_battery),
        "low_battery_ids": {row["device"].id for row in low_battery},
        "status_by_device": status_by_device,
        "live": live,
    }


SEARCH_LOCATION_LIMIT = 8


def search_devices(q):
    """Devices whose name or ID contains `q` (case-insensitive)."""
    return Q(name__icontains=q) | Q(id__icontains=q)


def search_locations(q):
    """Locations whose name contains `q`, as {"level", "label", "query"} for the overview's chips, the
    query selecting that location in the header filter. Only locations that lead down to a Scope
    (like the dropdowns); a Region is shown per client ("Client · Region"), since regions are shared."""
    found = []
    for client in Client.objects.filter(name__icontains=q, sites__areas__scopes__isnull=False).distinct():
        found.append({"level": "client", "label": client.name, "query": f"client={client.pk}"})
    pairs = (Site.objects.filter(region__name__icontains=q, areas__scopes__isnull=False)
             .values_list("client_id", "client__name", "region_id", "region__name").distinct())
    for client_id, client_name, region_id, region_name in sorted(set(pairs), key=lambda p: (p[1], p[3])):
        found.append({"level": "region", "label": f"{client_name} · {region_name}",
                      "query": f"client={client_id}&region={region_id}"})
    for site in Site.objects.filter(name__icontains=q, areas__scopes__isnull=False).select_related("client").distinct():
        found.append({"level": "site", "label": f"{site.client.name} · {site.name}", "query": f"site={site.pk}"})
    for area in Area.objects.filter(name__icontains=q, scopes__isnull=False).select_related("site").distinct():
        found.append({"level": "area", "label": f"{area.site.name} · {area.name}", "query": f"area={area.pk}"})
    for scope in Scope.objects.filter(name__icontains=q).select_related("area"):
        found.append({"level": "scope", "label": f"{scope.area.name} · {scope.name}", "query": f"scope={scope.pk}"})
    return found[:SEARCH_LOCATION_LIMIT]


def _location_label(device):
    """"Area · Scope" of a device, or None when it has no Scope."""
    scope = device.scope
    return f"{scope.area.name} · {scope.name}" if scope else None
