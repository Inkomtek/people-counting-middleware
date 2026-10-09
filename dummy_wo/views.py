"""Dummy stand-in for Algospection's api_iot.php until real access is available.

Replies in the same shape as the real server (captured 2026-10-06):
success  -> 200 [{"error": 0, "results": [{"WO_NO": "280268", "EFCTV_DT": ..., "EXP_DT": ..., ...}]}]
failure  -> 4xx [{"error": 1, "message": "..."}]  (the real error shape is not known yet; best guess)
"""

import json
import re
from datetime import timedelta

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .models import InboxMessage, WorkOrder

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
@require_http_methods(["GET", "POST"])
def api_iot(request):
    if request.method == "GET":
        # Opening the URL in a browser is a health check, not a Work Order (the real server takes POST only).
        return JsonResponse({
            "status": "ok",
            "message": "Dummy Work Order API aktif. Kirim POST JSON berisi: " + ", ".join(REQUIRED_FIELDS),
            "required_fields": list(REQUIRED_FIELDS),
        })
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


SECRET_HEADERS = ("apikey", "authorization", "x-api-key")


@csrf_exempt
def inbox(request, kind, rest=""):
    """Dummy receiver for Automation demos: accepts any message and stores it. A `kind` ending in
    "-down" answers 503 (to demo failures and retries). Telegram bot tokens in the path are masked."""
    try:
        body = json.loads(request.body or b"null")
    except ValueError:
        body = {"raw": request.body.decode(errors="replace")[:2000]}
    headers = {k: ("••••" if k.lower() in SECRET_HEADERS else v) for k, v in request.headers.items()
               if k.lower() in SECRET_HEADERS or k.lower() == "content-type"}
    path = re.sub(r"bot[^/]+", "bot••••", rest)
    status = 503 if kind.endswith("-down") else 200
    InboxMessage.objects.create(kind=kind, path=path[:300], method=request.method, headers=headers, body=body,
                                response_status=status)
    if status != 200:
        return JsonResponse({"ok": False, "error": "service unavailable (dummy)"}, status=status)
    return JsonResponse({"ok": True, "result": {"message_id": InboxMessage.objects.latest("pk").pk}})
