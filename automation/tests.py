from datetime import timedelta
from unittest import mock

import requests
from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import Area, Client, DeviceList, NotificationLog, Region, Scope, SensorLog, Site
from washroom.models import CustomerResponse, SensorReading

from . import channels, engine
from .models import AutomationLog, Channel, Recipient, Rule, RuleState, Subchannel

DEVICE_ID = "2069691213314072577"


def http(status=200, json_data=None):
    response = mock.Mock()
    response.status_code = status
    response.reason = "OK" if status < 300 else "Error"
    response.json.return_value = json_data if json_data is not None else {"ok": status < 300}
    response.text = ""
    return response


def posts(*responses):
    return mock.patch.object(channels.requests, "post", side_effect=list(responses))


@override_settings(TELEGRAM_BOT_TOKEN="tg-secret", EVOLUTION_API_KEY="evo-secret")
class AutomationTests(TestCase):
    def setUp(self):
        client = Client.objects.create(name="ISS")
        site = Site.objects.create(client=client, region=Region.objects.create(name="Banten"), name="Bintaro")
        self.area = Area.objects.create(site=site, name="Graha ISS")
        self.scope = Scope.objects.create(area=self.area, name="Floor 2 - Toilet Pria")
        self.other_scope = Scope.objects.create(area=self.area, name="Floor 3 - Toilet Pria")
        self.soap = DeviceList.objects.create(device_id="SOAP-1", type="soap", name="Sabun Wastafel", scope=self.scope)
        self.telegram = Channel.objects.create(name="Telegram", type=Channel.TYPE_TELEGRAM, retry_count=2,
                                               retry_delay_minutes=5)
        self.whatsapp = Channel.objects.create(name="WhatsApp", type=Channel.TYPE_WHATSAPP)
        self.email = Channel.objects.create(name="Email", type=Channel.TYPE_EMAIL)
        self.group = Subchannel.objects.create(channel=self.telegram, name="Grup Cleaning", target="-100123")

    def soap_rule(self, **kwargs):
        rule = Rule.objects.create(name="Sabun habis", condition=Rule.SENSOR_STATUS, severities=["critical"],
                                   device_types=["soap"], **kwargs)
        rule.subchannels.add(self.group)
        return rule

    def reading(self, severity="critical", condition="Habis", level=0, battery=80, device=None):
        reading = SensorReading.objects.create(device=device or self.soap, time=timezone.now(), level=level,
                                               battery=battery, condition=condition, severity=severity)
        engine.reading_received(reading)
        return reading

    # ---------- channels ----------
    def test_defaults_per_type_and_secrets_never_logged(self):
        self.assertEqual(self.whatsapp.head, {"apikey": "{{env:EVOLUTION_API_KEY}}"})
        self.assertEqual(self.telegram.body, {"chat_id": "{{target}}", "text": "{{message}}"})
        self.soap_rule()
        with posts(http()) as post:
            self.reading()
        url = post.call_args.args[0]
        self.assertEqual(url, "https://api.telegram.org/bottg-secret/sendMessage")
        self.assertEqual(post.call_args.kwargs["json"]["chat_id"], "-100123")
        self.assertIn("Sabun Wastafel di Graha ISS · Floor 2 - Toilet Pria: Habis (0)", post.call_args.kwargs["json"]["text"])
        log = AutomationLog.objects.get()
        self.assertEqual(log.status, AutomationLog.STATUS_SUCCESS)
        self.assertNotIn("tg-secret", str(log.request) + str(log.context))
        self.assertIn("bot••••", log.request["url"])

    def test_subchannel_override_replaces_channel_body(self):
        self.group.body = {"chat_id": "{{target}}", "text": "OVERRIDE {{device}}", "parse_mode": "HTML"}
        self.group.save()
        self.soap_rule()
        with posts(http()) as post:
            self.reading()
        self.assertEqual(post.call_args.kwargs["json"], {"chat_id": "-100123", "text": "OVERRIDE Sabun Wastafel", "parse_mode": "HTML"})

    def test_unknown_placeholder_is_left_and_listed(self):
        self.assertEqual(channels.fill({"a": "{{nope}} {{device}}"}, {"device": "X"}), {"a": "{{nope}} X"})
        self.assertEqual(channels.unknown_placeholders({"a": "{{nope}} {{env:X}} {{device}}"}), ["nope"])
        self.assertEqual(channels.fill("{{env:EVOLUTION_API_KEY}}", {}, secrets=False), "••••")

    def test_whatsapp_sends_each_target_through_evolution(self):
        sub = Subchannel.objects.create(channel=self.whatsapp, name="Grup WA", target="62811, 1203@g.us")
        rule = self.soap_rule()
        rule.subchannels.set([sub])
        with posts(http(), http()) as post:
            self.reading()
        self.assertEqual([c.kwargs["json"]["number"] for c in post.call_args_list], ["62811", "1203@g.us"])
        self.assertEqual(post.call_args.args[0], "http://evolution:8080/message/sendText/washroom")
        self.assertEqual(post.call_args.kwargs["headers"], {"apikey": "evo-secret"})

    # ---------- occurrence / cooldown ----------
    def test_status_rule_sends_once_until_back_to_normal(self):
        self.soap_rule()
        with posts(http(), http()) as post:
            self.reading()
            self.reading()  # still empty: no second message
            self.reading(severity="normal", condition="Terisi", level=80)
            self.reading()  # empty again: a new occurrence
        self.assertEqual(post.call_count, 2)

    def test_cooldown_resends_while_still_active(self):
        rule = self.soap_rule(cooldown_minutes=30)
        with posts(http(), http()) as post:
            self.reading()
            RuleState.objects.filter(rule=rule).update(last_sent_at=timezone.now() - timedelta(minutes=31))
            self.reading()
        self.assertEqual(post.call_count, 2)

    def test_matching_by_device_scope_area_and_type(self):
        trash = DeviceList.objects.create(device_id="TRASH-1", type="trash", scope=self.other_scope)
        rule = Rule.objects.create(name="r", condition=Rule.SENSOR_STATUS, device_types=["soap"])
        self.assertEqual(list(rule.matching_devices()), [self.soap])
        rule.device_types = []
        rule.save()
        rule.scopes.add(self.other_scope)
        self.assertEqual(list(rule.matching_devices()), [trash])
        rule.scopes.clear()
        rule.areas.add(self.area)
        self.assertEqual(set(rule.matching_devices()), {self.soap, trash})
        rule.areas.clear()
        rule.devices.add(self.soap)
        self.assertEqual(list(rule.matching_devices()), [self.soap])

    # ---------- staff on duty (cleaners + supervisors together) ----------
    def staff(self):
        budi = Recipient.objects.create(name="Budi", whatsapp_number="62811", email="budi@x.id")
        sari = Recipient.objects.create(name="Sari", email="sari@x.id")  # no Telegram / WhatsApp
        boss = Recipient.objects.create(name="Spv", telegram_chat_id="777")
        budi.scopes.add(self.scope)
        sari.scopes.add(self.scope)
        boss.areas.add(self.area)
        return budi, sari, boss

    def staff_rule(self, **kwargs):
        rule = Rule.objects.create(name="Sabun habis", condition=Rule.SENSOR_STATUS, severities=["critical"],
                                   device_types=["soap"], notify_on_duty=True, **kwargs)
        rule.person_channels.set([self.telegram, self.whatsapp, self.email])
        return rule

    def test_cleaners_and_supervisor_get_it_at_the_same_time(self):
        self.staff()
        self.staff_rule()
        with posts(http(), http()) as post:
            self.reading()
        # Budi via WhatsApp (his first contact), Sari via email (her only contact), the supervisor via Telegram.
        self.assertEqual(post.call_count, 2)
        self.assertEqual([m.to for m in mail.outbox], [["sari@x.id"]])
        sent = {(log.recipient.name, log.tier, log.channel_type) for log in AutomationLog.objects.all()}
        self.assertEqual(sent, {("Budi", "cleaner", "whatsapp"), ("Sari", "cleaner", "email"),
                                ("Spv", "supervisor", "telegram")})
        texts = {c.kwargs["json"].get("text") for c in post.call_args_list}
        self.assertEqual(len(texts), 1)  # same message for cleaner and supervisor

    def test_scope_without_cleaner_still_reaches_supervisor(self):
        boss = Recipient.objects.create(name="Spv", telegram_chat_id="777")
        boss.areas.add(self.area)
        self.staff_rule()
        with posts(http()) as post:
            self.reading()
        self.assertEqual(post.call_args.kwargs["json"]["chat_id"], "777")
        self.assertEqual(AutomationLog.objects.get().tier, "supervisor")

    def test_nobody_on_duty_is_only_logged(self):
        self.staff_rule()
        with posts() as post:
            self.reading()
        post.assert_not_called()
        log = AutomationLog.objects.get()
        self.assertEqual((log.status, log.note), (AutomationLog.STATUS_SKIPPED, "Tidak ada cleaner maupun supervisor di lokasi ini"))

    def test_priority_falls_back_to_next_contact_and_all_mode(self):
        budi = Recipient.objects.create(name="Budi", whatsapp_number="62811", telegram_chat_id="555")
        budi.scopes.add(self.scope)
        self.staff_rule()
        with posts(http(500), http()) as post:
            self.reading()
        self.assertEqual(post.call_count, 2)  # WhatsApp failed (no retry scheduled) -> Telegram
        self.assertEqual(list(AutomationLog.objects.order_by("pk").values_list("channel_type", "status")),
                         [("whatsapp", "failed"), ("telegram", "success")])

    def test_people_threshold_sends_work_order_and_tells_cleaner_and_supervisor(self):
        _, _, boss = self.staff()
        people = DeviceList.objects.get(device_id=DEVICE_ID)
        people.scope = self.scope
        people.save()
        rule = Rule.objects.get(name="Pengunjung mencapai batas")
        rule.person_channels.set([self.telegram, self.whatsapp, self.email])
        with posts(http(json_data=[{"error": 0, "results": [{"WO_NO": "000001"}]}]), http(), http()):
            engine.people_threshold(people, 20, 20)
        self.assertEqual(NotificationLog.objects.get().wo_number, "000001")
        self.assertFalse(RuleState.objects.exists())  # one-shot: no state
        self.assertTrue(AutomationLog.objects.filter(recipient=boss, tier="supervisor").exists())

    # ---------- retry ----------
    def test_failed_send_is_retried_until_the_limit(self):
        self.soap_rule()
        with posts(requests.ConnectionError("down")):
            self.reading()
        log = AutomationLog.objects.get()
        self.assertEqual((log.status, log.attempt), (AutomationLog.STATUS_RETRY, 1))
        later = timezone.now() + timedelta(minutes=6)
        with posts(http(500)):
            channels.retry_due(now=later)
        with posts(http(500)):
            channels.retry_due(now=later + timedelta(minutes=6))
        statuses = list(AutomationLog.objects.order_by("attempt").values_list("attempt", "status"))
        self.assertEqual(statuses, [(1, "failed"), (2, "failed"), (3, "failed")])  # retry_count=2

    def test_algospection_is_never_retried(self):
        channel = Channel.objects.get(name="Algospection")
        channel.retry_count = 3
        channel.save()
        self.assertEqual(Channel.objects.get(name="Algospection").retry_count, 0)

    # ---------- other conditions ----------
    def test_low_battery_bad_rating_and_offline(self):
        battery = Rule.objects.create(name="Baterai", condition=Rule.LOW_BATTERY, battery_below=20)
        battery.subchannels.add(self.group)
        feedback = DeviceList.objects.create(device_id="FB-1", type="satisfaction", scope=self.scope)
        rating = Rule.objects.create(name="Rating", condition=Rule.BAD_RATING)
        rating.subchannels.add(self.group)
        offline = Rule.objects.create(name="Offline", condition=Rule.OFFLINE, offline_minutes=30, device_types=["soap"])
        offline.subchannels.add(self.group)
        with posts(http(), http(), http()) as post:
            self.reading(severity="normal", condition="Terisi", battery=12)
            engine.rating_received(CustomerResponse.objects.create(device=feedback, time=timezone.now(), rating=1))
            engine.rating_received(CustomerResponse.objects.create(device=feedback, time=timezone.now(), rating=5))
            SensorReading.objects.filter(device=self.soap).update(time=timezone.now() - timedelta(hours=2))
            engine.check_offline()
            engine.check_offline()  # still offline: no repeat
        self.assertEqual(post.call_count, 3)
        self.assertEqual(sorted(AutomationLog.objects.values_list("rule__name", flat=True)), ["Baterai", "Offline", "Rating"])

    def test_people_offline_from_sensor_log(self):
        people = DeviceList.objects.get(device_id=DEVICE_ID)
        rule = Rule.objects.create(name="Offline", condition=Rule.OFFLINE, device_types=["people"])
        rule.subchannels.add(self.group)
        SensorLog.objects.create(device=people, status=SensorLog.STATUS_OFFLINE, endpoint_url="zk", response={})
        with posts(http()) as post:
            engine.check_offline()
        self.assertEqual(post.call_count, 1)

    def test_engine_errors_never_break_the_caller(self):
        self.soap_rule()
        with mock.patch.object(engine, "process", side_effect=RuntimeError("boom")):
            self.reading()  # no exception


class AutomationAdminTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_superuser("admin", "", "pw"))

    def test_admin_pages_and_test_send(self):
        channel = Channel.objects.create(name="Webhook", type=Channel.TYPE_WEBHOOK)
        sub = Subchannel.objects.create(channel=channel, name="Hook", target="http://example.test/hook")
        for url in ("/admin/automation/channel/", f"/admin/automation/channel/{channel.pk}/change/",
                    f"/admin/automation/subchannel/{sub.pk}/change/", "/admin/automation/rule/",
                    "/admin/automation/recipient/", "/admin/automation/automationlog/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        with posts(http()):
            self.client.post("/admin/automation/subchannel/", {"action": "send_test", "_selected_action": [sub.pk]})
        self.assertEqual(AutomationLog.objects.get().tier, "test")

    def test_invalid_json_body_is_rejected(self):
        channel = Channel.objects.create(name="Webhook", type=Channel.TYPE_WEBHOOK)
        response = self.client.post(f"/admin/automation/channel/{channel.pk}/change/", {
            "name": "Webhook", "type": "webhook", "is_active": "on", "config": "{}", "method": "POST",
            "head": "{}", "body": "[1, 2]", "email_subject": "", "email_body": "",
            "retry_count": 0, "retry_delay_minutes": 5,
            "subchannels-TOTAL_FORMS": 0, "subchannels-INITIAL_FORMS": 0,
        })
        self.assertContains(response, "Must be a JSON object")
