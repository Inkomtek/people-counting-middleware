from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema, inline_serializer
from rest_framework import generics, serializers, status
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from .authentication import ApiKeyAuthentication, IsApiClientOrStaff
from .models import CustomerResponse, SensorReading, Washroom
from .serializers import (
    CustomerResponseInSerializer,
    CustomerResponseOutSerializer,
    DashboardSerializer,
    ReadingInSerializer,
    ReadingOutSerializer,
    WashroomSerializer,
)
from .services import build_dashboard, store_customer_responses, store_readings

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


class ReadingListCreateView(generics.ListAPIView):
    """POST sensor readings; GET reading history for the dashboard."""

    permission_classes = [IsAuthenticated]
    serializer_class = ReadingOutSerializer

    def get_queryset(self):
        queryset = SensorReading.objects.select_related("device").order_by("-time", "-id")
        params = self.request.query_params
        if params.get("device_id"):
            queryset = queryset.filter(device_id=params["device_id"])
        if params.get("type"):
            queryset = queryset.filter(device__type=params["type"])
        if parse_datetime(params.get("time_from") or ""):
            queryset = queryset.filter(time__gte=parse_datetime(params["time_from"]))
        if parse_datetime(params.get("time_to") or ""):
            queryset = queryset.filter(time__lte=parse_datetime(params["time_to"]))
        return queryset

    @extend_schema(
        summary="List sensor readings (newest first)",
        parameters=[
            OpenApiParameter("device_id", str),
            OpenApiParameter("type", str, enum=["amonia", "soap", "tissue", "toilet_paper", "trash"]),
            OpenApiParameter("time_from", str, description="ISO 8601, inclusive"),
            OpenApiParameter("time_to", str, description="ISO 8601, inclusive"),
        ],
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(
        summary="Send sensor readings",
        description=(
            "Send one reading as a JSON object, or up to 500 as a JSON list. Unknown devices are registered "
            "automatically. The condition (Normal, Terisi, Hampir Habis, Habis, Penuh, ...) is computed by the "
            "server from the Status rules in Admin. A batch is all or nothing."
        ),
        request=ReadingInSerializer(many=True),
        responses={
            201: inline_serializer("ReadingCreated", {
                "status": serializers.CharField(default="success"),
                "data": ReadingOutSerializer(many=True),
            }),
            400: OpenApiResponse(ERROR_RESPONSE), 401: OpenApiResponse(ERROR_RESPONSE),
        },
    )
    def post(self, request):
        items, many, error = _validate_batch(request, ReadingInSerializer)
        if error:
            return error
        readings = store_readings(items, request.user)
        data = ReadingOutSerializer(readings, many=True).data
        return Response({"status": "success", "data": data if many else data[0]}, status=status.HTTP_201_CREATED)


class CustomerResponseListCreateView(generics.ListAPIView):
    """POST customer feedback (rating) presses; GET their history."""

    permission_classes = [IsAuthenticated]
    serializer_class = CustomerResponseOutSerializer

    def get_queryset(self):
        queryset = CustomerResponse.objects.order_by("-time", "-id")
        params = self.request.query_params
        if params.get("device_id"):
            queryset = queryset.filter(device_id=params["device_id"])
        if parse_datetime(params.get("time_from") or ""):
            queryset = queryset.filter(time__gte=parse_datetime(params["time_from"]))
        if parse_datetime(params.get("time_to") or ""):
            queryset = queryset.filter(time__lte=parse_datetime(params["time_to"]))
        return queryset

    @extend_schema(
        summary="List customer responses (newest first)",
        parameters=[
            OpenApiParameter("device_id", str),
            OpenApiParameter("time_from", str, description="ISO 8601, inclusive"),
            OpenApiParameter("time_to", str, description="ISO 8601, inclusive"),
        ],
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(
        summary="Send customer responses",
        description="One rating press (1-5) as a JSON object, or up to 500 as a JSON list. A batch is all or nothing.",
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
        responses = store_customer_responses(items, request.user)
        data = CustomerResponseOutSerializer(responses, many=True).data
        return Response({"status": "success", "data": data if many else data[0]}, status=status.HTTP_201_CREATED)


class WashroomListView(generics.ListAPIView):
    """Washrooms for the dashboard's building / floor / gender filters."""

    authentication_classes = [ApiKeyAuthentication, SessionAuthentication]
    permission_classes = [IsApiClientOrStaff]
    serializer_class = WashroomSerializer
    queryset = Washroom.objects.all()
    pagination_class = None

    @extend_schema(summary="List washrooms (building, floor, gender)")
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class DashboardView(APIView):
    # Our own dashboard page calls this with the Admin login session; external systems use X-API-Key.
    authentication_classes = [ApiKeyAuthentication, SessionAuthentication]
    permission_classes = [IsApiClientOrStaff]

    @extend_schema(
        summary="Dashboard for one washroom on one day",
        description=(
            "People counting, customer satisfaction and the state of each sensor type for one washroom. "
            "A card with available=false has no device of that type yet (shown as \"Segera Hadir\"). "
            "For a past date, sensors show their last reading of that day and current_count / online are null. "
            "Dates are Asia/Jakarta."
        ),
        parameters=[
            OpenApiParameter("washroom", int, description="Washroom id (default: the first one)"),
            OpenApiParameter("date", str, description="YYYY-MM-DD (default: today, max: today)"),
        ],
        responses={200: DashboardSerializer, 400: OpenApiResponse(ERROR_RESPONSE),
                   401: OpenApiResponse(ERROR_RESPONSE), 404: OpenApiResponse(ERROR_RESPONSE)},
    )
    def get(self, request):
        washroom_id = request.query_params.get("washroom")
        if washroom_id:
            washroom = Washroom.objects.filter(pk=washroom_id).first() if washroom_id.isdigit() else None
        else:
            washroom = Washroom.objects.first()
        if washroom is None:
            return _error("Washroom tidak ditemukan", status.HTTP_404_NOT_FOUND)

        raw_date = request.query_params.get("date")
        day = parse_date(raw_date) if raw_date else timezone.localdate()
        if day is None:
            return _error("Format date harus YYYY-MM-DD", status.HTTP_400_BAD_REQUEST)
        if day > timezone.localdate():
            return _error("Tanggal tidak boleh melewati hari ini", status.HTTP_400_BAD_REQUEST)
        return Response(DashboardSerializer(build_dashboard(washroom, day)).data)
