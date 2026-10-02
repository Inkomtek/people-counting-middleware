"""Store readings and ratings sent by the ZK side."""

from django.db import transaction

from core.models import DeviceList

from .models import SATISFACTION_TYPE, CustomerResponse, SensorReading, StatusRule


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


def _get_device(device_id, device_type):
    # Unknown devices are registered without a location; Admin sets building/floor/gender so the
    # dashboard can place them. Only "people" devices are synced from ZK, so these never are.
    device, _ = DeviceList.objects.get_or_create(id=device_id, defaults={"type": device_type})
    return device


def store_readings(items, client):
    """Save validated reading dicts. Returns the created readings."""
    rules = list(StatusRule.objects.all())
    with transaction.atomic():
        created = []
        for item in items:
            device = _get_device(item["device_id"], item["type"])
            condition, severity = evaluate_condition(device.type, item.get("level"), rules)
            created.append(SensorReading.objects.create(
                device=device,
                time=item["time"],
                battery=item.get("battery"),
                level=item.get("level"),
                condition=condition,
                severity=severity,
                payload=item["payload"],
                client=client,
            ))
    return created


def store_customer_responses(items, client):
    """Save validated rating dicts. Returns the created responses."""
    with transaction.atomic():
        return [
            CustomerResponse.objects.create(
                device=_get_device(item["device_id"], SATISFACTION_TYPE),
                time=item["time"],
                rating=item["rating"],
                comment=item.get("comment", ""),
                payload=item["payload"],
                client=client,
            )
            for item in items
        ]
