from unittest import mock

from django.test import TestCase
from django.utils import timezone

from core import services
from core.models import DeviceList, EventLog, NotificationLog
from dashboard.queries import mask_secrets

DEVICE_ID = "2069691213314072577"
LOCATION = {"building": "GRAHA ISS BINTARO", "floor": "2", "gender": "male"}


class DashboardTests(TestCase):
    def setUp(self):
        self.device = DeviceList.objects.get(id=DEVICE_ID)
        self.device.current_count = 8
        self.device.maximum_trigger = 20
        self.device.save()
        now = timezone.localtime()
        for i, event_type in enumerate(["in", "in", "out", "passby and in"]):
            EventLog.objects.create(id=f"e{i}", time=now, device=self.device, event_type=event_type,
                                    recognition_target="Cross Line")
        NotificationLog.objects.create(
            device=self.device, endpoint_url="http://127.0.0.1:8000/dummy/api_iot.php",
            body={"token": "iss_secret_value", "LOC_ID": "GRAHA ISS BINTARO"},
            response={"status": "success", "wo_id": "WO-000128"}, response_status="200 OK",
        )
        NotificationLog.objects.create(device=self.device, endpoint_url="x", response_status="ERROR",
                                       response={"error": "down"})

    def add_people_device(self, device_id, name, count=0, events=0, **location):
        device = DeviceList.objects.create(
            id=device_id, name=name, type=DeviceList.TYPE_PEOPLE, current_count=count, maximum_trigger=10,
            **{**LOCATION, **location},
        )
        for i in range(events):
            EventLog.objects.create(id=f"{device_id}-{i}", time=timezone.localtime(), device=device,
                                    event_type="in", recognition_target="Cross Line")
        return device

    def test_seeded_device_is_people_counting_with_location(self):
        self.assertEqual(self.device.type, DeviceList.TYPE_PEOPLE)
        self.assertEqual((self.device.building, self.device.floor, self.device.gender),
                         ("GRAHA ISS BINTARO", "2", "male"))

    def test_overview_is_public_and_shows_sections(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "GRAHA ISS BINTARO")
        self.assertContains(response, "Segera Hadir", count=6)
        self.assertEqual(response.context["kpis"]["people_in"], 2)

    def test_people_counting_kpis_and_tables(self):
        response = self.client.get("/people-counting/")
        self.assertEqual(response.status_code, 200)
        kpis = response.context["kpis"]
        self.assertEqual(kpis["people_in"], 2)
        self.assertEqual([(c["current_count"], c["progress"]) for c in kpis["counters"]], [(8, 40)])
        self.assertEqual((kpis["wo_sent"], kpis["wo_success"], kpis["wo_failed"]), (2, 1, 1))
        self.assertNotIn("online", kpis)
        self.assertContains(response, "WO-000128")
        self.assertEqual(response.context["recap"][0]["people_in"], 2)

    def test_all_devices_are_summed_by_default(self):
        self.add_people_device("dev-b", "Pintu Belakang", count=3, events=5)
        response = self.client.get("/people-counting/")
        kpis = response.context["kpis"]
        self.assertEqual(kpis["people_in"], 7)
        self.assertEqual(len(kpis["counters"]), 2)
        self.assertContains(response, "Semua device (2)")

    def test_device_filter_narrows_to_one_device(self):
        self.add_people_device("dev-b", "Pintu Belakang", count=3, events=5)
        response = self.client.get("/people-counting/", {"device": "dev-b"})
        self.assertEqual(response.context["selected_device"].id, "dev-b")
        self.assertEqual(response.context["kpis"]["people_in"], 5)
        self.assertEqual(response.context["wo_rows"], [])

    def test_device_filter_only_lists_this_module_and_toilet(self):
        DeviceList.objects.create(id="amonia-1", name="Sensor Amonia", type="ammonia", **LOCATION)
        self.add_people_device("dev-female", "Wanita", gender="female")
        response = self.client.get("/people-counting/", {"gender": "male"})
        self.assertEqual([d.id for d in response.context["module_devices"]], [DEVICE_ID])

    def test_drawer_masks_token(self):
        response = self.client.get("/people-counting/")
        self.assertNotContains(response, "iss_secret_value")
        self.assertContains(response, "iss_sec•••••")

    def test_work_order_search(self):
        response = self.client.get("/people-counting/", {"q": "WO-000128"})
        self.assertEqual([row["wo"] for row in response.context["wo_rows"]], ["WO-000128"])

    def test_device_log_and_sensor_status_are_hidden(self):
        response = self.client.get("/people-counting/")
        self.assertNotContains(response, "Log Perangkat")
        self.assertNotContains(response, "Status sensor")

    def test_recap_exports(self):
        csv = self.client.get("/people-counting/rekap.csv")
        self.assertEqual(csv.status_code, 200)
        self.assertIn("Orang Masuk (IN)", csv.content.decode())
        self.assertIn("semua-device", csv["Content-Disposition"])
        xlsx = self.client.get("/people-counting/rekap.xlsx", {"device": DEVICE_ID})
        self.assertTrue(xlsx.content.startswith(b"PK"))
        self.assertIn(DEVICE_ID, xlsx["Content-Disposition"])

    def test_unknown_floor_falls_back_to_a_toilet(self):
        response = self.client.get("/", {"floor": "99"})
        self.assertEqual(response.context["toilet"]["floor"], "2")

    def test_mask_secrets_nested(self):
        self.assertEqual(mask_secrets({"a": [{"token": "abcdefghij"}]}), {"a": [{"token": "abcdefg•••••"}]})


class SyncScopeTests(TestCase):
    def test_sync_all_skips_non_people_devices(self):
        DeviceList.objects.create(id="amonia-1", type="ammonia", **LOCATION)
        with mock.patch.object(services, "ZKClient"), mock.patch.object(services, "sync_device") as sync:
            services.sync_all()
        self.assertEqual([call.args[0].id for call in sync.call_args_list], [DEVICE_ID])
