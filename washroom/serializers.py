from django.utils import timezone
from rest_framework import serializers

from core.models import DeviceList

from .models import PPM_TYPES, READING_TYPES, SATISFACTION_TYPE, CustomerResponse, SensorReading

TIME_HELP = "ISO 8601. Without offset it is read as WIB. Default: time received."


def check_device_type(device_id, expected_type):
    """Reject unknown or mismatched device IDs; only Admin-managed devices are accepted."""
    registered = DeviceList.objects.filter(pk=device_id).first()
    if not registered:
        raise serializers.ValidationError(
            {"device_id": "Device ID tidak terdaftar di admin. Harap daftarkan device terlebih dahulu."}
        )

    if registered.type != expected_type:
        raise serializers.ValidationError(
            {"device_id": f"Device {device_id} terdaftar sebagai '{registered.type}', bukan '{expected_type}'."}
        )


class ReadingInSerializer(serializers.Serializer):
    device_id = serializers.CharField(max_length=100)
    type = serializers.ChoiceField(choices=[c for c in DeviceList.TYPE_CHOICES if c[0] in READING_TYPES])
    time = serializers.DateTimeField(required=False, help_text=TIME_HELP)
    battery = serializers.IntegerField(min_value=0, max_value=100, required=False, allow_null=True)
    level = serializers.FloatField(
        min_value=0, required=False, allow_null=True,
        help_text="Fill level in % (soap, toilet-paper, tissue, trash) or concentration in ppm (ammonia).",
    )

    def validate(self, attrs):
        if attrs["type"] not in PPM_TYPES and attrs.get("level") is not None and attrs["level"] > 100:
            raise serializers.ValidationError({"level": "Level dalam persen, maksimal 100."})
        check_device_type(attrs["device_id"], attrs["type"])
        attrs.setdefault("time", timezone.now())
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


class ReadingOutSerializer(DeviceLocationMixin, serializers.ModelSerializer):
    reading_id = serializers.IntegerField(source="id")
    type = serializers.CharField(source="device.type")

    class Meta:
        model = SensorReading
        fields = ("reading_id", "device_id", "type", "building", "floor", "gender",
                  "time", "battery", "level", "condition", "severity")


class CustomerResponseOutSerializer(DeviceLocationMixin, serializers.ModelSerializer):
    response_id = serializers.IntegerField(source="id")

    class Meta:
        model = CustomerResponse
        fields = ("response_id", "device_id", "building", "floor", "gender", "time", "rating", "comment")
