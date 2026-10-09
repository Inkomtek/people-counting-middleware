from datetime import timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from core.models import Area, Client, DeviceList, Region, Scope, Site

from .models import ApiClient, CustomerResponse, SensorReading
from .services import evaluate_condition

READINGS_URL = "/api/v1/readings/"
RESPONSES_URL = "/api/v1/customer-responses/"
# The seeded ZK people counter's toilet (core/0007).
TOILET = {"building": "GRAHA ISS BINTARO", "floor": "2", "gender": "male"}


def make_scope(name="Floor 2 - Toilet Pria"):
    site = Site.objects.create(client=Client.objects.get_or_create(name="ISS")[0],
                               region=Region.objects.get_or_create(name="Banten")[0], name="Bintaro")
    return Scope.objects.create(area=Area.objects.create(site=site, name="Graha ISS"), name=name)


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


def raw(device_id, data_id="1", **fields):
    """A reading in the sensor team's raw data format."""
    return {"id": data_id, "inputDate": "2026-10-02T13:45:00+07:00", "deviceId": device_id, **fields}


class ReadingTests(ApiTestCase):
    def test_reading_in_sensor_team_format(self):
        DeviceList.objects.create(device_id="TIS1", type="tissue", **TOILET)
        response = self.post(READINGS_URL, {
            "id": "1", "inputDate": "2026-08-19T10:15:30Z", "deviceId": "TIS1", "value": 60, "battery": 81,
            "lastOnline": "2026-08-19T10:15:30Z", "status": "Terisi",
        })
        self.assertEqual(response.status_code, 201, response.json())
        body = response.json()
        self.assertEqual((body["created"], body["duplicates"]), (1, 0))
        data = body["data"]
        self.assertEqual((data["id"], data["deviceId"], data["type"]), ("1", "TIS1", "tissue"))
        self.assertEqual((data["value"], data["battery"], data["status"]), (60, 81, "Terisi"))
        self.assertEqual(data["severity"], "normal")  # from the "Terisi" Status rule
        self.assertEqual((data["building"], data["floor"], data["gender"]), tuple(TOILET.values()))
        self.assertEqual(data["inputDate"], "2026-08-19T17:15:30+07:00")
        reading = SensorReading.objects.get()
        self.assertEqual((reading.external_id, reading.level, reading.condition), ("1", 60, "Terisi"))
        self.assertEqual(reading.last_online.isoformat(), "2026-08-19T10:15:30+00:00")
        self.assertEqual((reading.payload["value"], reading.client), (60, self.api_client))

    def test_status_and_value_are_stored_as_sent(self):
        DeviceList.objects.create(device_id="A", type="soap")
        DeviceList.objects.create(device_id="C", type="ammonia")
        response = self.post(READINGS_URL, [
            raw("A", value=150, status="Status Baru"),
            raw("C", value=None, status=None),
        ])
        self.assertEqual(response.status_code, 201, response.json())
        data = response.json()["data"]
        # value and status are kept as sent; an unknown status gets its colour from the value (150% soap ->
        # "Terisi" rule -> normal), and nothing at all leaves both empty.
        self.assertEqual([(r["value"], r["status"], r["severity"]) for r in data],
                         [(150, "Status Baru", "normal"), (None, "", "")])

    def test_severity_matches_status_case_insensitively(self):
        DeviceList.objects.create(device_id="B", type="trash")
        response = self.post(READINGS_URL, raw("B", value=95, status="penuh"))
        self.assertEqual(response.json()["data"]["severity"], "critical")

    def test_resent_id_is_skipped_per_device(self):
        DeviceList.objects.create(device_id="A", type="soap")
        DeviceList.objects.create(device_id="B", type="soap")
        self.assertEqual(self.post(READINGS_URL, raw("A", "7", value=10)).status_code, 201)
        response = self.post(READINGS_URL, [raw("A", "7", value=99), raw("B", "7", value=20), raw("B", "7")])
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual((body["created"], body["duplicates"]), (1, 2))
        self.assertEqual([r["value"] for r in body["data"]], [10, 20, 20])
        self.assertEqual(SensorReading.objects.count(), 2)
        again = self.post(READINGS_URL, raw("A", "7", value=10))
        self.assertEqual((again.status_code, again.json()["created"]), (200, 0))

    def test_required_fields(self):
        DeviceList.objects.create(device_id="A", type="soap")
        response = self.post(READINGS_URL, {"deviceId": "A", "value": 10})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.json()["errors"][0]["errors"]), {"id", "inputDate"})

    def test_unknown_device_is_rejected(self):
        response = self.post(READINGS_URL, raw("NEW", value=0))
        self.assertEqual(response.status_code, 400)
        self.assertIn("Device ID tidak terdaftar", response.json()["errors"][0]["errors"]["deviceId"][0])
        self.assertFalse(DeviceList.objects.filter(device_id="NEW").exists())

    def test_people_and_satisfaction_devices_cannot_send_readings(self):
        DeviceList.objects.create(device_id="FB1", type="satisfaction")
        for device_id in ("2069691213314072577", "FB1"):
            response = self.post(READINGS_URL, raw(device_id, value=10))
            self.assertEqual(response.status_code, 400)
            self.assertIn("terdaftar sebagai", response.json()["errors"][0]["errors"]["deviceId"][0])

    def test_registered_devices_are_never_synced_from_zk(self):
        from core import services

        DeviceList.objects.create(device_id="SOAP-ADMIN-01", type="soap")
        with mock.patch.object(services, "sync_device") as sync_device, \
                mock.patch.object(services.ZKClient, "__init__", return_value=None):
            services.sync_all()
        self.assertNotIn("SOAP-ADMIN-01", [call.args[0].id for call in sync_device.call_args_list])

    def test_invalid_item_rejects_whole_batch(self):
        DeviceList.objects.create(device_id="A", type="soap")
        response = self.post(READINGS_URL, [raw("A", value=50), raw("A", "2", battery=120)])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["errors"][0]["index"], 1)
        self.assertFalse(SensorReading.objects.exists())

    def test_url_without_trailing_slash(self):
        DeviceList.objects.create(device_id="A", type="soap")
        self.assertEqual(self.post(READINGS_URL.rstrip("/"), raw("A", value=5)).status_code, 201)

    def test_list_filters_by_toilet_and_type(self):
        DeviceList.objects.create(device_id="A", type="soap", **TOILET)
        DeviceList.objects.create(device_id="B", type="soap", **{**TOILET, "gender": "female"})
        DeviceList.objects.create(device_id="C", type="trash", **TOILET)
        self.post(READINGS_URL, [raw("A", value=1), raw("B", value=1), raw("C", value=1)])
        response = self.get(READINGS_URL, {**TOILET, "type": "soap"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([r["deviceId"] for r in response.json()["results"]], ["A"])
        self.assertEqual(len(self.get(READINGS_URL, {"deviceId": "C"}).json()["results"]), 1)

    def test_location_hierarchy_in_output_and_scope_filter(self):
        scope = make_scope()
        DeviceList.objects.create(device_id="A", type="soap", scope=scope)
        DeviceList.objects.create(device_id="B", type="soap")
        response = self.post(READINGS_URL, [raw("A", value=1), raw("B", value=1)])
        self.assertEqual([r["location"] for r in response.json()["data"]], [
            {"client": "ISS", "region": "Banten", "site": "Bintaro", "area": "Graha ISS",
             "scope": "Floor 2 - Toilet Pria", "scope_id": scope.pk},
            None,
        ])
        results = self.get(READINGS_URL, {"scope": scope.pk}).json()["results"]
        self.assertEqual([(r["deviceId"], r["location"]["scope_id"]) for r in results], [("A", scope.pk)])

    def test_non_object_body_rejected(self):
        self.assertEqual(self.post(READINGS_URL, [1, 2]).status_code, 400)
        self.assertEqual(self.post(READINGS_URL, []).status_code, 400)


class ListFilterTests(ApiTestCase):
    def test_impossible_time_filter_is_ignored_not_a_500(self):
        for params in ({"time_from": "2026-02-30T00:00:00"}, {"time_to": "2026-13-01T25:00:00"}):
            self.assertEqual(self.get(READINGS_URL, params).status_code, 200, params)
            self.assertEqual(self.get(RESPONSES_URL, params).status_code, 200, params)


class AuditFixTests(ApiTestCase):
    """Bugs 5, 7 and 8 from the 2026-10-09 audit."""

    def setUp(self):
        super().setUp()
        DeviceList.objects.create(device_id="SOAP1", type="soap", **TOILET)
        DeviceList.objects.create(device_id="FB1", type="satisfaction", **TOILET)

    def reading(self, **extra):
        return {"id": extra.pop("id", "r1"), "inputDate": "2026-10-09T10:00:00+07:00", "deviceId": "SOAP1", **extra}

    # Bug 5: without a status, the condition comes from the value.
    def test_condition_is_derived_from_value_without_status(self):
        data = self.post(READINGS_URL, self.reading(value=0)).json()["data"]
        self.assertEqual((data["status"], data["severity"]), ("Habis", "critical"))

    def test_sender_status_is_kept_as_sent(self):
        data = self.post(READINGS_URL, self.reading(value=0, status="Terisi")).json()["data"]
        self.assertEqual((data["status"], data["severity"]), ("Terisi", "normal"))

    def test_unknown_status_is_kept_and_coloured_from_value(self):
        data = self.post(READINGS_URL, self.reading(value=0, status="Rusak")).json()["data"]
        self.assertEqual((data["status"], data["severity"]), ("Rusak", "critical"))

    def test_no_status_and_no_value_stays_empty(self):
        data = self.post(READINGS_URL, self.reading()).json()["data"]
        self.assertEqual((data["status"], data["severity"]), ("", ""))

    # Bug 7: times in the future.
    def test_far_future_times_are_rejected(self):
        future = (timezone.now() + timedelta(hours=1)).isoformat()
        for field in ("inputDate", "lastOnline"):
            response = self.post(READINGS_URL, self.reading(**{field: future}))
            self.assertEqual(response.status_code, 400, field)
            self.assertIn(field, response.json()["errors"][0]["errors"])
        response = self.post(RESPONSES_URL, {"device_id": "FB1", "rating": 5, "time": future})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(SensorReading.objects.exists() or CustomerResponse.objects.exists())

    def test_small_clock_drift_is_accepted(self):
        soon = (timezone.now() + timedelta(minutes=2)).isoformat()
        self.assertEqual(self.post(READINGS_URL, self.reading(inputDate=soon)).status_code, 201)
        self.assertEqual(self.post(RESPONSES_URL, {"device_id": "FB1", "rating": 4, "time": soon}).status_code, 201)

    # Bug 8: resent ratings.
    def test_rating_with_same_id_is_stored_once(self):
        first = self.post(RESPONSES_URL, {"id": "p-1", "device_id": "FB1", "rating": 5})
        self.assertEqual((first.status_code, first.json()["created"], first.json()["data"]["id"]), (201, 1, "p-1"))
        again = self.post(RESPONSES_URL, [{"id": "p-1", "device_id": "FB1", "rating": 5},
                                          {"id": "p-2", "device_id": "FB1", "rating": 3},
                                          {"id": "p-2", "device_id": "FB1", "rating": 3}])
        self.assertEqual((again.status_code, again.json()["created"], again.json()["duplicates"]), (201, 1, 2))
        only_dupes = self.post(RESPONSES_URL, {"id": "p-1", "device_id": "FB1", "rating": 5})
        self.assertEqual((only_dupes.status_code, only_dupes.json()["created"]), (200, 0))
        self.assertEqual(CustomerResponse.objects.count(), 2)

    def test_ratings_without_id_are_all_new(self):
        body = self.post(RESPONSES_URL, [{"device_id": "FB1", "rating": 5}] * 2).json()
        self.assertEqual((body["created"], body["duplicates"]), (2, 0))
        self.assertEqual(CustomerResponse.objects.count(), 2)


class CustomerResponseTests(ApiTestCase):
    def test_post_rating_on_registered_satisfaction_device(self):
        DeviceList.objects.create(device_id="FB01", type="satisfaction")
        response = self.post(RESPONSES_URL, {"device_id": "FB01", "rating": 5, "comment": "bersih"})
        self.assertEqual(response.status_code, 201, response.json())
        self.assertEqual(CustomerResponse.objects.get().rating, 5)
        self.assertEqual(DeviceList.objects.get(device_id="FB01").type, "satisfaction")

    def test_unknown_device_is_rejected(self):
        response = self.post(RESPONSES_URL, {"device_id": "FB-UNKNOWN", "rating": 5, "comment": "bersih"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Device ID tidak terdaftar", response.json()["errors"][0]["errors"]["device_id"][0])
        self.assertFalse(DeviceList.objects.filter(device_id="FB-UNKNOWN").exists())

    def test_rating_out_of_range_rejected(self):
        DeviceList.objects.create(device_id="FB01", type="satisfaction")
        self.assertEqual(self.post(RESPONSES_URL, {"device_id": "FB01", "rating": 6}).status_code, 400)

    def test_sensor_device_cannot_send_ratings(self):
        DeviceList.objects.create(device_id="SOAP1", type="soap")
        self.assertEqual(self.post(RESPONSES_URL, {"device_id": "SOAP1", "rating": 4}).status_code, 400)

    def test_list_filters_by_toilet(self):
        DeviceList.objects.create(device_id="FB1", type="satisfaction", **TOILET)
        DeviceList.objects.create(device_id="FB2", type="satisfaction", **{**TOILET, "floor": "3"})
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
            path = "/" + url.split("/", 3)[3]
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
        devices = DeviceList.objects.filter(device_id__startswith="DUMMY-")
        self.assertEqual(
            sorted(devices.values_list("type", flat=True)),
            sorted(["soap", "toilet-paper", "tissue", "trash", "ammonia", "satisfaction"]),
        )
        self.assertFalse(devices.exclude(**TOILET).exists())
        self.assertEqual(set(SensorReading.objects.values_list("severity", flat=True)), {"critical"})
        self.assertEqual(CustomerResponse.objects.count(), 3)

    def test_uses_the_people_counter_scope(self):
        scope = make_scope()
        DeviceList.objects.filter(type="people").update(scope=scope)
        self.assertEqual(self.run_command("--ratings", "1").count("HTTP 201"), 2)
        devices = DeviceList.objects.filter(device_id__startswith="DUMMY-")
        self.assertEqual(devices.count(), 6)
        self.assertFalse(devices.exclude(scope=scope).exists())
        self.assertTrue(devices.filter(device_id=f"DUMMY-SOAP-S{scope.pk}").exists())

    def test_default_base_url_prefers_docker_alias(self):
        from washroom.management.commands import send_dummy_data as command

        with mock.patch.object(command.socket, "gethostbyname", return_value="172.18.0.2"):
            self.assertEqual(command.default_base_url(), "http://web.internal:8000")
        with mock.patch.object(command.socket, "gethostbyname", side_effect=OSError):
            self.assertEqual(command.default_base_url(), "http://127.0.0.1:8000")

    def test_unknown_scope_is_an_error(self):
        with self.assertRaises(CommandError):
            self.run_command("--scope", "999")

    def test_cleanup_removes_dummy_data_only(self):
        DeviceList.objects.create(device_id="REAL-1", type="soap")
        self.run_command()
        self.run_command("--cleanup")
        self.assertFalse(DeviceList.objects.filter(device_id__startswith="DUMMY-").exists())
        self.assertEqual(DeviceList.objects.filter(device_id__in=["REAL-1", "2069691213314072577"]).count(), 2)
        self.assertFalse(SensorReading.objects.exists() or CustomerResponse.objects.exists())
        self.assertFalse(ApiClient.objects.filter(name="dummy-tester").exists())
