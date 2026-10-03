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
    """Only Admin-managed devices are allowed. Unknown device IDs are rejected earlier in validation."""
    device = DeviceList.objects.filter(id=device_id).first()
    if device is None:
        raise ValueError(f"Device ID {device_id} tidak terdaftar di admin.")
    if device.type != device_type:
        raise ValueError(f"Device {device_id} terdaftar sebagai '{device.type}', bukan '{device_type}'.")
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
