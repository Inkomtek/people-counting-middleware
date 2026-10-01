"""Dummy stand-in for Algospection's api_iot.php until real access is available."""

import json

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .models import WorkOrder

REQUIRED_FIELDS = ("token", "INSTANCE", "LOC_ID", "ASSET_ID", "REQ_TYP", "REQ_DESC")


def _reply(payload, accepted, status, message):
    order = WorkOrder.objects.create(
        payload=payload, accepted=accepted, response_status=status, message=message
    )
    body = {"status": "success" if accepted else "error", "message": message}
    if accepted:
        body["wo_id"] = order.wo_id
    return JsonResponse(body, status=status)


@csrf_exempt
@require_POST
def api_iot(request):
    try:
        payload = json.loads(request.body)
    except ValueError:
        return _reply({"raw": request.body.decode(errors="replace")[:5000]}, False, 400, "Body harus berupa JSON")
    if not isinstance(payload, dict):
        return _reply({"raw": payload}, False, 400, "Body harus berupa JSON object")

    if payload.get("token") != settings.DUMMY_WO_TOKEN:
        return _reply(payload, False, 401, "Token tidak valid")

    missing = [field for field in REQUIRED_FIELDS if not payload.get(field)]
    if missing:
        return _reply(payload, False, 400, f"Field wajib kosong: {', '.join(missing)}")

    return _reply(payload, True, 200, "Work Order berhasil dibuat")
