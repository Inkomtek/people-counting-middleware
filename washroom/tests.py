from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from core.models import DeviceList

from .models import ApiClient, CustomerResponse, SensorReading
from .services import evaluate_condition

READINGS_URL = "/api/v1/readings/"
RESPONSES_URL = "/api/v1/customer-responses/"
# The seeded ZK people counter's toilet (core/0007).
TOILET = {"building": "GRAHA ISS BINTARO", "floor": "2", "gender": "male"}


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
    def test_missing_key_is_401_with_error_shape(self):
        response = self.client.get(READINGS_URL)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["status"], "error")
        self.assertIn("message", response.json())

    def test_wrong_key_is_401(self):
        self.assertEqual(self.get(READINGS_URL, key="wrong").status_code, 401)

    def test_inactive_client_is_401(self):
        self.api_client.is_active = False
        self.api_client.save()
        self.assertEqual(self.get(READINGS_URL).status_code, 401)

    def test_raw_key_is_not_stored(self):
        self.api_client.refresh_from_db()
        self.assertNotEqual(self.api_client.key_hash, self.key)
        self.assertEqual(self.get(READINGS_URL).status_code, 200)
        self.api_client.refresh_from_db()
        self.assertIsNotNone(self.api_client.last_used_at)

    def test_admin_shows_key_once_on_create(self):
        self.client.force_login(User.objects.create_superuser("admin", "", "pw"))
        response = self.client.post("/admin/washroom/apiclient/add/", {"name": "dash", "is_active": "on"}, follow=True)
        message = str(list(response.context["messages"])[0])
        raw_key = message.split("API key for dash: ")[1].split(" ")[0]
        self.client.logout()
        self.assertEqual(self.get(READINGS_URL, key=raw_key).status_code, 200)

    def test_admin_session_cannot_post(self):
        self.client.force_login(User.objects.create_superuser("admin", "", "pw"))
        response = self.client.post(READINGS_URL, {"device_id": "A", "type": "soap"}, content_type="application/json")
        self.assertEqual(response.status_code, 401)


class ConditionTests(TestCase):
    def test_default_rules(self):
        cases = [
            ("tissue", 28, "Hampir Habis"), ("tissue", 0, "Habis"), ("soap", 100, "Terisi"),
            ("toilet-paper", 0.5, "Habis"), ("trash", 100, "Penuh"), ("trash", 50, "Normal"),
            ("ammonia", 3.2, "Normal"), ("ammonia", 30, "Bahaya"),
        ]
        for sensor_type, level, condition in cases:
            self.assertEqual(evaluate_condition(sensor_type, level)[0], condition, (sensor_type, level))

    def test_no_level_has_no_condition(self):
        self.assertEqual(evaluate_condition("soap", None), ("", ""))


class ReadingTests(ApiTestCase):
    def test_reading_on_located_device(self):
        DeviceList.objects.create(id="TIS1", type="tissue", **TOILET)
        response = self.post(READINGS_URL, {
            "device_id": "TIS1", "type": "tissue", "battery": 55, "level": 28, "time": "2026-10-02T13:45:00+07:00",
        })
        self.assertEqual(response.status_code, 201, response.json())
        data = response.json()["data"]
        self.assertEqual((data["condition"], data["severity"]), ("Hampir Habis", "warning"))
        self.assertEqual((data["building"], data["floor"], data["gender"]), tuple(TOILET.values()))
        self.assertEqual(data["time"], "2026-10-02T13:45:00+07:00")
        reading = SensorReading.objects.get()
        self.assertEqual((reading.payload["level"], reading.client), (28, self.api_client))

    def test_unknown_device_is_registered_with_type_and_no_location(self):
        self.assertEqual(self.post(READINGS_URL, {"device_id": "NEW", "type": "toilet-paper", "level": 0}).status_code, 201)
        device = DeviceList.objects.get(id="NEW")
        self.assertEqual((device.type, device.building), ("toilet-paper", ""))

    def test_registered_devices_are_never_synced_from_zk(self):
        from core import services

        self.post(READINGS_URL, {"device_id": "NEW", "type": "soap", "level": 50})
        with mock.patch.object(services, "sync_device") as sync_device, \
                mock.patch.object(services.ZKClient, "__init__", return_value=None):
            services.sync_all()
        self.assertNotIn("NEW", [call.args[0].id for call in sync_device.call_args_list])

    def test_batch_reading(self):
        response = self.post(READINGS_URL, [
            {"device_id": "A", "type": "soap", "level": 100},
            {"device_id": "B", "type": "trash", "level": 95},
            {"device_id": "C", "type": "ammonia", "level": 30},
        ])
        self.assertEqual(response.status_code, 201)
        self.assertEqual([r["condition"] for r in response.json()["data"]], ["Terisi", "Penuh", "Bahaya"])

    def test_invalid_item_rejects_whole_batch(self):
        response = self.post(READINGS_URL, [
            {"device_id": "A", "type": "soap", "level": 50},
            {"device_id": "B", "type": "amonia", "level": 5},
        ])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["errors"][0]["index"], 1)
        self.assertFalse(SensorReading.objects.exists())
        self.assertFalse(DeviceList.objects.filter(id="A").exists())

    def test_percent_level_over_100_rejected(self):
        self.assertEqual(self.post(READINGS_URL, {"device_id": "A", "type": "soap", "level": 120}).status_code, 400)

    def test_type_mismatch_with_registered_device_rejected(self):
        DeviceList.objects.create(id="A", type="soap")
        self.assertEqual(self.post(READINGS_URL, {"device_id": "A", "type": "trash", "level": 10}).status_code, 400)

    def test_people_counter_cannot_send_readings(self):
        response = self.post(READINGS_URL, {"device_id": "2069691213314072577", "type": "soap", "level": 10})
        self.assertEqual(response.status_code, 400)

    def test_list_filters_by_toilet_and_type(self):
        DeviceList.objects.create(id="A", type="soap", **TOILET)
        DeviceList.objects.create(id="B", type="soap", **{**TOILET, "gender": "female"})
        DeviceList.objects.create(id="C", type="trash", **TOILET)
        self.post(READINGS_URL, [
            {"device_id": "A", "type": "soap", "level": 1},
            {"device_id": "B", "type": "soap", "level": 1},
            {"device_id": "C", "type": "trash", "level": 1},
        ])
        response = self.get(READINGS_URL, {**TOILET, "type": "soap"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([r["device_id"] for r in response.json()["results"]], ["A"])

    def test_non_object_body_rejected(self):
        self.assertEqual(self.post(READINGS_URL, [1, 2]).status_code, 400)
        self.assertEqual(self.post(READINGS_URL, []).status_code, 400)


class CustomerResponseTests(ApiTestCase):
    def test_post_rating_registers_satisfaction_device(self):
        response = self.post(RESPONSES_URL, {"device_id": "FB01", "rating": 5, "comment": "bersih"})
        self.assertEqual(response.status_code, 201, response.json())
        self.assertEqual(CustomerResponse.objects.get().rating, 5)
        self.assertEqual(DeviceList.objects.get(id="FB01").type, "satisfaction")

    def test_rating_out_of_range_rejected(self):
        self.assertEqual(self.post(RESPONSES_URL, {"device_id": "FB01", "rating": 6}).status_code, 400)

    def test_sensor_device_cannot_send_ratings(self):
        DeviceList.objects.create(id="SOAP1", type="soap")
        self.assertEqual(self.post(RESPONSES_URL, {"device_id": "SOAP1", "rating": 4}).status_code, 400)

    def test_list_filters_by_toilet(self):
        DeviceList.objects.create(id="FB1", type="satisfaction", **TOILET)
        DeviceList.objects.create(id="FB2", type="satisfaction", **{**TOILET, "floor": "3"})
        self.post(RESPONSES_URL, [{"device_id": "FB1", "rating": 5}, {"device_id": "FB2", "rating": 1}])
        results = self.get(RESPONSES_URL, {"floor": "2"}).json()["results"]
        self.assertEqual([(r["device_id"], r["floor"]) for r in results], [("FB1", "2")])


class DocsTests(TestCase):
    def test_schema_and_docs_are_public(self):
        self.assertEqual(self.client.get("/api/schema/").status_code, 200)
        self.assertEqual(self.client.get("/api/docs/").status_code, 200)


class CreateApiClientCommandTests(TestCase):
    def run_command(self, *args):
        out = StringIO()
        call_command("create_api_client", *args, stdout=out)
        return out.getvalue().strip().splitlines()[-1]

    def test_creates_client_with_working_key(self):
        key = self.run_command("gateway")
        self.assertEqual(self.client.get(READINGS_URL, HTTP_X_API_KEY=key).status_code, 200)

    def test_existing_name_needs_regenerate(self):
        old_key = self.run_command("gateway")
        with self.assertRaises(CommandError):
            self.run_command("gateway")
        new_key = self.run_command("gateway", "--regenerate")
        self.assertEqual(self.client.get(READINGS_URL, HTTP_X_API_KEY=old_key).status_code, 401)
        self.assertEqual(self.client.get(READINGS_URL, HTTP_X_API_KEY=new_key).status_code, 200)


class SendDummyDataCommandTests(TestCase):
    def run_command(self, *args):
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

    def test_sends_every_type_to_the_people_counter_toilet(self):
        output = self.run_command("--scenario", "critical", "--ratings", "3")
        self.assertEqual(output.count("HTTP 201"), 2)
        devices = DeviceList.objects.filter(id__startswith="DUMMY-")
        self.assertEqual(
            sorted(devices.values_list("type", flat=True)),
            sorted(["soap", "toilet-paper", "tissue", "trash", "ammonia", "satisfaction"]),
        )
        self.assertFalse(devices.exclude(**TOILET).exists())
        self.assertEqual(set(SensorReading.objects.values_list("severity", flat=True)), {"critical"})
        self.assertEqual(CustomerResponse.objects.count(), 3)

    def test_cleanup_removes_dummy_data_only(self):
        DeviceList.objects.create(id="REAL-1", type="soap")
        self.run_command()
        self.run_command("--cleanup")
        self.assertFalse(DeviceList.objects.filter(id__startswith="DUMMY-").exists())
        self.assertEqual(DeviceList.objects.filter(id__in=["REAL-1", "2069691213314072577"]).count(), 2)
        self.assertFalse(SensorReading.objects.exists() or CustomerResponse.objects.exists())
        self.assertFalse(ApiClient.objects.filter(name="dummy-tester").exists())
