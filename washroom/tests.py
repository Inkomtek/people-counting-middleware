from datetime import datetime, timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from core.models import DeviceList, EventLog, NotificationLog

from .models import ApiClient, CustomerResponse, SensorDevice, SensorReading, Washroom
from .services import evaluate_condition

READINGS_URL = "/api/v1/readings/"
RESPONSES_URL = "/api/v1/customer-responses/"
DASHBOARD_URL = "/api/v1/dashboard/"


class ApiTestCase(TestCase):
    def setUp(self):
        self.api_client = ApiClient(name="partner")
        self.key = self.api_client.set_new_key()
        self.api_client.save()

    def post(self, url, data, key=None):
        return self.client.post(url, data, content_type="application/json", HTTP_X_API_KEY=key or self.key)

    def get(self, url, params=None, key=None):
        return self.client.get(url, params or {}, HTTP_X_API_KEY=key or self.key)


class AuthTests(ApiTestCase):
    def test_missing_key_is_401(self):
        response = self.client.get(DASHBOARD_URL)
        self.assertEqual(response.status_code, 401)

    def test_wrong_key_is_401(self):
        self.assertEqual(self.get(DASHBOARD_URL, key="wrong").status_code, 401)

    def test_inactive_client_is_401(self):
        self.api_client.is_active = False
        self.api_client.save()
        self.assertEqual(self.get(DASHBOARD_URL).status_code, 401)

    def test_raw_key_is_not_stored(self):
        self.api_client.refresh_from_db()
        self.assertNotEqual(self.api_client.key_hash, self.key)
        self.assertEqual(self.get(DASHBOARD_URL).status_code, 200)
        self.api_client.refresh_from_db()
        self.assertIsNotNone(self.api_client.last_used_at)

    def test_admin_shows_key_once_on_create(self):
        self.client.force_login(User.objects.create_superuser("admin", "", "pw"))
        response = self.client.post("/admin/washroom/apiclient/add/", {"name": "dash", "is_active": "on"}, follow=True)
        message = str(list(response.context["messages"])[0])
        raw_key = message.split("API key for dash: ")[1].split(" ")[0]
        self.assertEqual(self.get(DASHBOARD_URL, key=raw_key).status_code, 200)


class ConditionTests(TestCase):
    def test_default_rules(self):
        cases = [
            ("tissue", 28, "Hampir Habis"), ("tissue", 0, "Habis"), ("soap", 100, "Terisi"),
            ("toilet_paper", 0.5, "Habis"), ("trash", 100, "Penuh"), ("trash", 50, "Normal"),
            ("amonia", 3.2, "Normal"), ("amonia", 30, "Bahaya"),
        ]
        for sensor_type, level, condition in cases:
            self.assertEqual(evaluate_condition(sensor_type, level)[0], condition, (sensor_type, level))

    def test_no_level_has_no_condition(self):
        self.assertEqual(evaluate_condition("soap", None), ("", ""))


class ReadingTests(ApiTestCase):
    def test_single_reading_registers_device_and_computes_condition(self):
        response = self.post(READINGS_URL, {
            "device_id": "D2494A695C05", "type": "tissue", "battery": 55, "level": 28,
            "time": "2026-10-02T13:45:00+07:00", "location": "Lt 1 Pria",
        })
        self.assertEqual(response.status_code, 201, response.json())
        body = response.json()
        self.assertEqual(body["status"], "success")
        self.assertEqual((body["data"]["condition"], body["data"]["severity"]), ("Hampir Habis", "warning"))
        device = SensorDevice.objects.get(id="D2494A695C05")
        self.assertEqual((device.type, device.last_battery, device.location), ("tissue", 55, "Lt 1 Pria"))
        self.assertEqual(SensorReading.objects.get().payload["level"], 28)
        self.assertEqual(SensorReading.objects.get().client, self.api_client)

    def test_batch_reading(self):
        response = self.post(READINGS_URL, [
            {"device_id": "A", "type": "soap", "level": 100},
            {"device_id": "B", "type": "trash", "level": 95},
        ])
        self.assertEqual(response.status_code, 201)
        self.assertEqual([r["condition"] for r in response.json()["data"]], ["Terisi", "Penuh"])

    def test_invalid_item_rejects_whole_batch(self):
        response = self.post(READINGS_URL, [
            {"device_id": "A", "type": "soap", "level": 50},
            {"device_id": "B", "type": "kopi", "level": 150},
        ])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["errors"][0]["index"], 1)
        self.assertFalse(SensorReading.objects.exists())

    def test_percent_level_over_100_rejected(self):
        response = self.post(READINGS_URL, {"device_id": "A", "type": "soap", "level": 120})
        self.assertEqual(response.status_code, 400)

    def test_type_mismatch_with_registered_device_rejected(self):
        SensorDevice.objects.create(id="A", type="soap")
        response = self.post(READINGS_URL, {"device_id": "A", "type": "trash", "level": 10})
        self.assertEqual(response.status_code, 400)

    def test_older_reading_does_not_override_live_state(self):
        self.post(READINGS_URL, {"device_id": "A", "type": "soap", "level": 80, "time": "2026-10-02T13:00:00+07:00"})
        self.post(READINGS_URL, {"device_id": "A", "type": "soap", "level": 0, "time": "2026-10-02T12:00:00+07:00"})
        device = SensorDevice.objects.get(id="A")
        self.assertEqual((device.last_level, device.last_condition), (80, "Terisi"))
        self.assertEqual(SensorReading.objects.count(), 2)

    def test_list_readings_filters_by_device(self):
        self.post(READINGS_URL, [{"device_id": "A", "type": "soap", "level": 1}, {"device_id": "B", "type": "soap"}])
        response = self.get(READINGS_URL, {"device_id": "A"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([r["device_id"] for r in response.json()["results"]], ["A"])

    def test_non_object_body_rejected(self):
        self.assertEqual(self.post(READINGS_URL, [1, 2]).status_code, 400)
        self.assertEqual(self.post(READINGS_URL, []).status_code, 400)


class CustomerResponseTests(ApiTestCase):
    def test_post_rating(self):
        response = self.post(RESPONSES_URL, {"device_id": "FB01", "rating": 5, "location": "Lt 1"})
        self.assertEqual(response.status_code, 201, response.json())
        self.assertEqual(CustomerResponse.objects.get().rating, 5)

    def test_rating_out_of_range_rejected(self):
        self.assertEqual(self.post(RESPONSES_URL, {"device_id": "FB01", "rating": 6}).status_code, 400)


class DashboardTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        # Seeded by washroom/0004: Graha ISS Bintaro, Lantai 2, Pria, with the ZK counter.
        self.washroom = Washroom.objects.get(floor="Lantai 2", gender="pria")
        self.counter = DeviceList.objects.get(id="2069691213314072577")

    def dashboard(self, **params):
        response = self.get(DASHBOARD_URL, {"washroom": self.washroom.id, **params})
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_unassigned_types_are_coming_soon(self):
        body = self.dashboard()
        self.assertEqual(body["washroom"]["building"], "GRAHA ISS BINTARO")
        self.assertTrue(body["people_counting"]["available"])
        self.assertFalse(body["customer_satisfaction"]["available"])
        self.assertEqual([s["type"] for s in body["sensors"]], ["soap", "toilet_paper", "tissue", "trash", "amonia"])
        self.assertFalse(any(s["available"] for s in body["sensors"]))

    def test_people_counting_today(self):
        now = timezone.now()
        self.counter.current_count, self.counter.maximum_trigger = 3, 20
        self.counter.save()
        for i, event_type in enumerate(["in", "in", "out"]):
            EventLog.objects.create(id=f"e{i}", time=now, device=self.counter, event_type=event_type,
                                    recognition_target="Cross Line")
        EventLog.objects.create(id="old", time=now - timedelta(days=1), device=self.counter, event_type="in",
                                recognition_target="Cross Line")
        NotificationLog.objects.create(device=self.counter, endpoint_url="x", response_status="200 OK")
        people = self.dashboard()["people_counting"]
        self.assertEqual((people["people_in"], people["current_count"], people["maximum_trigger"]), (2, 3, 20))
        self.assertEqual(people["work_orders"], 1)

    def test_past_date_has_no_running_count_or_online(self):
        yesterday = timezone.localdate() - timedelta(days=1)
        day_start = timezone.make_aware(datetime.combine(yesterday, datetime.min.time()))
        EventLog.objects.create(id="y", time=day_start + timedelta(hours=9), device=self.counter, event_type="in",
                                recognition_target="Cross Line")
        SensorDevice.objects.create(id="SOAP1", type="soap", washroom=self.washroom)
        self.post(READINGS_URL, [
            {"device_id": "SOAP1", "type": "soap", "level": 20, "time": (day_start + timedelta(hours=8)).isoformat()},
            {"device_id": "SOAP1", "type": "soap", "level": 90, "time": timezone.now().isoformat()},
        ])
        body = self.dashboard(date=yesterday.isoformat())
        self.assertFalse(body["is_today"])
        self.assertEqual(body["people_counting"]["people_in"], 1)
        self.assertIsNone(body["people_counting"]["current_count"])
        soap = body["sensors"][0]
        self.assertEqual((soap["level"], soap["condition"], soap["online"]), (20, "Hampir Habis", None))
        self.assertEqual(self.dashboard()["sensors"][0]["level"], 90)

    def test_sensor_card_shows_worst_device(self):
        for device_id in ("TR1", "TR2"):
            SensorDevice.objects.create(id=device_id, type="trash", washroom=self.washroom)
        SensorDevice.objects.create(id="OTHER", type="trash")  # not assigned: ignored
        self.post(READINGS_URL, [
            {"device_id": "TR1", "type": "trash", "level": 20, "battery": 80},
            {"device_id": "TR2", "type": "trash", "level": 95, "battery": 40,
             "time": (timezone.now() - timedelta(hours=2)).isoformat()},
            {"device_id": "OTHER", "type": "trash", "level": 100},
        ])
        trash = {s["type"]: s for s in self.dashboard()["sensors"]}["trash"]
        self.assertEqual((trash["device_id"], trash["condition"], trash["severity"]), ("TR2", "Penuh", "critical"))
        self.assertEqual((trash["device_count"], trash["offline_count"], trash["online"]), (2, 1, False))

    def test_customer_satisfaction_uses_assigned_feedback_devices(self):
        self.post(RESPONSES_URL, [{"device_id": "FB", "rating": 5}, {"device_id": "FB", "rating": 3}])
        self.post(RESPONSES_URL, {"device_id": "ELSEWHERE", "rating": 1})
        SensorDevice.objects.filter(id="FB").update(washroom=self.washroom)
        cs = self.dashboard()["customer_satisfaction"]
        self.assertEqual((cs["available"], cs["total"], cs["average"]), (True, 2, 4.0))
        self.assertEqual(cs["by_rating"]["5"], 1)

    def test_future_or_bad_date_rejected(self):
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        self.assertEqual(self.get(DASHBOARD_URL, {"date": tomorrow}).status_code, 400)
        self.assertEqual(self.get(DASHBOARD_URL, {"date": "02-10-2026"}).status_code, 400)

    def test_unknown_washroom_is_404(self):
        self.assertEqual(self.get(DASHBOARD_URL, {"washroom": 999}).status_code, 404)

    def test_washroom_list(self):
        Washroom.objects.create(building="GRAHA ISS BINTARO", floor="Lantai 2", gender="wanita")
        response = self.get("/api/v1/washrooms/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([w["gender_label"] for w in response.json()], ["Pria", "Wanita"])


class FeedbackDeviceTests(ApiTestCase):
    def test_response_registers_feedback_device(self):
        self.post(RESPONSES_URL, {"device_id": "FB9", "rating": 4})
        self.assertEqual(SensorDevice.objects.get(id="FB9").type, "feedback")

    def test_feedback_type_not_accepted_as_reading(self):
        self.assertEqual(self.post(READINGS_URL, {"device_id": "X", "type": "feedback", "level": 1}).status_code, 400)

    def test_sensor_device_cannot_send_ratings(self):
        SensorDevice.objects.create(id="SOAP1", type="soap")
        self.assertEqual(self.post(RESPONSES_URL, {"device_id": "SOAP1", "rating": 4}).status_code, 400)


class DocsTests(TestCase):
    def test_schema_and_docs_are_public(self):
        self.assertEqual(self.client.get("/api/schema/").status_code, 200)
        self.assertEqual(self.client.get("/api/docs/").status_code, 200)


class ErrorFormatTests(TestCase):
    def test_auth_error_uses_status_message_shape(self):
        response = self.client.get(DASHBOARD_URL)
        self.assertEqual(response.json()["status"], "error")
        self.assertIn("message", response.json())


class CreateApiClientCommandTests(TestCase):
    def run_command(self, *args):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        call_command("create_api_client", *args, stdout=out)
        return out.getvalue().strip().splitlines()[-1]

    def test_creates_client_with_working_key(self):
        key = self.run_command("gateway")
        self.assertEqual(self.client.get(DASHBOARD_URL, HTTP_X_API_KEY=key).status_code, 200)

    def test_existing_name_needs_regenerate(self):
        from django.core.management.base import CommandError

        old_key = self.run_command("gateway")
        with self.assertRaises(CommandError):
            self.run_command("gateway")
        new_key = self.run_command("gateway", "--regenerate")
        self.assertEqual(self.client.get(DASHBOARD_URL, HTTP_X_API_KEY=old_key).status_code, 401)
        self.assertEqual(self.client.get(DASHBOARD_URL, HTTP_X_API_KEY=new_key).status_code, 200)


class DashboardTimezoneTests(ApiTestCase):
    def test_times_are_wib(self):
        self.assertTrue(self.get(DASHBOARD_URL).json()["generated_at"].endswith("+07:00"))


class DashboardPageTests(ApiTestCase):
    def test_page_requires_admin_login(self):
        response = self.client.get("/dashboard/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_staff_session_sees_page_and_api(self):
        self.client.force_login(User.objects.create_user("staff", password="pw", is_staff=True))
        self.assertContains(self.client.get("/dashboard/"), "Washroom Dashboard")
        body = self.client.get(DASHBOARD_URL).json()
        self.assertIn("status_rules", body)
        self.assertEqual(body["offline_after_minutes"], 30)

    def test_non_staff_session_cannot_read_api(self):
        self.client.force_login(User.objects.create_user("user", password="pw"))
        self.assertEqual(self.client.get(DASHBOARD_URL).status_code, 403)

    def test_session_cannot_post_readings(self):
        self.client.force_login(User.objects.create_user("staff", password="pw", is_staff=True))
        response = self.client.post(READINGS_URL, {"device_id": "A", "type": "soap"}, content_type="application/json")
        self.assertEqual(response.status_code, 401)


class SendDummyDataCommandTests(TestCase):
    def run_command(self, *args):
        from io import StringIO
        from unittest import mock

        from django.core.management import call_command

        def post_through_test_client(url, json, headers, timeout):
            # Route the command's HTTP calls to the Django test client.
            path = url.split("127.0.0.1:8000", 1)[1]
            response = self.client.post(path, json, content_type="application/json",
                                        HTTP_X_API_KEY=headers["X-API-Key"])
            fake = mock.Mock(status_code=response.status_code, text=response.content.decode())
            fake.json.return_value = response.json()
            return fake

        out = StringIO()
        with mock.patch("washroom.management.commands.send_dummy_data.requests.post", post_through_test_client):
            call_command("send_dummy_data", *args, stdout=out)
        return out.getvalue()

    def test_sends_every_sensor_and_shows_on_dashboard(self):
        output = self.run_command("--scenario", "critical", "--ratings", "3")
        self.assertEqual(output.count("HTTP 201"), 2)
        washroom = Washroom.objects.first()
        self.client.force_login(User.objects.create_user("staff", password="pw", is_staff=True))
        body = self.client.get(DASHBOARD_URL, {"washroom": washroom.id}).json()
        self.assertTrue(all(s["available"] and s["severity"] == "critical" for s in body["sensors"]))
        self.assertEqual(body["customer_satisfaction"]["total"], 3)

    def test_cleanup_removes_dummy_data_only(self):
        SensorDevice.objects.create(id="REAL-1", type="soap")
        self.run_command()
        self.run_command("--cleanup")
        self.assertEqual(list(SensorDevice.objects.values_list("id", flat=True)), ["REAL-1"])
        self.assertFalse(CustomerResponse.objects.exists())
        self.assertFalse(ApiClient.objects.filter(name="dummy-tester").exists())
