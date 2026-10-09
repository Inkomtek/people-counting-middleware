"""Store readings and ratings sent by the sensor side."""

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


def severity_for_status(sensor_type, status, rules):
    """Severity of the StatusRule whose condition matches the sent status (case-insensitive), or ""."""
    wanted = (status or "").strip().lower()
    for rule in rules:
        if rule.sensor_type == sensor_type and rule.condition.lower() == wanted:
            return rule.severity
    return ""


def _get_device(device_id, device_type):
    """Only Admin-managed devices are allowed. Unknown device IDs are rejected earlier in validation."""
    device = DeviceList.objects.filter(device_id=device_id).first()
    if device is None:
        raise ValueError(f"Device ID {device_id} tidak terdaftar di admin.")
    if device.type != device_type:
        raise ValueError(f"Device {device_id} terdaftar sebagai '{device.type}', bukan '{device_type}'.")
    return device


def store_readings(items, client):
    """Save validated readings in the sender's format.

    A reading whose (deviceId, id) is already stored, or repeated in the same batch, is skipped, so the
    sender can safely resend. Returns (stored, created_count): one reading per item, in order (the
    existing one for a duplicate).
    """
    rules = list(StatusRule.objects.all())
    stored, created_count, seen = [], 0, {}
    with transaction.atomic():
        for item in items:
            device = item["device"]
            key = (device.pk, item["id"])
            reading = seen.get(key) or SensorReading.objects.filter(device=device, external_id=item["id"]).first()
            if reading is None:
                status = (item.get("status") or "").strip()
                battery = item.get("battery")
                reading = SensorReading.objects.create(
                    device=device,
                    external_id=item["id"],
                    time=item["inputDate"],
                    last_online=item.get("lastOnline"),
                    battery=round(battery) if battery is not None else None,
                    level=item.get("value"),
                    condition=status,
                    severity=severity_for_status(device.type, status, rules),
                    payload=item["payload"],
                    client=client,
                )
                created_count += 1
            seen[key] = reading
            stored.append(reading)
    return stored, created_count


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
