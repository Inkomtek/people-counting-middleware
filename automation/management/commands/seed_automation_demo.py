"""Demo data for Admin › Automation: channels, subchannels, staff, rules and 7 days of logs.

Every demo channel points at the local dummy inbox (/dummy/inbox/<kind>/), so test sends really succeed
without any Telegram/WhatsApp/email account; "Telegram (gangguan)" answers 503 to show retries.
Everything is named "[DEMO] …" and `--cleanup` removes it all (the real Algospection rule is untouched).
"""

import random
import socket
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from automation import engine
from automation.models import AutomationLog, Channel, Recipient, Rule, Subchannel
from core.models import Area, DeviceList, Scope

PREFIX = "[DEMO] "
DOCKER_BASE_URL = "http://web.internal:8000"
LOCAL_BASE_URL = "http://127.0.0.1:8000"
CLEANERS = ["Budi", "Sari", "Joko", "Rina", "Agus", "Dewi", "Eko", "Fitri", "Hadi", "Lina", "Rudi", "Wati"]
SUPERVISORS = ["Pak Hendra", "Bu Maya", "Pak Taufik", "Bu Ratna"]


def default_base_url():
    try:
        socket.gethostbyname("web.internal")
    except OSError:
        return LOCAL_BASE_URL
    return DOCKER_BASE_URL


class Command(BaseCommand):
    help = "Create [DEMO] Automation channels, subchannels, staff, rules and ~150 log rows (7 days). --cleanup removes them."

    def add_arguments(self, parser):
        parser.add_argument("--cleanup", action="store_true", help="Delete every [DEMO] automation object and its logs")
        parser.add_argument("--base-url", help=f"Where the dummy inbox runs (default {DOCKER_BASE_URL} in Docker, else {LOCAL_BASE_URL})")
        parser.add_argument("--logs", type=int, default=150, help="Number of demo log rows")
        parser.add_argument("--seed", type=int, default=9)

    def handle(self, *args, **options):
        with transaction.atomic():
            removed = self.cleanup()
            if options["cleanup"]:
                self.stdout.write(self.style.SUCCESS(f"Removed {removed} demo automation object(s)."))
                return
            base = (options["base_url"] or default_base_url()).rstrip("/")
            rng = random.Random(options["seed"])
            subs, person_channels = self.channels(base)
            staff = self.staff(rng)
            rules = self.rules(subs, person_channels)
            logs = self.logs(rules, rng, options["logs"])
        self.stdout.write(self.style.SUCCESS(
            f"Demo automation: {len(person_channels) + 2} channels, {len(subs)} subchannels, {staff} staff, "
            f"{len(rules)} rules, {logs} log rows (inbox {base}/dummy/inbox/). Remove with --cleanup."
        ))

    # ---------- build ----------
    def cleanup(self):
        logs = AutomationLog.objects.filter(context__demo=True).delete()[0]
        count = logs
        for model in (Rule, Recipient, Channel):
            count += model.objects.filter(name__startswith=PREFIX).delete()[0]
        return count

    def channels(self, base):
        inbox = f"{base}/dummy/inbox"
        telegram = Channel.objects.create(name=PREFIX + "Telegram", type=Channel.TYPE_TELEGRAM,
                                          config={"token_env": "TELEGRAM_BOT_TOKEN", "base_url": f"{inbox}/telegram"},
                                          retry_count=2, retry_delay_minutes=5)
        down = Channel.objects.create(name=PREFIX + "Telegram (gangguan)", type=Channel.TYPE_TELEGRAM,
                                      config={"token_env": "TELEGRAM_BOT_TOKEN", "base_url": f"{inbox}/telegram-down"},
                                      retry_count=3, retry_delay_minutes=5)
        whatsapp = Channel.objects.create(name=PREFIX + "WhatsApp", type=Channel.TYPE_WHATSAPP,
                                          config={"base_url": f"{inbox}/whatsapp", "instance": "demo"},
                                          retry_count=2, retry_delay_minutes=10)
        email = Channel.objects.create(name=PREFIX + "Email", type=Channel.TYPE_EMAIL, retry_count=1)
        webhook = Channel.objects.create(name=PREFIX + "Webhook", type=Channel.TYPE_WEBHOOK, retry_count=1)
        subs = {
            "tg_cleaning": Subchannel.objects.create(channel=telegram, name="Grup Cleaning Graha ISS", target="-1001234567890"),
            "tg_down": Subchannel.objects.create(channel=down, name="Grup Teknisi", target="-1009876543210"),
            "wa_group": Subchannel.objects.create(channel=whatsapp, name="Grup WA Petugas", target="120363012345678901@g.us"),
            "wa_spv": Subchannel.objects.create(
                channel=whatsapp, name="Grup WA Supervisor", target="120363098765432109@g.us",
                body={"number": "{{target}}", "text": "*[{{rule}}]* {{message}}"},  # override: bold rule name
            ),
            "email_spv": Subchannel.objects.create(channel=email, name="Supervisor & Koordinator",
                                                   target="supervisor@iss.example, koordinator@iss.example"),
            "webhook": Subchannel.objects.create(channel=webhook, name="Sistem internal", target=f"{inbox}/webhook/"),
        }
        return subs, [telegram, whatsapp, email]

    def staff(self, rng):
        count, names = 0, iter(CLEANERS * 10)
        for scope in Scope.objects.select_related("area").order_by("pk"):
            for _ in range(2):
                name = next(names)
                person = Recipient.objects.create(
                    name=f"{PREFIX}{name} ({scope.name})",
                    whatsapp_number=f"62812{rng.randint(10000000, 99999999)}" if rng.random() > .25 else "",
                    telegram_chat_id=str(rng.randint(100000000, 999999999)) if rng.random() > .5 else "",
                    email=f"{name.lower()}.{scope.pk}@iss.example" if rng.random() > .3 else "",
                    contact_priority=rng.choice([["whatsapp", "telegram", "email"], ["telegram", "whatsapp", "email"]]),
                )
                if not person.contacts():
                    person.email = f"{name.lower()}.{scope.pk}@iss.example"
                    person.save()
                person.scopes.add(scope)
                count += 1
        for index, area in enumerate(Area.objects.filter(scopes__isnull=False).distinct().order_by("pk")):
            boss = Recipient.objects.create(
                name=f"{PREFIX}{SUPERVISORS[index % len(SUPERVISORS)]} ({area.name})",
                telegram_chat_id=str(rng.randint(100000000, 999999999)), email=f"spv{area.pk}@iss.example",
                contact_priority=["telegram", "email"],
            )
            boss.areas.add(area)
            count += 1
        return count

    def rules(self, subs, person_channels):
        def rule(name, condition, targets=(), **fields):
            created = Rule.objects.create(name=PREFIX + name, condition=condition, **fields)
            created.subchannels.set([subs[t] for t in targets])
            if fields.get("notify_on_duty"):
                created.person_channels.set(person_channels)
            return created

        return [
            rule("Sabun & tisu habis", Rule.SENSOR_STATUS, ["wa_group"], severities=["critical"],
                 device_types=["soap", "toilet-paper", "tissue"], notify_on_duty=True),
            rule("Sampah penuh", Rule.SENSOR_STATUS, ["tg_cleaning"], severities=["critical", "warning"],
                 device_types=["trash"], notify_on_duty=True,
                 message_template="🗑️ {{device}} di {{location}} {{status}} ({{value}}%) pukul {{time}}. Mohon dikosongkan."),
            rule("Amonia bahaya", Rule.SENSOR_STATUS, ["webhook", "wa_spv"], severities=["critical"], device_types=["ammonia"],
                 notify_on_duty=True, contact_mode=Rule.CONTACT_ALL),
            rule("Baterai lemah", Rule.LOW_BATTERY, ["email_spv"], battery_below=20, cooldown_minutes=24 * 60),
            rule("Sensor offline", Rule.OFFLINE, ["tg_down"], offline_minutes=60),
            rule("Rating buruk", Rule.BAD_RATING, ["wa_group"], notify_on_duty=True),
        ]

    # ---------- history ----------
    def logs(self, rules, rng, total):
        now = timezone.now()
        made = 0
        plans = []
        for rule in rules:
            devices = list(rule.matching_devices().select_related("scope__area__site__client"))
            if devices:
                plans.append((rule, devices))
        if not plans:
            return 0
        for _ in range(total):
            rule, devices = rng.choice(plans)
            device = rng.choice(devices)
            when = now - timedelta(days=rng.uniform(0, 7))
            status_text = {"soap": "Habis", "toilet-paper": "Habis", "tissue": "Habis", "trash": "Penuh",
                           "ammonia": "Bahaya"}.get(device.type, "Offline")
            context = engine.device_context(device, status=status_text, value=rng.choice([0, 5, 92, 95, 30]),
                                            battery=rng.randint(5, 19), rating=1, comment="Bau dan kotor",
                                            last_seen=timezone.localtime(when - timedelta(hours=2)).strftime("%d %b %H:%M"))
            context["time"] = timezone.localtime(when).strftime("%H:%M")
            context.update(rule=rule.name, demo=True)
            context["message"] = engine.render(rule, context)
            made += self.log_occurrence(rule, device, context, when, rng)
            if made >= total:
                break
        return made

    def log_occurrence(self, rule, device, context, when, rng):
        rows = []
        for sub in rule.subchannels.select_related("channel"):
            rows += self.attempts(rule, device, context, when, sub.channel, sub.target, rng, subchannel=sub)
        if rule.notify_on_duty:
            # Cleaners of the Scope and supervisors of the Area get the same message at the same time.
            for level, tier in (("scope", "cleaner"), ("area", "supervisor")):
                for person in engine.on_duty(device, level)[:2]:
                    channel = self.person_channel(rule, person)
                    if channel:
                        rows += self.attempts(rule, device, {**context, "recipient": person.name},
                                              when + timedelta(seconds=2), channel[0], channel[1], rng,
                                              recipient=person, tier=tier)
        return len(rows)

    @staticmethod
    def person_channel(rule, person):
        by_type = {c.type: c for c in rule.person_channels.all()}
        kinds = {"whatsapp": Channel.TYPE_WHATSAPP, "telegram": Channel.TYPE_TELEGRAM, "email": Channel.TYPE_EMAIL}
        for kind, address in person.contacts():
            if kinds[kind] in by_type:
                return by_type[kinds[kind]], address
        return None

    @staticmethod
    def attempts(rule, device, context, when, channel, target, rng, subchannel=None, recipient=None, tier="subchannel"):
        """One send, plus retries when it failed (the dummy "gangguan" channel always fails)."""
        always_down = "gangguan" in channel.name
        rows, attempt, at = [], 1, when
        while True:
            ok = not always_down and rng.random() > .12
            last = attempt > channel.retry_count or channel.type == Channel.TYPE_ALGOSPECTION
            status = AutomationLog.STATUS_SUCCESS if ok else (AutomationLog.STATUS_FAILED if last else AutomationLog.STATUS_RETRY)
            log = AutomationLog.objects.create(
                rule=rule, device=device, channel=channel, subchannel=subchannel, recipient=recipient,
                channel_type=channel.type, tier=tier, attempt=attempt, status=status if ok or last else AutomationLog.STATUS_FAILED,
                target=target.split(",")[0].strip(), message=context["message"], context=context,
                request={"method": channel.method, "head": channel.head, "body": channel.body, "demo": True},
                response={"ok": True} if ok else {"ok": False, "error": "service unavailable (dummy)"},
                response_status="200 OK" if ok else "503 Service Unavailable",
            )
            AutomationLog.objects.filter(pk=log.pk).update(time=at)
            rows.append(log)
            if ok or last:
                return rows
            attempt += 1
            at += timedelta(minutes=channel.retry_delay_minutes)
