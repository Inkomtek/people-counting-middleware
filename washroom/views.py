from django.utils.dateparse import parse_datetime
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, inline_serializer
from rest_framework import generics, serializers, status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from core.models import DeviceList

from .models import READING_TYPES, CustomerResponse, SensorReading
from .serializers import (
    CustomerResponseInSerializer,
    CustomerResponseOutSerializer,
    ReadingInSerializer,
    ReadingOutSerializer,
)
from .services import store_customer_responses, store_readings

MAX_BATCH = 500

ERROR_RESPONSE = inline_serializer(
    "ErrorResponse",
    {"status": serializers.CharField(default="error"), "message": serializers.CharField(),
     "errors": serializers.ListField(child=serializers.DictField(), required=False)},
)


def _error(message, http_status, errors=None):
    body = {"status": "error", "message": message}
    if errors is not None:
        body["errors"] = errors
    return Response(body, status=http_status)


def _validate_batch(request, serializer_class):
    """Accept one JSON object or a list of objects. Returns (items, error_response)."""
    data = request.data
    many = isinstance(data, list)
    items = data if many else [data]
    if not items or not all(isinstance(item, dict) for item in items):
        return None, many, _error("Body harus JSON object atau list of object", status.HTTP_400_BAD_REQUEST)
    if len(items) > MAX_BATCH:
        return None, many, _error(f"Maksimal {MAX_BATCH} data per request", status.HTTP_400_BAD_REQUEST)

    validated, errors = [], []
    for index, item in enumerate(items):
        serializer = serializer_class(data=item)
        if serializer.is_valid():
            validated.append({**serializer.validated_data, "payload": item})
        else:
            errors.append({"index": index, "errors": serializer.errors})
    if errors:
        # All or nothing: a batch with any invalid item stores nothing.
        return None, many, _error("Data tidak valid", status.HTTP_400_BAD_REQUEST, errors)
    return validated, many, None


LOCATION_PARAMETERS = [
    OpenApiParameter("device_id", str),
    OpenApiParameter("building", str, description='e.g. "GRAHA ISS BINTARO"'),
    OpenApiParameter("floor", str, description='e.g. "2"'),
    OpenApiParameter("gender", str, enum=[value for value, _ in DeviceList.GENDER_CHOICES]),
    OpenApiParameter("time_from", str, description="ISO 8601, inclusive"),
    OpenApiParameter("time_to", str, description="ISO 8601, inclusive"),
]


def _filter(queryset, params):
    """Filters shared by both list endpoints; location filters follow the dashboard's toilet fields."""
    device_id = params.get("device_id") or params.get("deviceId")
    if device_id:
        queryset = queryset.filter(device_id=device_id)
    for field in ("building", "floor", "gender"):
        if params.get(field):
            queryset = queryset.filter(**{f"device__{field}": params[field]})
    if parse_datetime(params.get("time_from") or ""):
        queryset = queryset.filter(time__gte=parse_datetime(params["time_from"]))
    if parse_datetime(params.get("time_to") or ""):
        queryset = queryset.filter(time__lte=parse_datetime(params["time_to"]))
    return queryset


def _created(serializer_class, objects, many):
    data = serializer_class(objects, many=True).data
    return Response({"status": "success", "data": data if many else data[0]}, status=status.HTTP_201_CREATED)


class ReadingListCreateView(generics.ListAPIView):
    """POST sensor readings; GET their history."""

    permission_classes = [IsAuthenticated]
    serializer_class = ReadingOutSerializer

    def get_queryset(self):
        queryset = _filter(SensorReading.objects.select_related("device"), self.request.query_params)
        if self.request.query_params.get("type"):
            queryset = queryset.filter(device__type=self.request.query_params["type"])
        return queryset.order_by("-time", "-id")

    @extend_schema(
        summary="List sensor readings (newest first)",
        parameters=[*LOCATION_PARAMETERS, OpenApiParameter("deviceId", str, description="Same as device_id"),
                    OpenApiParameter("type", str, enum=READING_TYPES)],
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(
        summary="Send sensor readings",
        description=(
            "Raw data format of the sensor team (Washroom Dashboard Raw Data Documentation v1.0). Send one "
            "reading as a JSON object, or up to 500 as a JSON list (all or nothing). `deviceId` must be "
            "registered in Admin (Device list); its type there (soap, toilet-paper, tissue, trash, ammonia) is "
            "the sensor type. `value` and `status` are stored as sent. `id` is unique per device: a reading "
            "already stored is skipped (counted in `duplicates`), so resending is safe. 201 when at least one "
            "reading is new, 200 when all were duplicates."
        ),
        request=ReadingInSerializer(many=True),
        responses={
            (201, "application/json"): inline_serializer("ReadingCreated", {
                "status": serializers.CharField(default="success"),
                "created": serializers.IntegerField(),
                "duplicates": serializers.IntegerField(),
                "data": ReadingOutSerializer(many=True),
            }),
            400: OpenApiResponse(ERROR_RESPONSE), 401: OpenApiResponse(ERROR_RESPONSE),
        },
    )
    def post(self, request):
        items, many, error = _validate_batch(request, ReadingInSerializer)
        if error:
            return error
        stored, created = store_readings(items, request.user)
        data = ReadingOutSerializer(stored, many=True).data
        return Response(
            {"status": "success", "created": created, "duplicates": len(stored) - created,
             "data": data if many else data[0]},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class CustomerResponseListCreateView(generics.ListAPIView):
    """POST customer satisfaction (rating) presses; GET their history."""

    permission_classes = [IsAuthenticated]
    serializer_class = CustomerResponseOutSerializer

    def get_queryset(self):
        queryset = CustomerResponse.objects.select_related("device")
        return _filter(queryset, self.request.query_params).order_by("-time", "-id")

    @extend_schema(summary="List customer responses (newest first)", parameters=LOCATION_PARAMETERS)
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(
        summary="Send customer responses",
        description=(
            "One rating press (1-5) as a JSON object, or up to 500 as a JSON list (all or nothing). The "
            "`device_id` must already exist in Admin `DeviceList` and must be a `satisfaction` device. Unknown or "
            "mismatched IDs are rejected."
        ),
        request=CustomerResponseInSerializer(many=True),
        responses={
            201: inline_serializer("CustomerResponseCreated", {
                "status": serializers.CharField(default="success"),
                "data": CustomerResponseOutSerializer(many=True),
            }),
            400: OpenApiResponse(ERROR_RESPONSE), 401: OpenApiResponse(ERROR_RESPONSE),
        },
    )
    def post(self, request):
        items, many, error = _validate_batch(request, CustomerResponseInSerializer)
        if error:
            return error
        return _created(CustomerResponseOutSerializer, store_customer_responses(items, request.user), many)
