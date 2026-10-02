"""Ingest sensor readings and build the dashboard snapshot."""

from datetime import datetime, time, timedelta

from django.db import transaction
from django.db.models import Avg, Count
from django.utils import timezone

from core.models import EventLog, NotificationLog
from core.services import COUNTED_RECOGNITION_TARGET

from .models import (
    READING_TYPES,
    CustomerResponse,
    SensorDevice,
    SensorReading,
    SensorType,
    Severity,
    StatusRule,
    WashroomConfig,
)


def evaluate_condition(sensor_type, level, rules=None):
    """Return (condition, severity) for a level, or ("", "") when no level or no rule matches."""
    if level is None:
        return "", ""
    if rules is None:
        rules = StatusRule.objects.filter(sensor_type=sensor_type)
    for rule in rules:
        if rule.sensor_type == sensor_type and rule.matches(level):
            return rule.condition, rule.severity
    return "", ""


def store_readings(items, client):
    """Save validated reading dicts, registering unknown devices. Returns the created readings."""
    rules = list(StatusRule.objects.all())
    created = []
    with transaction.atomic():
        for item in items:
            device, _ = SensorDevice.objects.select_for_update().get_or_create(
                id=item["device_id"],
                defaults={"type": item["type"], "location": item.get("location", "")},
            )
            condition, severity = evaluate_condition(device.type, item.get("level"), rules)
            reading = SensorReading.objects.create(
                device=device,
                time=item["time"],
                battery=item.get("battery"),
                level=item.get("level"),
                condition=condition,
                severity=severity,
                payload=item["payload"],
                client=client,
            )
            # Readings may arrive out of order; only a newer one updates the device's live state.
            if device.last_seen is None or reading.time >= device.last_seen:
                device.last_seen = reading.time
                device.last_battery = reading.battery
                device.last_level = reading.level
                device.last_condition = condition
                device.last_severity = severity
                if item.get("location"):
                    device.location = item["location"]
                device.save()
            created.append(reading)
    return created


def store_customer_responses(items, client):
    """Save validated rating dicts, registering unknown feedback devices. Returns the created responses."""
    created = []
    with transaction.atomic():
        for item in items:
            device, _ = SensorDevice.objects.select_for_update().get_or_create(
                id=item["device_id"],
                defaults={"type": SensorType.FEEDBACK, "location": item.get("location", "")},
            )
            response = CustomerResponse.objects.create(**item, client=client)
            if device.last_seen is None or response.time >= device.last_seen:
                device.last_seen = response.time
                device.save(update_fields=["last_seen"])
            created.append(response)
    return created


SEVERITY_RANK = {Severity.CRITICAL: 3, Severity.WARNING: 2, Severity.NORMAL: 1, "": 0}


def day_bounds(day):
    start = timezone.make_aware(datetime.combine(day, time.min))
    return start, start + timedelta(days=1)


def _device_state(device, day_end, is_today, now, offline_after):
    """Latest known state of a device as of the end of `day` (live state for today)."""
    if is_today:
        state = {
            "time": device.last_seen, "battery": device.last_battery, "level": device.last_level,
            "condition": device.last_condition, "severity": device.last_severity,
        }
    else:
        reading = device.readings.filter(time__lt=day_end).order_by("-time").first()
        state = {
            "time": reading.time if reading else None,
            "battery": reading.battery if reading else None,
            "level": reading.level if reading else None,
            "condition": reading.condition if reading else "",
            "severity": reading.severity if reading else "",
        }
    state["device_id"] = device.id
    # Online/offline only makes sense for the live view.
    state["online"] = (state["time"] is not None and now - state["time"] <= offline_after) if is_today else None
    return state


def _sensor_card(sensor_type, devices, day_end, is_today, now, offline_after):
    card = {"type": sensor_type.value, "label": sensor_type.label, "available": bool(devices),
            "unit": "ppm" if sensor_type == SensorType.AMONIA else "%", "device_count": len(devices)}
    if not devices:
        return card
    states = [_device_state(d, day_end, is_today, now, offline_after) for d in devices]
    # With several devices of one type, the card shows the one in the worst condition.
    worst = max(states, key=lambda st: (SEVERITY_RANK.get(st["severity"], 0), st["time"] is not None))
    card.update({
        "device_id": worst["device_id"],
        "condition": worst["condition"],
        "severity": worst["severity"],
        "level": worst["level"],
        "battery": worst["battery"],
        "last_seen": worst["time"],
        "online": worst["online"],
        "offline_count": sum(1 for st in states if st["online"] is False),
    })
    return card


def build_dashboard(washroom, day):
    now = timezone.now()
    is_today = day == timezone.localdate()
    day_start, day_end = day_bounds(day)
    offline_minutes = WashroomConfig.get().offline_after_minutes
    offline_after = timedelta(minutes=offline_minutes)

    counters = list(washroom.people_counters.all())
    events = EventLog.objects.filter(device__in=counters, time__gte=day_start, time__lt=day_end)
    people = {
        "available": bool(counters),
        "device_ids": [c.id for c in counters],
        # Same rule as the Work Order counter: only "in" crossings count as people entering.
        "people_in": events.filter(
            event_type__iexact="in", recognition_target=COUNTED_RECOGNITION_TARGET
        ).count(),
        "work_orders": NotificationLog.objects.filter(
            device__in=counters, time__gte=day_start, time__lt=day_end
        ).count(),
        # The running count only exists "now", so it is shown for today only.
        "current_count": sum(c.current_count for c in counters) if is_today and counters else None,
        "maximum_trigger": sum(c.maximum_trigger for c in counters) if is_today and counters else None,
    }

    devices = list(washroom.devices.all())
    sensors = [
        _sensor_card(t, [d for d in devices if d.type == t], day_end, is_today, now, offline_after)
        for t in READING_TYPES
    ]

    feedback_ids = [d.id for d in devices if d.type == SensorType.FEEDBACK]
    responses = CustomerResponse.objects.filter(
        device_id__in=feedback_ids, time__gte=day_start, time__lt=day_end
    )
    summary = responses.aggregate(total=Count("id"), average=Avg("rating"))
    distribution = dict(responses.values_list("rating").annotate(n=Count("id")))

    return {
        "generated_at": now,
        "date": day,
        "is_today": is_today,
        "offline_after_minutes": offline_minutes,
        "washroom": washroom,
        "people_counting": people,
        "customer_satisfaction": {
            "available": bool(feedback_ids),
            "total": summary["total"],
            "average": round(summary["average"], 2) if summary["average"] is not None else None,
            "by_rating": {str(r): distribution.get(r, 0) for r in range(1, 6)},
        },
        "sensors": sensors,
        "status_rules": [
            {"type": r.sensor_type, "condition": r.condition, "severity": r.severity,
             "min_level": r.min_level, "max_level": r.max_level}
            for r in StatusRule.objects.all()
        ],
    }
