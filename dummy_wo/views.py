"""Dummy stand-in for Algospection's api_iot.php until real access is available.

Replies in the same shape as the real server (captured 2026-10-06):
success  -> 200 [{"error": 0, "results": [{"WO_NO": "280268", "EFCTV_DT": ..., "EXP_DT": ..., ...}]}]
failure  -> 4xx [{"error": 1, "message": "..."}]  (the real error shape is not known yet; best guess)
"""

import json
from datetime import timedelta

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import WorkOrder

REQUIRED_FIELDS = ("token", "INSTANCE", "LOC_ID", "ASSET_ID", "REQ_TYP", "REQ_DESC")


# Work Orders created by the real server stay valid for three days.
WO_VALIDITY = timedelta(days=3)


def _reply(payload, accepted, status, message):
    order = WorkOrder.objects.create(
        payload=payload, accepted=accepted, response_status=status, message=message
    )
    if not accepted:
        return JsonResponse([{"error": 1, "message": message}], status=status, safe=False)
    now = timezone.localtime()
    return JsonResponse([{
        "error": 0,
        "results": [{
            "WO_NO": order.wo_id,
            "EXP_DT": (now + WO_VALIDITY).strftime("%Y-%m-%d %H:%M:%S"),
            "USR_NM": None,
            "TECH_NM": None,
            "EFCTV_DT": now.strftime("%Y-%m-%d %H:%M:%S"),
            "PRPRTY_NM": None,
            "USR_EMAIL": None,
            "LOC_ID_DESC": payload.get("LOC_ID"),
        }],
    }], status=status, safe=False)


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
