from django.utils import timezone
from rest_framework import serializers

from core.models import DeviceList

from .models import READING_TYPES, SATISFACTION_TYPE, CustomerResponse, SensorReading

TIME_HELP = "ISO 8601. Without offset it is read as WIB. Default: time received."


def registered_device(device_id, allowed_types, field="device_id"):
    """The Admin-registered device with that ID, if its type is one of `allowed_types`."""
    registered = DeviceList.objects.filter(device_id=device_id).first()
    if not registered:
        raise serializers.ValidationError(
            {field: "Device ID tidak terdaftar di admin. Harap daftarkan device terlebih dahulu."}
        )
    if registered.type not in allowed_types:
        expected = " / ".join(allowed_types)
        raise serializers.ValidationError(
            {field: f"Device {device_id} terdaftar sebagai '{registered.type}', bukan '{expected}'."}
        )
    return registered


def check_device_type(device_id, expected_type):
    registered_device(device_id, [expected_type])


class ReadingInSerializer(serializers.Serializer):
    """One reading in the sensor team's raw data format (Washroom Dashboard Raw Data Documentation v1.0).

    The sensor type is not in the payload: it is the `type` of the device registered in Admin.
    """

    id = serializers.CharField(max_length=100, help_text="The sender's data id, unique per device.")
    inputDate = serializers.DateTimeField(help_text=TIME_HELP.replace(" Default: time received.", ""))
    deviceId = serializers.CharField(max_length=100, help_text="Must be registered in Admin (Device list).")
    value = serializers.FloatField(
        required=False, allow_null=True,
        help_text="Stored as sent: % for soap, toilet-paper, tissue, trash; ppm for ammonia.",
    )
    battery = serializers.FloatField(min_value=0, max_value=100, required=False, allow_null=True)
    lastOnline = serializers.DateTimeField(required=False, allow_null=True, help_text=TIME_HELP.split(".")[0])
    status = serializers.CharField(max_length=50, required=False, allow_blank=True, allow_null=True)

    def validate(self, attrs):
        attrs["device"] = registered_device(attrs["deviceId"], READING_TYPES, field="deviceId")
        return attrs


class CustomerResponseInSerializer(serializers.Serializer):
    device_id = serializers.CharField(max_length=100)
    time = serializers.DateTimeField(required=False, help_text=TIME_HELP)
    rating = serializers.IntegerField(min_value=1, max_value=5)
    comment = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        check_device_type(attrs["device_id"], SATISFACTION_TYPE)
        attrs.setdefault("time", timezone.now())
        return attrs


class DeviceLocationMixin(serializers.Serializer):
    building = serializers.CharField(source="device.building", read_only=True)
    floor = serializers.CharField(source="device.floor", read_only=True)
    gender = serializers.CharField(source="device.gender", read_only=True)
    location = serializers.SerializerMethodField(
        help_text="The device's Client / Region / Site / Area / Scope (names, scope_id), or null when no Scope is set."
    )

    def get_location(self, obj) -> dict | None:
        scope = obj.device.scope
        if scope is None:
            return None
        site = scope.area.site
        return {"client": site.client.name, "region": site.region.name, "site": site.name,
                "area": scope.area.name, "scope": scope.name, "scope_id": scope.pk}


class ReadingOutSerializer(DeviceLocationMixin, serializers.ModelSerializer):
    """A stored reading in the sender's field names, plus our id and the device's type and location."""

    reading_id = serializers.IntegerField(source="pk")
    id = serializers.CharField(source="external_id")
    deviceId = serializers.CharField(source="device.device_id")
    type = serializers.CharField(source="device.type")
    inputDate = serializers.DateTimeField(source="time")
    value = serializers.FloatField(source="level", allow_null=True)
    lastOnline = serializers.DateTimeField(source="last_online", allow_null=True)
    status = serializers.CharField(source="condition")

    class Meta:
        model = SensorReading
        fields = ("reading_id", "id", "deviceId", "type", "building", "floor", "gender",
                  "location", "inputDate", "value", "battery", "lastOnline", "status", "severity")


class CustomerResponseOutSerializer(DeviceLocationMixin, serializers.ModelSerializer):
    response_id = serializers.IntegerField(source="id")
    device_id = serializers.CharField(source="device.device_id")

    class Meta:
        model = CustomerResponse
        fields = ("response_id", "device_id", "building", "floor", "gender", "location", "time", "rating", "comment")
