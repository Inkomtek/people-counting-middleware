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
SENSOR_SORTS = ("sensor", "location", "count", "visitors", "status")  # sortable sensor-table columns
DONUT_R = 15.915  # circumference 100


def sensor_table(devices, start, end, sort="", direction="asc"):
    """People counting sensors as table rows: sensor_statuses plus location, visitors in and notifications
    sent / failed in the range. Sorted by a column (SENSOR_SORTS) in `direction` ("asc" / "desc"); without
    one, "needs attention" order: offline first, then closest to the trigger."""
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
    keys = {
        "sensor": lambda r: r["device"].label.lower(),
        "location": lambda r: ((r["location"] or "").lower(), r["device"].label.lower()),
        "count": lambda r: (r["progress"], r["current_count"], r["device"].label.lower()),
        "visitors": lambda r: (r["visitors"], r["device"].label.lower()),
        "status": lambda r: (r["online"], r["device"].label.lower()),  # ascending = offline first
    }
    if sort not in keys:
        return sorted(rows, key=lambda r: (r["online"], -r["progress"], r["device"].label))
    return sorted(rows, key=keys[sort], reverse=direction == "desc")


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


def _gap_stats(gaps):
    """Average / fastest / slowest of within-day gaps ({"gap", "previous", "time"} dicts)."""
    return {
        "count": len(gaps),
        "average": round(sum(g["gap"] for g in gaps) / len(gaps)) if gaps else None,
        "fastest": min(gaps, key=lambda g: g["gap"], default=None),
        "slowest": max(gaps, key=lambda g: g["gap"], default=None),
    }


NT_SUCCESS, NT_FAILED = "success", "failed"
NT_MIN_HOURS = 8  # the chart shows at least this many hours around the busy ones
# The average time between notifications only uses notifications sent from 01:00 to 23:00 (WIB); the
# midnight and 23:xx hours are left out (decision 2026-10-08).
GAP_FIRST_HOUR, GAP_LAST_HOUR = 1, 22


def _round_up(value, step):
    return max(step, -(-value // step) * step)


def notification_chart(devices, start, end):
    """Notification traffic of one or several devices (summed) as ONE bar chart per hour of day over the range.

    Each hour has a bar of the notifications Algospection accepted, plus a pink bar beside it for failed sends
    (only when the range has any). When Work Orders have finish times (NotificationLog.completed_at, from
    Algospection) a line shows the average time to finish per hour on a right-hand minute axis. Hours shown:
    from the first to the last hour with notifications, widened to at least NT_MIN_HOURS. Also KPIs and the
    gap stats: per device, the gaps between its notifications on the same day sent between GAP_FIRST_HOUR and
    GAP_LAST_HOUR o'clock; `average` = the average of each device's own average (every device weighs the same),
    `device_averages` = those per-device averages."""
    since, until = range_bounds(start, end)
    logs = list(NotificationLog.objects.filter(device__in=devices, time__gte=since, time__lt=until)
                .order_by("time").only("time", "success", "completed_at", "device_id"))
    now = timezone.now()
    cells = {hour: {NT_SUCCESS: 0, NT_FAILED: 0, "minutes": []} for hour in range(24)}
    gaps, previous, open_logs, finished = {}, {}, [], []
    for log in logs:
        moment = timezone.localtime(log.time)
        cell = cells[moment.hour]
        cell[NT_SUCCESS if log.success else NT_FAILED] += 1
        if log.success:
            minutes = log.completion_minutes
            if minutes is None:
                open_logs.append(log)
            else:
                cell["minutes"].append(minutes)
                finished.append(minutes)
        if GAP_FIRST_HOUR <= moment.hour <= GAP_LAST_HOUR:
            last = previous.get(log.device_id)
            if last and last.date() == moment.date():
                gaps.setdefault(log.device_id, []).append(
                    {"gap": round((moment - last).total_seconds() / 60), "previous": last, "time": moment})
            previous[log.device_id] = moment

    device_averages = {device_id: round(sum(g["gap"] for g in device_gaps) / len(device_gaps))
                       for device_id, device_gaps in gaps.items()}
    all_gaps = [gap for device_gaps in gaps.values() for gap in device_gaps]
    failed = sum(cells[h][NT_FAILED] for h in range(24))
    series = (NT_SUCCESS, NT_FAILED) if failed else (NT_SUCCESS,)
    completion = bool(finished)
    busy = [hour for hour in range(24) if cells[hour][NT_SUCCESS] + cells[hour][NT_FAILED]]
    shown = []
    if busy:
        first, last = busy[0], busy[-1]
        if last - first + 1 < NT_MIN_HOURS:  # widen evenly around the busy hours, staying inside 00-23
            first = max(0, first - (NT_MIN_HOURS - (last - first + 1)) // 2)
            last = min(23, first + NT_MIN_HOURS - 1)
            first = max(0, last - NT_MIN_HOURS + 1)
        shown = list(range(first, last + 1))

    peak = max((cells[h][key] for h in shown for key in series), default=0)
    # Whole-number axis: one gridline per notification up to 5, else four even steps.
    count_top = peak if peak <= 5 else _round_up(peak, 4)
    count_values = list(range(count_top + 1)) if peak <= 5 else [count_top * i // 4 for i in range(5)]
    averages = {h: round(sum(cells[h]["minutes"]) / len(cells[h]["minutes"])) for h in shown if cells[h]["minutes"]}
    minute_top = _round_up(max(averages.values(), default=0), 20)  # four steps of whole minutes
    pct = lambda value, top: f"{value * CHART_TOP / top:g}" if top else "0"  # noqa: E731 (strings: valid CSS)
    hours = []
    for index, hour in enumerate(shown):
        cell = cells[hour]
        average = averages.get(hour)
        hours.append({
            "hour": hour,
            "total": cell[NT_SUCCESS] + cell[NT_FAILED],
            "bars": [{"key": key, "count": cell[key], "pct": pct(cell[key], count_top)} for key in series],
            "average": average,
            "finished": len(cell["minutes"]),
            "avg_pct": pct(average, minute_top) if average is not None else None,
            "x": f"{(index + 0.5) * 100 / len(shown):.3f}",  # centre of the hour's column, % of the chart width
        })
    line = " ".join(f"{(i + 0.5) * 1000 / len(shown):.1f},{1000 - h['average'] * 10 * CHART_TOP / minute_top:.1f}"
                    for i, h in enumerate(hours) if h["average"] is not None)
    totals = [cells[h][NT_SUCCESS] + cells[h][NT_FAILED] for h in range(24)]
    busiest = max(range(24), key=lambda h: totals[h]) if any(totals) else None
    oldest_open = min(open_logs, key=lambda log: log.time, default=None)
    slowest = max(averages, key=averages.get) if averages else None
    fastest = min(averages, key=averages.get) if averages else None
    return {
        "devices": list(devices),
        "completion": completion,
        "series": series,
        "hours": hours,
        "count_ticks": [{"value": v, "pct": pct(v, count_top)} for v in count_values] if peak else [],
        "minute_ticks": [{"value": minute_top * i // 4, "pct": f"{CHART_TOP * i / 4:g}"} for i in range(5)],
        "line": line,
        # KPIs
        "total": len(logs),
        "failed": failed,
        "busiest_hour": busiest,
        "busiest_count": totals[busiest] if busiest is not None else 0,
        "finished": len(finished),
        "open": len(open_logs),
        "oldest_open_minutes": round((now - oldest_open.time).total_seconds() / 60) if oldest_open else None,
        "average_completion": round(sum(finished) / len(finished)) if finished else None,
        "slowest_hour": slowest,
        "slowest_average": averages.get(slowest),
        "slowest_count": totals[slowest] if slowest is not None else 0,
        "fastest_hour": fastest,
        "fastest_average": averages.get(fastest),
        "fastest_count": totals[fastest] if fastest is not None else 0,
        **_gap_stats(all_gaps),
        "average": round(sum(device_averages.values()) / len(device_averages)) if device_averages else None,
        "device_averages": device_averages,
    }


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

# Sortable columns of the Notification Log and Event Log tables: column key -> model fields (ascending).
WO_SORTS = {"time": ("time",), "number": ("wo_number",), "device": ("device_id",),
            "destination": ("endpoint_url",), "status": ("success", "response_status")}
EVENT_SORTS = {"time": ("time", "id"), "device": ("device_id",), "event": ("id",), "type": ("event_type",),
               "target": ("recognition_target",), "counted": ("counted",)}


def _sorted(queryset, columns, sort, direction, default):
    """Order by a sortable column (`columns[sort]`, "asc"/"desc"), newest first among equal values; without
    a known column, the `default` ordering."""
    if sort not in columns:
        return queryset.order_by(*default)
    sign = "-" if direction == "desc" else ""
    return queryset.order_by(*(sign + field for field in columns[sort]), *default)


def work_orders(devices, start, end, status="", query="", sort="", direction="asc"):
    since, until = range_bounds(start, end)
    logs = _sorted(NotificationLog.objects.filter(device__in=devices, time__gte=since, time__lt=until)
                   .select_related("device"), WO_SORTS, sort, direction, ("-time",))
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

def events(devices, start, end, time_from=None, time_to=None, event_type="", sort="", direction="asc"):
    """Event log in the range, newest first unless sorted by a column (EVENT_SORTS); time_from/time_to filter
    the time of day."""
    since, until = range_bounds(start, end)
    logs = _sorted(EventLog.objects.filter(device__in=devices, time__gte=since, time__lt=until,
                                           event_type__in=EVENT_LOG_TYPES)
                   .select_related("device"), EVENT_SORTS, sort, direction, ("-time", "-id"))
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

# Satisfaction has 3 levels. The API still takes a 1-5 rating until the sensor team's spec arrives;
# this mapping is the only place to change when they send levels instead.
RATING_LEVELS = ["excellent", "average", "bad"]


def rating_level(rating):
    """excellent (4-5) / average (3) / bad (1-2)."""
    return "excellent" if rating >= 4 else "average" if rating == 3 else "bad"


def rating_levels(responses):
    """Counts and shares per level for a CustomerResponse queryset, plus the % excellent."""
    counts = dict.fromkeys(RATING_LEVELS, 0)
    for rating, n in responses.values("rating").annotate(n=Count("id")).values_list("rating", "n"):
        counts[rating_level(rating)] += n
    total = sum(counts.values())
    levels = [{"level": level, "count": counts[level],
               "pct": round(counts[level] * 100 / total) if total else 0,
               "width": f"{counts[level] * 100 / total:.2f}" if total else "0"}
              for level in RATING_LEVELS]
    return {"total": total, "levels": levels, "excellent_pct": levels[0]["pct"] if total else None}


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

    cards = []
    for device in devices:
        card = {"device": device, "type": device.type, "location": _location_label(device), "has_data": False}
        if device.type == SATISFACTION_TYPE:
            latest = last_response.get(device.id)
            if latest:
                card.update(has_data=True, ratings=rating_levels(responses.filter(device=device)),
                            latest=latest, latest_level=rating_level(latest.rating))
        else:
            reading = last_reading.get(device.id)
            if reading:
                card.update(has_data=True, latest=reading)
        cards.append(card)
    return cards


# ---------- overview: sensor summary ----------

READING_TYPES = ["soap", "toilet-paper", "tissue", "trash", "ammonia"]
STATUSES = ["critical", "warning", "normal", "nodata"]  # display order
LOW_BATTERY = 20  # %, below this a sensor is listed under "Baterai lemah"
NO_DATA_AFTER = timedelta(hours=2)  # sensors send every 30 min; older than this (today) = no data
SUMMARY_LIST_LIMIT = 5
DONUT_GAP = 0.6  # % of the ring left blank between segments


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


TREND_LABELS = 3  # axis labels under the summary's mini bars: first, middle, last


def visitor_trend(devices, start, end):
    """IN visitors over the selected range as mini bars for the summary card: per hour for a single
    day (today only up to the current hour), otherwise per day. Each bar has a label (hour or date) and
    a height %; the busiest one is flagged and returned as `peak`."""
    if start == end:
        counts = [bar["count"] for bar in hourly(devices, start, end)["bars"]]
        if end == timezone.localdate():
            counts = counts[:timezone.localtime().hour + 1]  # the rest of today hasn't happened yet
        labels = [f"{hour:02d}:00" for hour in range(len(counts))]
        unit = "hour"
    else:
        since, until = range_bounds(start, end)
        tz = timezone.get_current_timezone()
        per_day = dict(
            EventLog.objects.filter(device__in=devices, time__gte=since, time__lt=until).filter(COUNTED_EVENTS)
            .annotate(day=TruncDate("time", tzinfo=tz)).values("day").annotate(n=Count("id"))
            .values_list("day", "n")
        )
        days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        counts = [per_day.get(day, 0) for day in days]
        labels = days
        unit = "day"
    top = max(counts, default=0)
    peak_index = counts.index(top) if top else None
    marks = {0, len(counts) // 2, len(counts) - 1} if counts else set()
    bars = [{"count": n, "label": label, "pct": f"{n * 100 / top:.1f}" if top else "0",
             "peak": i == peak_index, "show_label": i in marks}
            for i, (n, label) in enumerate(zip(counts, labels))]
    return {"bars": bars, "unit": unit, "peak": bars[peak_index] if peak_index is not None else None}


def sensor_summary(device_filter, start, end, sensor_type=""):
    """Analytics above the overview's device cards, for the location, date range and the "Jenis Sensor"
    filter (`sensor_type`, "" = all; the search doesn't apply). Washroom sensors use their latest reading up to the end of the
    range; people counting and satisfaction use the range. Returns per-type blocks, overall KPIs, the
    critical ("perlu tindakan") and low-battery lists, and `status_by_device` for the card filter."""
    since, until = range_bounds(start, end)
    now = timezone.now()
    live = end >= timezone.localdate()
    devices = DeviceList.objects.filter(**device_filter).select_related("scope__area")
    if sensor_type:
        devices = devices.filter(type=sensor_type)
    devices = list(devices)
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

    # Notifications sent per sensor type in the range. Only people counting sends Work Orders today, so
    # the other types show 0 until they get their own notifications (then this fills in by itself).
    sent_by_type = dict(NotificationLog.objects.filter(device__in=devices, time__gte=since, time__lt=until)
                        .values("device__type").annotate(n=Count("id")).values_list("device__type", "n"))
    for block in blocks.values():
        block["notifications"] = sent_by_type.get(block["type"], 0)
        labels = _status_labels(block["type"], rules)
        block["statuses"] = [
            {"status": s, "label": labels.get(s), "count": block["counts"][s],
             "pct": f"{block['counts'][s] * 100 / block['total']:.2f}"}
            for s in STATUSES
        ]
        block["average_level"] = round(sum(block["levels"]) / len(block["levels"])) if block["levels"] else None
        block["donut"] = _donut(block["counts"], block["total"])

    satisfaction = None
    rating_devices = [d for d in devices if d.type == SATISFACTION_TYPE]
    if rating_devices:
        responses = CustomerResponse.objects.filter(device__in=rating_devices, time__gte=since, time__lt=until)
        satisfaction = {"devices": len(rating_devices), **rating_levels(responses),
                        "notifications": sent_by_type.get(SATISFACTION_TYPE, 0)}

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
                 "normal": counts["normal"], "nodata": counts["nodata"], "low_battery": len(low_battery)},
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
