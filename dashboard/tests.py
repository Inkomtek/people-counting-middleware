from unittest import mock

from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import override

from core import services
from core.models import DeviceList, EventLog, NotificationLog, SchedulerConfig, SensorLog
from washroom.models import CustomerResponse, SensorReading

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

    def test_overview_shows_sensor_data_for_selected_toilet(self):
        soap = DeviceList.objects.create(id="soap-2-male", type="soap", name="Soap dispenser", **LOCATION)
        SensorReading.objects.create(
            device=soap, time=timezone.localtime(), level=44, battery=92, condition="Terisi", severity="normal",
        )
        DeviceList.objects.create(id="soap-3-female", type="soap", **{**LOCATION, "floor": "3", "gender": "female"})
        response = self.client.get("/")
        soap_section = next(section for section in response.context["sections"] if section["key"] == "soap")
        self.assertTrue(soap_section["summary"]["has_data"])
        self.assertEqual(soap_section["summary"]["latest"].level, 44)
        self.assertContains(response, "44")
        self.assertContains(response, "92%")
        self.assertContains(response, 'aria-label="Baterai Soap dispenser"')
        self.assertContains(response, 'style="width: 92%"')
        self.assertContains(response, "Soap dispenser")
        self.assertContains(response, "Segera Hadir", count=5)

    def test_overview_shows_customer_rating_summary(self):
        feedback = DeviceList.objects.create(id="feedback-2-male", type="satisfaction", **LOCATION)
        CustomerResponse.objects.create(device=feedback, time=timezone.localtime(), rating=4, comment="Cukup bersih")
        CustomerResponse.objects.create(device=feedback, time=timezone.localtime(), rating=5, comment="Bersih")
        response = self.client.get("/")
        section = next(section for section in response.context["sections"] if section["key"] == "satisfaction")
        self.assertTrue(section["summary"]["has_data"])
        self.assertEqual(section["summary"]["average_rating"], 4.5)
        self.assertEqual(section["summary"]["rating_progress"], 90)
        self.assertContains(response, "4.5")
        self.assertContains(response, "90%")
        self.assertContains(response, "Rating terakhir: 5/5")
        self.assertContains(response, "Bersih")
        self.assertContains(response, "2 rating")

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
        self.assertEqual(response.context["recap"][0]["sent"], 2)
        self.assertNotContains(response, "Event Diterima")

    def test_all_devices_are_summed_by_default(self):
        self.add_people_device("dev-b", "Pintu Belakang", count=3, events=5)
        response = self.client.get("/people-counting/")
        kpis = response.context["kpis"]
        self.assertEqual(kpis["people_in"], 7)
        self.assertEqual(len(kpis["counters"]), 2)
        self.assertContains(response, "Semua perangkat (2)")

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

    def test_work_order_status_labels_and_no_detail(self):
        response = self.client.get("/people-counting/")
        self.assertContains(response, ">Berhasil</span>")
        self.assertContains(response, ">Gagal</span>")
        self.assertNotContains(response, "200 OK")
        self.assertNotContains(response, "data-drawer")
        self.assertNotContains(response, "iss_secret_value")

    def test_work_order_status_filter(self):
        response = self.client.get("/people-counting/", {"status": "failed"})
        self.assertEqual([row["success"] for row in response.context["wo_rows"]], [False])
        response = self.client.get("/people-counting/", {"status": "success"})
        self.assertEqual([row["success"] for row in response.context["wo_rows"]], [True])
        response = self.client.get("/people-counting/", {"status": "bogus"})
        self.assertEqual(len(response.context["wo_rows"]), 2)

    def test_device_log_and_sensor_status_are_hidden(self):
        response = self.client.get("/people-counting/")
        self.assertNotContains(response, "Log Perangkat")
        self.assertNotContains(response, "Status sensor")

    def test_monthly_recap(self):
        response = self.client.get("/people-counting/", {"recap": "monthly"})
        rows = response.context["recap"]
        today = timezone.localdate()
        self.assertEqual((rows[0]["period"], rows[0]["people_in"], rows[0]["sent"]), (today.replace(day=1), 2, 2))
        self.assertContains(response, "Rekap Bulanan")

    def test_recap_exports(self):
        csv = self.client.get("/people-counting/rekap.csv")
        self.assertEqual(csv.status_code, 200)
        self.assertEqual(csv.content.decode().splitlines()[0], "Tanggal,Pengunjung Masuk,Work Order Terkirim")
        self.assertIn("rekap-harian-semua-perangkat-", csv["Content-Disposition"])
        monthly = self.client.get("/people-counting/rekap.csv", {"recap": "monthly"})
        self.assertTrue(monthly.content.decode().startswith("Bulan,"))
        self.assertIn("rekap-bulanan", monthly["Content-Disposition"])
        xlsx = self.client.get("/people-counting/rekap.xlsx", {"device": DEVICE_ID})
        self.assertTrue(xlsx.content.startswith(b"PK"))
        self.assertIn(DEVICE_ID, xlsx["Content-Disposition"])

    def test_unknown_floor_falls_back_to_a_toilet(self):
        response = self.client.get("/", {"floor": "99"})
        self.assertEqual(response.context["toilet"]["floor"], "2")

    # ---------- auto-refresh ----------
    def test_refresh_interval_comes_from_scheduler_config(self):
        config = SchedulerConfig.get()
        config.dashboard_refresh_seconds = 45
        config.save()
        response = self.client.get("/people-counting/")
        self.assertContains(response, 'data-refresh-seconds="45"')

    def test_stale_banner_when_sensor_data_stopped(self):
        log = SensorLog.objects.create(device=self.device, status=SensorLog.STATUS_ONLINE, endpoint_url="zk")
        SensorLog.objects.filter(pk=log.pk).update(time=timezone.now() - timedelta(minutes=30))
        response = self.client.get("/")
        self.assertEqual(response.context["stale"]["minutes"], 30)
        self.assertContains(response, "30 menit lalu")

    def test_no_stale_banner_when_data_is_fresh_or_day_is_past(self):
        SensorLog.objects.create(device=self.device, status=SensorLog.STATUS_ONLINE, endpoint_url="zk")
        self.assertIsNone(self.client.get("/").context["stale"])
        yesterday = timezone.localdate() - timedelta(days=1)
        self.assertIsNone(self.client.get("/", {"date": yesterday.isoformat()}).context["stale"])

    # ---------- busiest hour ----------
    def test_busiest_hour(self):
        chart = self.client.get("/people-counting/").context["chart"]
        self.assertEqual(chart["busiest_hour"], timezone.localtime().hour)
        self.assertEqual(chart["peak"], 2)

    # ---------- language ----------
    def test_indonesian_is_default_with_indonesian_dates_and_numbers(self):
        self.add_people_device("dev-big", "Besar", events=0)
        EventLog.objects.bulk_create([
            EventLog(id=f"bulk{i}", time=timezone.localtime(), device=self.device, event_type="in",
                     recognition_target="Cross Line")
            for i in range(1500)
        ])
        response = self.client.get("/people-counting/")
        self.assertContains(response, '<html lang="id">')
        self.assertContains(response, "Penghitung Pengunjung")
        self.assertContains(response, "1.502")
        with override("id"):
            self.assertContains(response, date_format(timezone.localdate(), "D, d M Y"))
        with override("en"):
            self.assertNotContains(response, date_format(timezone.localdate(), "D, d M Y"))

    def test_english_via_lang_param_is_remembered(self):
        response = self.client.get("/people-counting/", {"lang": "en"})
        self.assertContains(response, '<html lang="en">')
        self.assertContains(response, "Work Order History")
        self.assertContains(response, "Visitors in today")
        self.assertEqual(response.cookies["wd_lang"].value, "en")
        follow_up = self.client.get("/")
        self.assertContains(follow_up, "Visitors In Today")
        self.assertContains(follow_up, "Coming Soon")

    def test_english_export(self):
        self.client.cookies["wd_lang"] = "en"
        csv = self.client.get("/people-counting/rekap.csv")
        self.assertEqual(csv.content.decode().splitlines()[0], "Date,Visitors In,Work Orders Sent")
        self.assertIn("recap-daily-all-devices-", csv["Content-Disposition"])

    # ---------- polish ----------
    def test_no_devices_page(self):
        DeviceList.objects.all().delete()
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Belum ada perangkat terdaftar")

    @override_settings(DEBUG=False)
    def test_custom_404(self):
        response = self.client.get("/does-not-exist/")
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "Halaman tidak ditemukan", status_code=404)

    def test_fonts_are_local(self):
        response = self.client.get("/")
        self.assertNotContains(response, "fonts.googleapis.com")
        self.assertContains(response, "favicon.svg")



class SyncScopeTests(TestCase):
    def test_sync_all_skips_non_people_devices(self):
        DeviceList.objects.create(id="amonia-1", type="ammonia", **LOCATION)
        with mock.patch.object(services, "ZKClient"), mock.patch.object(services, "sync_device") as sync:
            services.sync_all()
        self.assertEqual([call.args[0].id for call in sync.call_args_list], [DEVICE_ID])
