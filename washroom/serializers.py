from django.utils import timezone
from rest_framework import serializers

from .models import READING_TYPES, CustomerResponse, SensorDevice, SensorReading, SensorType, Washroom


class ReadingInSerializer(serializers.Serializer):
    device_id = serializers.CharField(max_length=100)
    type = serializers.ChoiceField(choices=[(t.value, t.label) for t in READING_TYPES])
    time = serializers.DateTimeField(
        required=False, help_text="ISO 8601. Without offset it is read as WIB. Default: time received."
    )
    battery = serializers.IntegerField(min_value=0, max_value=100, required=False, allow_null=True)
    level = serializers.FloatField(
        min_value=0, required=False, allow_null=True,
        help_text="Fill level in % (soap, tissue, toilet_paper, trash) or ammonia ppm (amonia).",
    )
    location = serializers.CharField(max_length=255, required=False, allow_blank=True)

    def validate(self, attrs):
        if attrs["type"] != SensorType.AMONIA and attrs.get("level") is not None and attrs["level"] > 100:
            raise serializers.ValidationError({"level": "Level dalam persen, maksimal 100."})
        device = SensorDevice.objects.filter(pk=attrs["device_id"]).only("type").first()
        if device and device.type != attrs["type"]:
            raise serializers.ValidationError(
                {"type": f"Device {device.pk} terdaftar sebagai '{device.type}', bukan '{attrs['type']}'."}
            )
        attrs.setdefault("time", timezone.now())
        return attrs


class CustomerResponseInSerializer(serializers.ModelSerializer):
    time = serializers.DateTimeField(
        required=False, help_text="ISO 8601. Without offset it is read as WIB. Default: time received."
    )

    class Meta:
        model = CustomerResponse
        fields = ("device_id", "location", "time", "rating", "comment")

    def validate(self, attrs):
        device = SensorDevice.objects.filter(pk=attrs["device_id"]).only("type").first()
        if device and device.type != SensorType.FEEDBACK:
            raise serializers.ValidationError(
                {"device_id": f"Device {device.pk} terdaftar sebagai sensor '{device.type}', bukan perangkat feedback."}
            )
        attrs.setdefault("time", timezone.now())
        return attrs


class ReadingOutSerializer(serializers.ModelSerializer):
    reading_id = serializers.IntegerField(source="id")
    type = serializers.CharField(source="device.type")

    class Meta:
        model = SensorReading
        fields = ("reading_id", "device_id", "type", "time", "battery", "level", "condition", "severity")


class CustomerResponseOutSerializer(serializers.ModelSerializer):
    response_id = serializers.IntegerField(source="id")

    class Meta:
        model = CustomerResponse
        fields = ("response_id", "device_id", "location", "time", "rating", "comment")


class WashroomSerializer(serializers.ModelSerializer):
    gender_label = serializers.CharField(source="get_gender_display")

    class Meta:
        model = Washroom
        fields = ("id", "building", "floor", "gender", "gender_label")


# Documentation-only serializers for the dashboard response.
class PeopleCountingSerializer(serializers.Serializer):
    available = serializers.BooleanField()
    device_ids = serializers.ListField(child=serializers.CharField())
    people_in = serializers.IntegerField(help_text='"in" Cross Line events on the date')
    work_orders = serializers.IntegerField(help_text="Work Order POSTs on the date")
    current_count = serializers.IntegerField(allow_null=True, help_text="Today only")
    maximum_trigger = serializers.IntegerField(allow_null=True, help_text="Today only")


class CustomerSatisfactionSerializer(serializers.Serializer):
    available = serializers.BooleanField()
    total = serializers.IntegerField()
    average = serializers.FloatField(allow_null=True)
    by_rating = serializers.DictField(child=serializers.IntegerField())


class SensorCardSerializer(serializers.Serializer):
    type = serializers.CharField()
    label = serializers.CharField()
    available = serializers.BooleanField(help_text='False = no device of this type here ("Segera Hadir")')
    unit = serializers.CharField()
    device_count = serializers.IntegerField()
    device_id = serializers.CharField(required=False)
    condition = serializers.CharField(required=False)
    severity = serializers.CharField(required=False)
    level = serializers.FloatField(required=False, allow_null=True)
    battery = serializers.IntegerField(required=False, allow_null=True)
    last_seen = serializers.DateTimeField(required=False, allow_null=True)
    online = serializers.BooleanField(required=False, allow_null=True, help_text="Today only")
    offline_count = serializers.IntegerField(required=False)


class StatusRuleOutSerializer(serializers.Serializer):
    type = serializers.CharField()
    condition = serializers.CharField()
    severity = serializers.CharField()
    min_level = serializers.FloatField(allow_null=True)
    max_level = serializers.FloatField(allow_null=True)


class DashboardSerializer(serializers.Serializer):
    generated_at = serializers.DateTimeField()
    date = serializers.DateField()
    is_today = serializers.BooleanField()
    offline_after_minutes = serializers.IntegerField()
    washroom = WashroomSerializer()
    people_counting = PeopleCountingSerializer()
    customer_satisfaction = CustomerSatisfactionSerializer()
    sensors = SensorCardSerializer(many=True)
    status_rules = StatusRuleOutSerializer(many=True)
