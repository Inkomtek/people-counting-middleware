"""Rule evaluation: called when data arrives (people threshold, washroom reading, rating) and every
minute by the scheduler (offline check, retries).

Callers never see an exception from here: a broken rule or channel must not stop syncing or the API.
"""

import logging
from datetime import timedelta

from django.utils import timezone

from core.models import DeviceList, SensorLog

from . import channels
from .models import AutomationLog, Channel, Recipient, Rule, RuleState

logger = logging.getLogger(__name__)

DEFAULT_MESSAGES = {
    Rule.PEOPLE_THRESHOLD: "{{device}} di {{location}} mencapai {{count}} pengunjung (batas {{threshold}}) pukul {{time}}. Mohon dibersihkan.",
    Rule.SENSOR_STATUS: "{{device}} di {{location}}: {{status}} ({{value}}) pukul {{time}}.",
    Rule.LOW_BATTERY: "Baterai {{device}} di {{location}} tinggal {{battery}}% (pukul {{time}}).",
    Rule.OFFLINE: "{{device}} di {{location}} tidak mengirim data sejak {{last_seen}}.",
    Rule.BAD_RATING: "Rating buruk ({{rating}}/5) di {{location}} pukul {{time}}. Komentar: {{comment}}",
}
CONTACT_CHANNEL = {"whatsapp": Channel.TYPE_WHATSAPP, "telegram": Channel.TYPE_TELEGRAM, "email": Channel.TYPE_EMAIL}


def device_context(device, **extra):
    scope = device.scope
    area = scope.area if scope else None
    site = area.site if area else None
    now = timezone.localtime()
    context = {
        "device": device.label, "device_id": device.device_id, "type": device.type,
        "scope": scope.name if scope else "", "area": area.name if area else "",
        "site": site.name if site else "", "client": site.client.name if site else "",
        "location": f"{area.name} · {scope.name}" if scope else "Belum diatur",
        "time": now.strftime("%H:%M"), "date": now.strftime("%Y-%m-%d"),
    }
    context.update({k: "" if v is None else str(v) for k, v in extra.items()})
    return context


def render(rule, context):
    template = rule.message_template or DEFAULT_MESSAGES.get(rule.condition, "{{device}}: {{status}}")
    return channels.fill(template, context, secrets=False)


# ---------- who receives ----------

def on_duty(device, level):
    """Active staff on duty for the device's Scope (level "scope") or Area (level "area")."""
    if device.scope_id is None:
        return []
    if level == "scope":
        staff = Recipient.objects.filter(scopes=device.scope_id)
    else:
        staff = Recipient.objects.filter(areas=device.scope.area_id)
    return [r for r in staff.filter(is_active=True).distinct() if r.is_on_duty()]


def _send_to_person(rule, device, person, context, tier):
    """Reach one person through the rule's person channels: one contact in priority order (falling
    back to the next on failure) or every contact. Returns True if anything got through."""
    by_type = {c.type: c for c in rule.person_channels.filter(is_active=True)}
    reachable = [(kind, address, by_type[CONTACT_CHANNEL[kind]]) for kind, address in person.contacts()
                 if CONTACT_CHANNEL[kind] in by_type]
    ctx = {**context, "recipient": person.name}
    if not reachable:
        AutomationLog.objects.create(rule=rule, device=device, recipient=person, tier=tier,
                                     status=AutomationLog.STATUS_SKIPPED, message=ctx["message"], context=ctx,
                                     note="Petugas tidak punya kontak untuk channel di rule ini")
        return False
    sent = False
    for index, (kind, address, channel) in enumerate(reachable):
        spec = {"method": channel.method, "head": channel.head, "body": channel.body,
                "email_subject": channel.email_subject, "email_body": channel.email_body}
        last = index == len(reachable) - 1
        ok = channels.deliver(channel, spec, address, ctx, rule=rule, device=device, recipient=person, tier=tier,
                              allow_retry=rule.contact_mode == Rule.CONTACT_ALL or last)
        sent = sent or ok
        if ok and rule.contact_mode == Rule.CONTACT_PRIORITY:
            break
    return sent


def _send_to_subchannels(rule, device, subchannels, context, tier):
    for sub in subchannels.filter(is_active=True, channel__is_active=True).select_related("channel"):
        channels.deliver(sub.channel, sub.effective(), sub.target, context, rule=rule, device=device,
                         subchannel=sub, tier=tier)


def dispatch(rule, device, context):
    """Send one occurrence: the rule's subchannels, plus (with notify_on_duty) the cleaners of the
    device's Scope and the supervisors of its Area at the same time. Nobody on duty: only logged."""
    context = {**context, "rule": rule.name}
    context["message"] = render(rule, context)
    _send_to_subchannels(rule, device, rule.subchannels, context, "subchannel")
    if not rule.notify_on_duty:
        return
    cleaners = on_duty(device, "scope")
    supervisors = [p for p in on_duty(device, "area") if p not in cleaners]
    for person in cleaners:
        _send_to_person(rule, device, person, context, "cleaner")
    for person in supervisors:
        _send_to_person(rule, device, person, context, "supervisor")
    if not cleaners and not supervisors:
        AutomationLog.objects.create(rule=rule, device=device, tier="cleaner", status=AutomationLog.STATUS_SKIPPED,
                                     message=context["message"], context=context,
                                     note="Tidak ada cleaner maupun supervisor di lokasi ini")


def process(rule, device, met, context):
    """Apply one evaluation of `rule` for `device` (met = condition true now)."""
    now = timezone.now()
    if rule.condition in Rule.ONE_SHOT:
        if met:
            dispatch(rule, device, context)
        return
    state, _ = RuleState.objects.get_or_create(rule=rule, device=device)
    if not met:
        if state.active:
            state.active = False
            state.save(update_fields=["active"])
        return
    cooled = rule.cooldown_minutes and state.last_sent_at and now - state.last_sent_at >= timedelta(minutes=rule.cooldown_minutes)
    if state.active and not cooled:
        return
    state.active, state.last_sent_at = True, now
    state.save(update_fields=["active", "last_sent_at"])
    dispatch(rule, device, context)


def _rules(*conditions):
    return Rule.objects.filter(is_active=True, condition__in=conditions).prefetch_related("person_channels")


def _safely(func):
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception:
            logger.exception("Automation failed in %s", func.__name__)
    wrapper.__name__ = func.__name__
    return wrapper


# ---------- entry points ----------

@_safely
def people_threshold(device, count, threshold):
    """A people counter reached its threshold (called by core.services.sync_device)."""
    for rule in _rules(Rule.PEOPLE_THRESHOLD):
        if rule.watches(device):
            process(rule, device, True, device_context(device, count=count, threshold=threshold, status="Batas tercapai"))


@_safely
def reading_received(reading):
    """A new washroom reading: status and battery rules."""
    device = reading.device
    context = device_context(device, status=reading.condition or "-", value="" if reading.level is None else f"{reading.level:g}",
                             battery=reading.battery)
    for rule in _rules(Rule.SENSOR_STATUS, Rule.LOW_BATTERY):
        if not rule.watches(device):
            continue
        if rule.condition == Rule.SENSOR_STATUS:
            names = {c.lower() for c in rule.conditions or []}
            met = (reading.severity in (rule.severities or [])) or (reading.condition or "").lower() in names
        else:
            met = reading.battery is not None and rule.battery_below is not None and reading.battery < rule.battery_below
        process(rule, device, met, context)


@_safely
def rating_received(response):
    from dashboard.queries import rating_level

    if rating_level(response.rating) != "bad":
        return
    device = response.device
    context = device_context(device, rating=response.rating, comment=response.comment or "-", status="Rating buruk")
    for rule in _rules(Rule.BAD_RATING):
        if rule.watches(device):
            process(rule, device, True, context)


def check_offline(now=None):
    """Offline rules: people counters whose latest ZK request failed; washroom sensors silent too long."""
    from washroom.models import SensorReading

    now = now or timezone.now()
    for rule in _rules(Rule.OFFLINE):
        for device in rule.matching_devices().exclude(type="satisfaction").select_related("scope__area__site__client"):
            if device.type == DeviceList.TYPE_PEOPLE:
                latest = SensorLog.objects.filter(device=device).order_by("-time").first()
                met = latest is not None and latest.status == SensorLog.STATUS_OFFLINE
                last_seen = latest.time if latest else None
            else:
                last_seen = SensorReading.objects.filter(device=device).order_by("-time").values_list("time", flat=True).first()
                # A device that never sent anything is "not installed yet", not offline.
                met = last_seen is not None and now - last_seen > timedelta(minutes=rule.offline_minutes)
            seen = timezone.localtime(last_seen).strftime("%d %b %H:%M") if last_seen else "-"
            process(rule, device, met, device_context(device, status="Offline", last_seen=seen))


@_safely
def tick():
    """Scheduler job (every minute): offline checks and retries."""
    check_offline()
    channels.retry_due()


def test_send(subchannel, user_note=""):
    """Admin action: send a sample message to one subchannel."""
    context = {**channels.SAMPLE_CONTEXT, "message": f"Pesan uji dari Automation{user_note}"}
    channels.deliver(subchannel.channel, subchannel.effective(), subchannel.target, context,
                     subchannel=subchannel, tier="test", allow_retry=False)


def test_send_person(rule_channels, person):
    """Admin action: sample message to a staff member through every channel matching their contacts."""
    context = {**channels.SAMPLE_CONTEXT, "message": f"Pesan uji untuk {person.name}", "recipient": person.name}
    by_type = {c.type: c for c in rule_channels}
    for kind, address in person.contacts():
        channel = by_type.get(CONTACT_CHANNEL[kind])
        if channel:
            spec = {"method": channel.method, "head": channel.head, "body": channel.body,
                    "email_subject": channel.email_subject, "email_body": channel.email_body}
            channels.deliver(channel, spec, address, context, recipient=person, tier="test", allow_retry=False)

