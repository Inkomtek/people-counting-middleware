"""Sending one message to one destination, per channel type, and logging the attempt.

Head/body/email text hold {{placeholders}}: event data ({{message}}, {{device}}, {{target}}, …) and
secrets ({{env:NAME}} plus the legacy {{algospection_token}}, {{client_id}}, {{client_secret}}).
Secrets are filled in only for the actual request; logs and NotificationLog keep the unfilled text.
"""

import json
import os
import re
from datetime import timedelta

import requests
from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

from core.models import NotificationLog

from .models import AutomationLog, Channel

PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_:.]+)\s*\}\}")
MASK = "••••"

DEFAULTS = {
    Channel.TYPE_TELEGRAM: {
        "config": {"token_env": "TELEGRAM_BOT_TOKEN"},
        "body": {"chat_id": "{{target}}", "text": "{{message}}"},
    },
    Channel.TYPE_WHATSAPP: {
        "config": {"base_url": "http://evolution:8080", "instance": "washroom"},
        "head": {"apikey": "{{env:EVOLUTION_API_KEY}}"},
        "body": {"number": "{{target}}", "text": "{{message}}"},
    },
    Channel.TYPE_WEBHOOK: {"body": {"text": "{{message}}"}},
    Channel.TYPE_EMAIL: {"email_subject": "[Washroom] {{status}} · {{device}}", "email_body": "{{message}}"},
    Channel.TYPE_ALGOSPECTION: {},
}

# Placeholders the admin can use (shown in the Admin preview with sample values).
SAMPLE_CONTEXT = {
    "message": "Sabun Wastafel di Gedung A · Floor 10 - Toilet Pria West: Habis pukul 13:05",
    "rule": "Sabun habis", "device": "Sabun Wastafel", "device_id": "SOAP-01", "type": "soap",
    "location": "Gedung A · Floor 10 - Toilet Pria West", "client": "BCA", "site": "Thamrin", "area": "Gedung A",
    "scope": "Floor 10 - Toilet Pria West", "status": "Habis", "value": "0", "battery": "12", "count": "20",
    "threshold": "20", "rating": "1", "time": "13:05", "date": "2026-10-09", "recipient": "Budi",
    "target": "120363000000000000@g.us",
}
LEGACY_SECRETS = {"algospection_token": "ALGOSPECTION_TOKEN", "client_id": "ZK_CLIENT_ID", "client_secret": "ZK_CLIENT_SECRET"}


def secret(name):
    """A secret from settings (if defined there) or the environment / .env."""
    value = getattr(settings, name, None)
    return str(value) if value not in (None, "") else os.getenv(name, "")


def _replace(text, context, secrets):
    def sub(match):
        key = match.group(1)
        if key.startswith("env:"):
            return secret(key[4:]) if secrets else MASK
        if key in LEGACY_SECRETS:
            return secret(LEGACY_SECRETS[key]) if secrets else MASK
        if key in context:
            return str(context[key])
        return match.group(0)  # unknown placeholder: left as is
    return PLACEHOLDER.sub(sub, text)


def fill(value, context, secrets=True):
    """Fill placeholders in a string or anywhere inside a JSON value."""
    if isinstance(value, str):
        return _replace(value, context, secrets)
    if isinstance(value, dict):
        return {k: fill(v, context, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [fill(v, context, secrets) for v in value]
    return value


def unknown_placeholders(*values):
    """Placeholder names that are neither event data nor secrets (for an Admin warning)."""
    names = set()
    for value in values:
        names |= set(PLACEHOLDER.findall(json.dumps(value) if not isinstance(value, str) else value))
    return sorted(n for n in names if not n.startswith("env:") and n not in LEGACY_SECRETS and n not in SAMPLE_CONTEXT)


def _response_json(response):
    try:
        return response.json()
    except ValueError:
        return {"text": response.text[:2000]}


def _url(channel, target, secrets):
    config = channel.config or {}
    if channel.type == Channel.TYPE_TELEGRAM:
        token = secret(config.get("token_env", "TELEGRAM_BOT_TOKEN")) if secrets else MASK
        return f"{config.get('base_url', 'https://api.telegram.org').rstrip('/')}/bot{token}/sendMessage"
    if channel.type == Channel.TYPE_WHATSAPP:
        return f"{config.get('base_url', 'http://evolution:8080').rstrip('/')}/message/sendText/{config.get('instance', 'washroom')}"
    return target  # Algospection / Webhook: the destination is a URL


def split_targets(channel, target):
    """WhatsApp numbers and email addresses may be listed with commas; other types use one target."""
    if channel.type in (Channel.TYPE_WHATSAPP, Channel.TYPE_TELEGRAM):
        return [t.strip() for t in target.split(",") if t.strip()]
    return [target.strip()]


def _send(channel, spec, target, context, device):
    """Perform the request. Returns (ok, response_status, response, request_for_log)."""
    context = {**context, "target": target}
    if channel.type == Channel.TYPE_EMAIL:
        subject = fill(spec["email_subject"], context)
        body = fill(spec["email_body"], context)
        addresses = [a.strip() for a in target.split(",") if a.strip()]
        request_log = {"to": addresses, "subject": spec["email_subject"], "body": spec["email_body"]}
        try:
            send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, addresses)
        except Exception as exc:  # SMTP errors come in many classes
            return False, "ERROR", {"error": str(exc)}, request_log
        return True, "SENT", {"to": addresses}, request_log

    url = _url(channel, target, secrets=True)
    head = fill(spec["head"] or {}, context) or None
    body = fill(spec["body"] or {}, context)
    request_log = {"method": spec["method"], "url": _url(channel, target, secrets=False), "head": spec["head"], "body": spec["body"]}
    method = "POST" if channel.type != Channel.TYPE_WEBHOOK else spec["method"]
    try:
        if method == "GET":
            response = requests.get(url, params=body, headers=head, timeout=settings.HTTP_TIMEOUT)
        elif method == "PUT":
            response = requests.put(url, json=body, headers=head, timeout=settings.HTTP_TIMEOUT)
        else:
            response = requests.post(url, json=body, headers=head, timeout=settings.HTTP_TIMEOUT)
        status = f"{response.status_code} {response.reason}"
        payload = _response_json(response)
        ok = 200 <= response.status_code < 300
    except requests.RequestException as exc:
        status, payload, ok = "ERROR", {"error": str(exc)}, False

    if channel.type == Channel.TYPE_ALGOSPECTION and device is not None:
        # Keep the Work Order history the dashboard reads (success / WO number are derived on save).
        log = NotificationLog.objects.create(
            device=device, endpoint_url=target, body=spec["body"], head=spec["head"], response=payload,
            response_status=status[:50],
        )
        ok = log.success
    return ok, status[:50], payload, request_log


def deliver(channel, spec, target, context, *, rule=None, device=None, subchannel=None, recipient=None,
            tier="subchannel", attempt=1, allow_retry=True):
    """Send to every target in `target` and log each attempt. Failed sends with retries left are
    scheduled (never for Algospection, nor when `allow_retry` is off because the caller falls back to
    another contact). Returns True when every target succeeded."""
    all_ok = True
    for one in split_targets(channel, target):
        ok, status, payload, request_log = _send(channel, spec, one, context, device)
        retry = (not ok and allow_retry and channel.type != Channel.TYPE_ALGOSPECTION
                 and attempt <= channel.retry_count)
        AutomationLog.objects.create(
            rule=rule, device=device, channel=channel, subchannel=subchannel, recipient=recipient,
            channel_type=channel.type, tier=tier, attempt=attempt,
            status=AutomationLog.STATUS_SUCCESS if ok else (AutomationLog.STATUS_RETRY if retry else AutomationLog.STATUS_FAILED),
            next_retry_at=timezone.now() + timedelta(minutes=channel.retry_delay_minutes) if retry else None,
            target=one, message=context.get("message", ""), request={**request_log, "spec": spec},
            context=context, response=payload, response_status=status,
        )
        all_ok = all_ok and ok
    return all_ok


def retry_due(now=None):
    """Resend every scheduled retry whose time has come (scheduler job). Returns how many were resent."""
    now = now or timezone.now()
    due = AutomationLog.objects.filter(status=AutomationLog.STATUS_RETRY, next_retry_at__lte=now).select_related("channel")
    count = 0
    for log in due:
        log.status, log.next_retry_at = AutomationLog.STATUS_FAILED, None
        log.save(update_fields=["status", "next_retry_at"])
        if log.channel is None or not log.channel.is_active:
            continue
        spec = (log.request or {}).get("spec") or {}
        deliver(log.channel, spec, log.target, log.context, rule=log.rule, device=log.device,
                subchannel=log.subchannel, recipient=log.recipient, tier=log.tier, attempt=log.attempt + 1)
        count += 1
    return count
