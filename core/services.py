"""Sync engine: fetch ZK events, count traffic, and dispatch Work Orders."""

import json
import logging
from datetime import datetime

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import DeviceList, Endpoint, EventLog, NotificationLog, SensorLog

logger = logging.getLogger(__name__)

ZK_SUCCESS_CODE = "00000000"
EVENT_TYPE_CROSS_LINE = 3
COUNTED_EVENT_TYPES = {"in"}
COUNTED_RECOGNITION_TARGET = "Cross Line"
# Safety cap so a long outage cannot page through the device's whole history in one cycle.
MAX_PAGES = 50


class ZKError(Exception):
    pass


def _fill_template(value):
    """Replace {{...}} placeholders in an Endpoint head/body with secrets from settings."""
    raw = json.dumps(value)
    raw = raw.replace("{{client_id}}", settings.ZK_CLIENT_ID)
    raw = raw.replace("{{client_secret}}", settings.ZK_CLIENT_SECRET)
    raw = raw.replace("{{algospection_token}}", settings.ALGOSPECTION_TOKEN)
    return json.loads(raw)


def _response_json(response):
    try:
        return response.json()
    except ValueError:
        return {"raw": response.text[:5000]}


class ZKClient:
    def __init__(self):
        self.token_endpoint = Endpoint.objects.get(type=Endpoint.TYPE_TOKEN)
        self.event_endpoint = Endpoint.objects.get(type=Endpoint.TYPE_EVENT)
        self.token = None

    def refresh_token(self):
        url = self.token_endpoint.url
        try:
            response = requests.post(
                url,
                json=_fill_template(self.token_endpoint.body),
                headers=self.token_endpoint.head or None,
                timeout=settings.HTTP_TIMEOUT,
            )
        except requests.RequestException as exc:
            SensorLog.objects.create(
                status=SensorLog.STATUS_OFFLINE, response={"error": str(exc)}, endpoint_url=url
            )
            raise ZKError(f"Token request failed: {exc}") from exc

        data = _response_json(response)
        token = (data.get("data") or {}).get("access_token") if isinstance(data, dict) else None
        ok = response.ok and data.get("code") == ZK_SUCCESS_CODE and token
        # Never store the access token itself in the log.
        logged = {k: v for k, v in data.items() if k != "data"} if isinstance(data, dict) else data
        SensorLog.objects.create(
            status=SensorLog.STATUS_ONLINE if ok else SensorLog.STATUS_OFFLINE,
            response={"http_status": response.status_code, **logged},
            endpoint_url=url,
        )
        if not ok:
            raise ZKError(f"Token request rejected: HTTP {response.status_code} {logged}")
        self.token = token

    def _get_page(self, device, page, page_size=None):
        url = self.event_endpoint.url
        params = {
            "page": page,
            "pageSize": page_size or self.event_endpoint.body.get("pageSize", 10),
            "deviceId": device.device_id,
            "type": EVENT_TYPE_CROSS_LINE,
        }
        headers = {**(self.event_endpoint.head or {}), "Authorization": f"Bearer {self.token}"}
        try:
            response = requests.get(url, params=params, headers=headers, timeout=settings.HTTP_TIMEOUT)
        except requests.RequestException as exc:
            SensorLog.objects.create(
                status=SensorLog.STATUS_OFFLINE,
                response={"error": str(exc), "params": params},
                endpoint_url=url,
                device=device,
            )
            raise ZKError(f"Event request failed: {exc}") from exc

        data = _response_json(response)
        ok = response.ok and isinstance(data, dict) and data.get("code") == ZK_SUCCESS_CODE
        SensorLog.objects.create(
            status=SensorLog.STATUS_ONLINE if ok else SensorLog.STATUS_OFFLINE,
            response={"http_status": response.status_code, "params": params, **(data if isinstance(data, dict) else {"body": data})},
            endpoint_url=url,
            device=device,
        )
        return response, data, ok

    def fetch_events(self, device, page, page_size=None):
        """Return the event list for one page, refreshing the token once if it is rejected."""
        if self.token is None:
            self.refresh_token()
        response, data, ok = self._get_page(device, page, page_size)
        if not ok:
            # 401 or a non-success code (e.g. expired token): refresh and retry once.
            self.refresh_token()
            response, data, ok = self._get_page(device, page, page_size)
            if not ok:
                raise ZKError(f"Event request rejected: HTTP {response.status_code}")
        return (data.get("data") or {}).get("data") or []


def _parse_event(item, device):
    vo = item.get("eventLogVO") or {}
    try:
        height = int(vo.get("height"))
    except (TypeError, ValueError):
        height = None
    time = timezone.make_aware(datetime.strptime(item["eventTime"], "%Y-%m-%d %H:%M:%S"))
    return EventLog(
        id=item["id"],
        metadata_id=item["id"],
        time=time,
        track_id=vo.get("trackId") or "",
        event_type=vo.get("eventType") or "",
        recognition_target=vo.get("recognitionTarget") or "",
        height=height,
        device=device,
    )


def is_countable(event):
    return (
        event.event_type.lower() in COUNTED_EVENT_TYPES
        and event.recognition_target == COUNTED_RECOGNITION_TARGET
    )


def fetch_new_events(client, device):
    """Page through ZK events (newest first) until an already-stored event is found."""
    page_size = client.event_endpoint.body.get("pageSize", 10)
    new_events = []
    seen_ids = set()
    for page in range(1, MAX_PAGES + 1):
        items = client.fetch_events(device, page)
        if not items:
            break
        ids = [item["id"] for item in items]
        existing = set(EventLog.objects.filter(id__in=ids).values_list("id", flat=True))
        for item in items:
            if item["id"] in existing or item["id"] in seen_ids:
                continue
            seen_ids.add(item["id"])
            new_events.append(_parse_event(item, device))
        if existing or len(items) < page_size or not device.baseline_done:
            break
    else:
        logger.warning("Device %s: reached MAX_PAGES (%s), older new events may be skipped", device.device_id, MAX_PAGES)
    return new_events


def apply_daily_count(device, events):
    """Mark countable events and add them to device.current_count, resetting it on a new WIB day.

    Day boundaries follow each event's own time, oldest first. A leftover count from a previous
    day is discarded without a Work Order. Events older than count_date are not counted.
    Returns the number of events counted.
    """
    counted = 0
    for event in sorted(events, key=lambda e: e.time):
        event.counted = False
        if not is_countable(event):
            continue
        event_date = timezone.localtime(event.time).date()
        if device.count_date is None:
            device.count_date = event_date
        elif event_date > device.count_date:
            device.current_count = 0
            device.count_date = event_date
        elif event_date < device.count_date:
            continue
        event.counted = True
        device.current_count += 1
        counted += 1
    return counted


def sync_device(device, client):
    new_events = fetch_new_events(client, device)

    if not device.baseline_done:
        EventLog.objects.bulk_create(new_events, ignore_conflicts=True)
        device.baseline_done = True
        device.save(update_fields=["baseline_done"])
        logger.info("Device %s: stored %s baseline events (not counted)", device.device_id, len(new_events))
        return

    with transaction.atomic():
        device = DeviceList.objects.select_for_update().get(pk=device.pk)
        increment = apply_daily_count(device, new_events)
        EventLog.objects.bulk_create(new_events, ignore_conflicts=True)
        device.save(update_fields=["current_count", "count_date"])
    logger.info(
        "Device %s: %s new events, +%s counted, count %s/%s on %s",
        device.device_id, len(new_events), increment, device.current_count, device.maximum_trigger, device.count_date,
    )

    if device.current_count >= device.maximum_trigger:
        # Where the notification goes (Work Order, WhatsApp, cleaners, ...) is set by Automation rules.
        from automation import engine

        engine.people_threshold(device, device.current_count, device.maximum_trigger)
        # Reset even if every send failed (failures are only logged).
        DeviceList.objects.filter(pk=device.pk).update(current_count=0)
        logger.info("Device %s: threshold reached, notifications sent, count reset to 0", device.device_id)


BACKFILL_PAGE_SIZE = 100
BACKFILL_MAX_PAGES = 500


def backfill_events(client, device, start):
    """Store events newer than `start` that are missing from EventLog, for history only.

    Never touches current_count and never dispatches Work Orders. `counted` is set to whether
    the event matches the counting rule, so the daily recap stays consistent.
    Returns (stored, relabeled).
    """
    stored = 0
    for page in range(1, BACKFILL_MAX_PAGES + 1):
        items = client.fetch_events(device, page, BACKFILL_PAGE_SIZE)
        if not items:
            break
        events = [_parse_event(item, device) for item in items]
        in_range = [event for event in events if event.time >= start]
        existing = set(EventLog.objects.filter(id__in=[e.id for e in in_range]).values_list("id", flat=True))
        missing = [event for event in in_range if event.id not in existing]
        for event in missing:
            event.counted = is_countable(event)
        EventLog.objects.bulk_create(missing, ignore_conflicts=True)
        stored += len(missing)
        # Newest first: once a page reaches events older than `start`, everything after is older too.
        if len(in_range) < len(events) or len(items) < BACKFILL_PAGE_SIZE:
            break
    else:
        logger.warning("Device %s: backfill reached BACKFILL_MAX_PAGES (%s)", device.device_id, BACKFILL_MAX_PAGES)

    relabeled = 0
    for event in EventLog.objects.filter(device=device, time__gte=start):
        countable = is_countable(event)
        if event.counted != countable:
            event.counted = countable
            event.save(update_fields=["counted"])
            relabeled += 1
    return stored, relabeled


def sync_all():
    client = ZKClient()
    # Only People Counting devices read the ZK people-counting API; other modules need their own integration.
    # DEMO-* devices (seed_locations --demo, local dummies) are not real ZK sensors.
    for device in DeviceList.objects.filter(type=DeviceList.TYPE_PEOPLE).exclude(device_id__startswith="DEMO-"):
        try:
            sync_device(device, client)
        except ZKError as exc:
            logger.error("Device %s: sync failed: %s", device.device_id, exc)
