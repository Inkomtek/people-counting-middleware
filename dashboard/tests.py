from unittest import mock

from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import override

from core import services
from core.models import Area, Client, DeviceList, EventLog, NotificationLog, Region, SchedulerConfig, Scope, SensorLog, Site
from washroom.models import CustomerResponse, SensorReading

DEVICE_ID = "2069691213314072577"
LOCATION = {"building": "GRAHA ISS BINTARO", "floor": "2", "gender": "male"}


class DashboardTests(TestCase):
    def setUp(self):
        client = Client.objects.create(name="BCA")
        region = Region.objects.create(name="Jakarta")
        self.site = Site.objects.create(client=client, region=region, name="Thamrin")
        self.area = Area.objects.create(site=self.site, name="Gedung A")
        self.scope = Scope.objects.create(area=self.area, name="Floor 10 - Toilet Pria West")
        self.other_scope = Scope.objects.create(area=self.area, name="Floor 10 - Toilet Wanita West")
        self.device = DeviceList.objects.get(id=DEVICE_ID)
        self.device.scope = self.scope
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

    def add_people_device(self, device_id, name, count=0, events=0, scope=None):
        device = DeviceList.objects.create(
            id=device_id, name=name, type=DeviceList.TYPE_PEOPLE, current_count=count, maximum_trigger=10,
            scope=scope or self.scope, **LOCATION,
        )
        for i in range(events):
            EventLog.objects.create(id=f"{device_id}-{i}", time=timezone.localtime(), device=device,
                                    event_type="in", recognition_target="Cross Line")
        return device

    def test_seeded_device_is_people_counting_with_location(self):
        self.assertEqual(self.device.type, DeviceList.TYPE_PEOPLE)
        self.assertEqual((self.device.building, self.device.floor, self.device.gender),
                         ("GRAHA ISS BINTARO", "2", "male"))

    def test_overview_is_public_and_hides_types_without_devices(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["show_people"])
        self.assertEqual(response.context["device_cards"], [])
        self.assertNotContains(response, "Segera Hadir")
        self.assertEqual(response.context["kpis"]["people_in"], 2)

    def test_one_card_per_device_grouped_by_type(self):
        soap_a = DeviceList.objects.create(id="soap-a", type="soap", name="Sabun A", scope=self.scope)
        DeviceList.objects.create(id="soap-b", type="soap", name="Sabun B", scope=self.other_scope)
        DeviceList.objects.create(id="trash-a", type="trash", name="Sampah A", scope=self.scope)
        SensorReading.objects.create(device=soap_a, time=timezone.localtime(), level=44, battery=92,
                                     condition="Terisi", severity="normal")
        response = self.client.get("/")
        cards = response.context["device_cards"]
        # One flowing grid ordered by type, then name; no per-type headings.
        self.assertEqual([c["device"].id for c in cards], ["soap-a", "soap-b", "trash-a"])
        self.assertNotContains(response, "group-title")
        soap_cards = {card["device"].id: card for card in cards}
        self.assertEqual(soap_cards["soap-a"]["latest"].level, 44)
        self.assertEqual(soap_cards["soap-a"]["location"], "Gedung A · Floor 10 - Toilet Pria West")
        self.assertFalse(soap_cards["soap-b"]["has_data"])
        self.assertContains(response, 'aria-label="Baterai Sabun A"')
        self.assertContains(response, 'style="width: 92%"')
        self.assertContains(response, "Menunggu data")
        self.assertContains(response, "Gedung A · Floor 10 - Toilet Wanita West")
        # Inside one toilet the location line is dropped.
        scoped = self.client.get("/", {"scope": self.scope.pk})
        self.assertEqual([c["device"].id for c in scoped.context["device_cards"]], ["soap-a", "trash-a"])
        self.assertNotContains(scoped, "Gedung A · Floor 10 - Toilet Pria West</span>")

    def test_satisfaction_card_per_device(self):
        feedback = DeviceList.objects.create(id="fb-a", type="satisfaction", name="Tombol Rating A", scope=self.scope)
        CustomerResponse.objects.create(device=feedback, time=timezone.localtime(), rating=4, comment="Cukup bersih")
        CustomerResponse.objects.create(device=feedback, time=timezone.localtime(), rating=5, comment="Bersih")
        response = self.client.get("/")
        card = response.context["device_cards"][0]
        self.assertEqual((card["device"].label, card["average_rating"], card["response_count"], card["rating_progress"]),
                         ("Tombol Rating A", 4.5, 2, 90))
        self.assertContains(response, "Rating terakhir: 5/5")
        self.assertContains(response, "2 rating")

    def test_device_cards_are_paginated(self):
        DeviceList.objects.bulk_create([
            DeviceList(id=f"soap-{i:02d}", type="soap", scope=self.scope) for i in range(25)
        ])
        response = self.client.get("/")
        self.assertEqual(len(response.context["device_cards"]), 20)
        self.assertEqual(response.context["device_pager"]["page"].paginator.count, 25)
        page2 = self.client.get("/", {"dev_page": "2"})
        self.assertEqual(len(page2.context["device_cards"]), 5)
        self.assertTrue(page2.context["show_people"])  # the people card shows on every page

    def test_people_counting_overview(self):
        response = self.client.get("/people-counting/")
        self.assertEqual(response.status_code, 200)
        kpis = response.context["kpis"]
        self.assertEqual(kpis["people_in"], 2)
        self.assertEqual((kpis["wo_sent"], kpis["wo_success"], kpis["wo_failed"]), (2, 1, 1))
        self.assertEqual([(s["current_count"], s["progress"]) for s in response.context["sensors"]], [(8, 40)])
        self.assertContains(response, "WO-000128")
        self.assertContains(response, "/dummy/api_iot.php")
        self.assertContains(response, "200 OK")
        self.assertContains(response, "Event Log")
        self.assertNotContains(response, "data-drawer")
        self.assertNotContains(response, "iss_secret_value")

    def test_sensor_status_online_offline(self):
        SensorLog.objects.create(device=self.device, status=SensorLog.STATUS_ONLINE, endpoint_url="zk")
        other = self.add_people_device("dev-b", "Pintu Belakang", count=3)
        SensorLog.objects.create(device=other, status=SensorLog.STATUS_OFFLINE, endpoint_url="zk")
        response = self.client.get("/people-counting/")
        statuses = {s["device"].id: s["online"] for s in response.context["sensors"]}
        self.assertEqual(statuses, {DEVICE_ID: True, "dev-b": False})
        self.assertContains(response, "Sensor tidak dapat dihubungi")
        overview = self.client.get("/")
        self.assertContains(overview, "1/2 ONLINE")

    def test_all_devices_are_summed(self):
        self.add_people_device("dev-b", "Pintu Belakang", count=3, events=5)
        response = self.client.get("/people-counting/")
        self.assertEqual(response.context["kpis"]["people_in"], 7)
        self.assertEqual(len(response.context["sensors"]), 2)

    # ---------- date range ----------
    def test_date_range_presets_and_custom_range(self):
        old = timezone.localtime() - timedelta(days=5)
        EventLog.objects.create(id="old", time=old, device=self.device, event_type="in",
                                recognition_target="Cross Line")
        self.assertEqual(self.client.get("/").context["kpis"]["people_in"], 2)
        self.assertEqual(self.client.get("/", {"range": "7"}).context["kpis"]["people_in"], 3)
        day = old.date().isoformat()
        custom = self.client.get("/", {"start": day, "end": day})
        self.assertEqual(custom.context["kpis"]["people_in"], 1)
        self.assertEqual(custom.context["preset"], "")

    def test_future_end_is_clamped_to_today(self):
        response = self.client.get("/", {"start": "2000-01-01", "end": "2999-01-01"})
        self.assertEqual(response.context["end"], timezone.localdate())

    # ---------- detail tabs ----------
    def test_detail_recap_tab(self):
        response = self.client.get("/people-counting/detail/")
        self.assertEqual(response.context["tab"], "recap")
        totals = response.context["recap_totals"]
        # "passby and in" is not an in/out event, so it is not counted as received.
        self.assertEqual((totals["total"], totals["people_in"], totals["sent"], totals["success"], totals["failed"]),
                         (3, 2, 2, 1, 1))
        self.assertContains(response, "Event Diterima")
        monthly = self.client.get("/people-counting/detail/", {"recap": "monthly"})
        self.assertEqual(monthly.context["recap"][0]["period"], timezone.localdate().replace(day=1))

    def test_recap_events_received_counts_only_in_and_out(self):
        for i, event_type in enumerate(["passby", "turnback", "out"]):
            EventLog.objects.create(id=f"x{i}", time=timezone.localtime(), device=self.device, event_type=event_type)
        totals = self.client.get("/people-counting/detail/").context["recap_totals"]
        self.assertEqual(totals["total"], 4)  # in, in, out (setUp) + out
        csv = self.client.get("/people-counting/detail/recap.csv").content.decode().splitlines()
        self.assertEqual(csv[1].split(",")[1], "4")

    def test_detail_work_order_tab_filters(self):
        url = "/people-counting/detail/"
        rows = self.client.get(url, {"tab": "wo", "status": "failed"}).context["wo_rows"]
        self.assertEqual([r["log"].response_status for r in rows], ["ERROR"])
        rows = self.client.get(url, {"tab": "wo", "q": "WO-000128"}).context["wo_rows"]
        self.assertEqual([r["wo_number"] for r in rows], ["WO-000128"])

    def test_detail_event_tab_filters(self):
        url = "/people-counting/detail/"
        rows = self.client.get(url, {"tab": "events", "event_type": "out"}).context["event_rows"]
        self.assertEqual([e.id for e in rows], ["e2"])
        hour = timezone.localtime().hour
        later = f"{min(hour + 1, 23):02d}:59"
        response = self.client.get(url, {"tab": "events", "time_from": later if hour < 23 else "23:59:59"})
        if hour < 23:
            self.assertEqual(response.context["event_rows"], [])
        response = self.client.get(url, {"tab": "events"})
        self.assertEqual(response.context["event_types"], ["in", "out"])
        # Only in/out events are listed: e3 ("passby and in") is hidden.
        self.assertEqual(sorted(e.id for e in response.context["event_rows"]), ["e0", "e1", "e2"])
        self.assertNotContains(response, "Track ID")
        self.assertNotContains(response, "Tinggi")

    def test_daily_traffic_chart_in_recap(self):
        yesterday = timezone.localtime() - timedelta(days=1)
        EventLog.objects.create(id="y1", time=yesterday, device=self.device, event_type="in",
                                recognition_target="Cross Line")
        response = self.client.get("/people-counting/detail/", {"range": "7"})
        traffic = response.context["traffic"]
        self.assertEqual(len(traffic["bars"]), 7)
        self.assertEqual([b["count"] for b in traffic["bars"]][-2:], [1, 2])
        # Today: 2 visitors and 2 notifications on one shared scale.
        self.assertEqual((traffic["bars"][-1]["sent"], traffic["scale"]), (2, 2))
        self.assertEqual(traffic["bars"][-1]["sent_pct"], 100)
        # Labels are just the day number, all shown for a week.
        self.assertEqual([b["label"] for b in traffic["bars"]], [b["period"].strftime("%d") for b in traffic["bars"]])
        self.assertTrue(all(b["show_label"] for b in traffic["bars"]))
        # With no range given the recap tab shows the last 7 days, the other tabs 30.
        default = self.client.get("/people-counting/detail/").context
        self.assertEqual((default["end"] - default["start"]).days, 6)
        wo_default = self.client.get("/people-counting/detail/", {"tab": "wo"}).context
        self.assertEqual((wo_default["end"] - wo_default["start"]).days, 29)
        self.assertEqual(traffic["busiest"], timezone.localdate())
        self.assertContains(response, "Lalu Lintas Harian")
        self.assertNotContains(response, "bar-wo")
        overview = self.client.get("/people-counting/")
        self.assertContains(overview, "Notifikasi terkirim")
        self.assertContains(overview, "bar-wo")
        monthly = self.client.get("/people-counting/detail/", {"range": "7", "recap": "monthly"}).context["traffic"]
        self.assertEqual(monthly["bars"][-1]["period"], timezone.localdate().replace(day=1))

    def test_no_work_order_wording_on_dashboard(self):
        for url in ("/", "/people-counting/", "/people-counting/detail/",
                    "/people-counting/detail/?tab=wo", "/people-counting/detail/?tab=events"):
            for lang in ("id", "en"):
                self.assertNotContains(self.client.get(url, {"lang": lang}), "Work Order", msg_prefix=f"{url} {lang}")

    def test_notification_log_naming(self):
        self.assertContains(self.client.get("/people-counting/"), "Log Notifikasi")
        wo = self.client.get("/people-counting/detail/wo.csv")
        self.assertIn("log-notifikasi-", wo["Content-Disposition"])
        self.assertContains(self.client.get("/people-counting/", {"lang": "en"}), "Notification Log")

    def test_device_filter_narrows_to_one_device(self):
        self.add_people_device("dev-b", "Pintu Belakang", count=3, events=5)
        response = self.client.get("/people-counting/detail/", {"device": "dev-b"})
        self.assertEqual(response.context["selected_device"].id, "dev-b")
        self.assertEqual(response.context["recap_totals"]["people_in"], 5)

    def test_device_filter_only_lists_this_module_and_toilet(self):
        DeviceList.objects.create(id="amonia-1", name="Sensor Amonia", type="ammonia", scope=self.scope, **LOCATION)
        self.add_people_device("dev-female", "Wanita", scope=self.other_scope)
        response = self.client.get("/people-counting/detail/", {"scope": self.scope.pk})
        self.assertEqual([d.id for d in response.context["module_devices"]], [DEVICE_ID])

    # ---------- exports ----------
    def test_exports(self):
        base = "/people-counting/detail/"
        recap = self.client.get(base + "recap.csv")
        self.assertEqual(recap.content.decode().splitlines()[0],
                         "Tanggal,Event Diterima,Pengunjung Masuk,Notifikasi Terkirim,Berhasil,Gagal")
        self.assertIn("rekap-harian-semua-perangkat-", recap["Content-Disposition"])
        wo = self.client.get(base + "wo.csv", {"status": "success"})
        self.assertIn("WO-000128", wo.content.decode())
        self.assertNotIn("ERROR", wo.content.decode())
        events = self.client.get(base + "events.xlsx", {"device": DEVICE_ID})
        self.assertTrue(events.content.startswith(b"PK"))
        self.assertIn(f"event-log-{DEVICE_ID}-", events["Content-Disposition"])
        self.assertEqual(self.client.get(base + "bogus.csv").status_code, 404)

    # ---------- pagination ----------
    def make_events(self, n):
        EventLog.objects.bulk_create([
            EventLog(id=f"p{i:03d}", time=timezone.localtime() - timedelta(seconds=i), device=self.device,
                     event_type="in", recognition_target="Cross Line")
            for i in range(n)
        ])

    def test_detail_table_page_size_and_pages(self):
        self.make_events(45)
        url = "/people-counting/detail/"
        response = self.client.get(url, {"tab": "events"})
        pager = response.context["pager"]
        self.assertEqual((pager["size"], len(response.context["event_rows"])), (20, 20))
        self.assertEqual([link["number"] for link in pager["links"]], [1, 2, 3])
        response = self.client.get(url, {"tab": "events", "size": "50"})
        self.assertEqual(len(response.context["event_rows"]), 48)  # 45 + e0, e1, e2 (in/out only)
        response = self.client.get(url, {"tab": "events", "size": "10", "page": "5"})
        self.assertEqual(len(response.context["event_rows"]), 8)
        self.assertContains(response, "Baris per halaman")
        # An unsupported size falls back to the default.
        self.assertEqual(self.client.get(url, {"tab": "events", "size": "7"}).context["pager"]["size"], 20)

    def test_overview_tables_page_independently(self):
        self.make_events(25)
        response = self.client.get("/people-counting/", {"ev_page": "2"})
        self.assertEqual(response.context["event_pager"]["size"], 10)
        self.assertEqual(response.context["event_pager"]["page"].number, 2)
        self.assertEqual(response.context["wo_pager"]["page"].number, 1)
        # Page links keep the other table's state and the filters.
        link = response.context["wo_pager"]["links"][0]["url"]
        self.assertIn("ev_page=2", link)
        self.assertTrue(link.endswith("#notification-log"))
        # The size picker resubmits everything except its own page/size.
        hidden = dict(response.context["event_pager"]["hidden"])
        self.assertNotIn("ev_page", hidden)

    def test_recap_table_is_paginated(self):
        response = self.client.get("/people-counting/detail/", {"range": "30", "size": "10"})
        self.assertEqual(response.context["pager"]["size"], 10)
        self.assertLessEqual(len(response.context["recap"]), 10)

    # ---------- location hierarchy ----------
    def title(self, **params):
        location = self.client.get("/", params).context["location"]
        return location["title"], location["chips"]

    def test_header_is_named_after_the_deepest_level(self):
        site, area, scope = self.site, self.area, self.scope
        client, region = site.client, site.region
        self.assertEqual(self.title(), (None, []))
        self.assertContains(self.client.get("/"), "Semua Lokasi")
        self.assertEqual(self.title(client=client.pk), ("BCA", []))
        self.assertEqual(self.title(client=client.pk, region=region.pk), ("Jakarta", ["BCA"]))
        self.assertEqual(self.title(client=client.pk, region=region.pk, site=site.pk), ("Thamrin", ["BCA", "Jakarta"]))
        self.assertEqual(self.title(site=site.pk, area=area.pk), ("Gedung A", ["BCA", "Jakarta", "Thamrin"]))
        self.assertEqual(self.title(scope=scope.pk),
                         ("Gedung A · Floor 10 - Toilet Pria West", ["BCA", "Jakarta", "Thamrin"]))
        self.assertContains(self.client.get("/", {"lang": "en"}), "<h1>All</h1>")

    def test_all_levels_sum_their_devices(self):
        self.add_people_device("dev-female", "Wanita", events=5, scope=self.other_scope)
        self.assertEqual(self.client.get("/").context["kpis"]["people_in"], 7)
        self.assertEqual(self.client.get("/", {"area": self.area.pk}).context["kpis"]["people_in"], 7)
        by_scope = self.client.get("/people-counting/", {"scope": self.other_scope.pk})
        self.assertEqual([s["device"].id for s in by_scope.context["sensors"]], ["dev-female"])
        self.assertEqual(by_scope.context["kpis"]["people_in"], 5)
        self.assertIn(f"scope={self.other_scope.pk}", by_scope.context["filter_query"])

    def test_choosing_all_resets_the_levels_below(self):
        scope, area, site = self.scope, self.area, self.site
        full = {"client": site.client.pk, "region": site.region.pk, "site": site.pk, "area": area.pk, "scope": scope.pk}
        # The form still submits the lower levels after "Semua" is chosen higher up; "all" must win.
        self.assertEqual(self.title(**{**full, "client": "all"}), (None, []))
        self.assertEqual(self.title(**{**full, "region": "all"}), ("BCA", []))
        self.assertEqual(self.title(**{**full, "site": "all"}), ("Jakarta", ["BCA"]))
        self.assertEqual(self.title(**{**full, "area": "all"}), ("Thamrin", ["BCA", "Jakarta"]))
        self.assertEqual(self.title(**{**full, "scope": "all"}), ("Gedung A", ["BCA", "Jakarta", "Thamrin"]))
        self.assertContains(self.client.get("/"), "data-location-level")

    def test_unknown_scope_is_ignored(self):
        self.assertEqual(self.title(scope="99999"), (None, []))
        self.assertEqual(self.title(area=self.area.pk, scope="99999")[0], "Gedung A")

    def test_switching_client_drops_stale_lower_levels(self):
        other_client = Client.objects.create(name="Mandiri")
        other_site = Site.objects.create(client=other_client, region=self.site.region, name="Sudirman")
        Scope.objects.create(area=Area.objects.create(site=other_site, name="Tower 1"), name="Lobby")
        # The form still submits BCA's site/area/scope after switching the client to Mandiri.
        response = self.client.get("/", {"client": other_client.pk, "region": self.site.region.pk,
                                         "site": self.site.pk, "area": self.area.pk, "scope": self.scope.pk})
        selected = response.context["location"]["selected"]
        self.assertEqual((selected["client"], selected["region"], selected["site"]),
                         (other_client, self.site.region, None))
        self.assertEqual(response.context["location"]["choices"]["site"], [other_site])

    def test_devices_without_scope(self):
        loose = self.add_people_device("loose", "Tanpa lokasi", events=3)
        DeviceList.objects.filter(pk=loose.pk).update(scope=None)
        response = self.client.get("/", {"client": "none"})
        self.assertTrue(response.context["location"]["unassigned"])
        self.assertEqual(response.context["kpis"]["people_in"], 3)
        self.assertContains(response, "Belum diatur")
        # "All locations" includes devices without a Scope.
        self.assertEqual(self.client.get("/").context["kpis"]["people_in"], 5)

    # ---------- overview: device lists, sensor filter, ids, total ----------
    def test_sensor_type_filter_and_total_devices(self):
        DeviceList.objects.create(id="soap-a", type="soap", scope=self.scope)
        DeviceList.objects.create(id="trash-a", type="trash", scope=self.scope)
        everything = self.client.get("/")
        self.assertEqual([c["type"] for c in everything.context["device_cards"]], ["soap", "trash"])
        self.assertEqual(everything.context["total_devices"], 3)
        self.assertNotContains(everything, "jenis sensor aktif")
        self.assertContains(everything, "Total perangkat: 3")
        soap_only = self.client.get("/", {"sensor": "soap"})
        self.assertEqual([c["type"] for c in soap_only.context["device_cards"]], ["soap"])
        self.assertFalse(soap_only.context["show_people"])
        self.assertEqual(soap_only.context["total_devices"], 1)
        self.assertIn("sensor=soap", soap_only.context["filter_query"])
        self.assertEqual(len(self.client.get("/", {"sensor": "bogus"}).context["device_cards"]), 2)

    def test_people_counting_ids_only_when_scope_selected(self):
        self.assertNotContains(self.client.get("/"), f"ID {DEVICE_ID}")
        self.assertContains(self.client.get("/", {"scope": self.scope.pk}), f"ID {DEVICE_ID}")

    # ---------- auto-refresh ----------
    def test_refresh_interval_comes_from_scheduler_config(self):
        config = SchedulerConfig.get()
        config.dashboard_refresh_seconds = 45
        config.save()
        self.assertContains(self.client.get("/people-counting/"), 'data-refresh-seconds="45"')

    def test_stale_banner_when_sensor_data_stopped(self):
        log = SensorLog.objects.create(device=self.device, status=SensorLog.STATUS_ONLINE, endpoint_url="zk")
        SensorLog.objects.filter(pk=log.pk).update(time=timezone.now() - timedelta(minutes=30))
        response = self.client.get("/")
        self.assertEqual(response.context["stale"]["minutes"], 30)
        self.assertContains(response, "30 menit lalu")

    def test_no_stale_banner_when_fresh_or_range_in_past(self):
        SensorLog.objects.create(device=self.device, status=SensorLog.STATUS_ONLINE, endpoint_url="zk")
        self.assertIsNone(self.client.get("/").context["stale"])
        yesterday = (timezone.localdate() - timedelta(days=1)).isoformat()
        self.assertIsNone(self.client.get("/", {"start": yesterday, "end": yesterday}).context["stale"])

    def test_hourly_axis_ticks_are_split_from_the_peak(self):
        response = self.client.get("/people-counting/")
        ticks = response.context["chart"]["ticks"]
        self.assertEqual([t["value"] for t in ticks], [0, 0, 1, 2, 2])  # peak 2 split in four
        self.assertEqual([t["pct"] for t in ticks], ["0", "22", "44", "66", "88"])
        self.assertContains(response, 'style="--p: 22"')  # never "22,0" (invalid CSS)

    def test_notification_timeline_per_day_with_gaps(self):
        from dashboard import queries

        NotificationLog.objects.all().delete()
        today = timezone.localdate()
        yesterday = today - timedelta(days=1)
        tz = timezone.get_current_timezone()

        def notify(day, hour, minute, status="200 OK"):
            log = NotificationLog.objects.create(device=self.device, endpoint_url="x", response_status=status)
            NotificationLog.objects.filter(pk=log.pk).update(
                time=timezone.datetime.combine(day, timezone.datetime.min.time().replace(hour=hour, minute=minute), tz))

        notify(yesterday, 9, 15)
        notify(yesterday, 10, 57, "ERROR")
        notify(yesterday, 11, 30)
        notify(today, 6, 0)
        timeline = queries.notification_timeline([self.device], yesterday, today)
        self.assertEqual([(row["day"], row["count"]) for row in timeline["days"]], [(today, 1), (yesterday, 3)])
        dots = timeline["days"][1]["dots"]
        self.assertEqual([d["gap"] for d in dots], [None, 102, 33])  # no gap across days
        self.assertEqual(dots[0]["pct"], "38.542")  # 09:15 of 24 h
        self.assertEqual((dots[1]["gap_label"], dots[2]["gap_label"]), (True, False))  # 33 min is too narrow
        self.assertFalse(dots[1]["success"])
        self.assertEqual((timeline["total"], timeline["count"], timeline["average"]), (4, 2, 68))
        self.assertEqual((timeline["fastest"]["gap"], timeline["slowest"]["gap"]), (33, 102))
        response = self.client.get("/people-counting/", {"start": yesterday.isoformat(), "end": today.isoformat(),
                                                         "scope": self.scope.pk})
        self.assertNotIn("bubbles", response.context)
        self.assertContains(response, "Lalu Lintas Notifikasi")
        self.assertContains(response, "<em>1j 42m</em>")
        self.assertContains(response, "timeline-dot--failed")
        self.assertContains(response, "3 notif")

    def test_notification_timeline_with_one_notification_has_no_gap(self):
        NotificationLog.objects.all()[0].delete()  # one left today: no fastest/slowest gap
        response = self.client.get("/people-counting/", {"scope": self.scope.pk, "range": "today"})
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["timeline"]["fastest"])
        self.assertContains(response, "1 notif")

    def test_notification_timeline_empty(self):
        NotificationLog.objects.all().delete()
        self.assertContains(self.client.get("/people-counting/"), "Belum ada notifikasi pada periode ini.")

    def test_notification_bubbles_above_scope(self):
        from dashboard import queries

        NotificationLog.objects.all().delete()
        other = self.add_people_device("pc-2", "Toilet Wanita", scope=self.other_scope)
        day = timezone.localdate() - timedelta(days=1)
        tz = timezone.get_current_timezone()
        for device, hour, minute in [(self.device, 9, 0), (self.device, 9, 20), (self.device, 9, 50),
                                     (other, 9, 10), (other, 14, 0)]:
            log = NotificationLog.objects.create(device=device, endpoint_url="x", response_status="200 OK")
            NotificationLog.objects.filter(pk=log.pk).update(
                time=timezone.datetime.combine(day, timezone.datetime.min.time().replace(hour=hour, minute=minute), tz))
        bubbles = queries.notification_bubbles([self.device, other], day, day)
        first, second = bubbles["rows"]
        nine = first["bubbles"][9]
        # Gaps only within the same device: 20 and 30 min, not 10 min to the other toilet.
        self.assertEqual((nine["count"], nine["average"], nine["level"], nine["size"]), (3, 25, "fast", "30"))
        self.assertEqual((second["bubbles"][9]["level"], second["bubbles"][14]["level"]), ("none", "slow"))
        self.assertEqual((first["total"], first["average"], first["fastest"]["gap"], first["slowest"]["gap"]),
                         (3, 25, 20, 30))
        self.assertEqual(second["location"], "Gedung A · Floor 10 - Toilet Wanita West")

        params = {"start": day.isoformat(), "end": day.isoformat()}
        response = self.client.get("/people-counting/", params)
        self.assertIn("bubbles", response.context)
        self.assertContains(response, "bubble bubble--fast")
        # Inside one Scope, or with one device picked, it is the timeline again.
        self.assertNotIn("bubbles", self.client.get("/people-counting/", {**params, "scope": self.scope.pk}).context)
        picked = self.client.get("/people-counting/", {**params, "nt_device": "pc-2"})
        self.assertNotIn("bubbles", picked.context)
        self.assertEqual(picked.context["timeline"]["total"], 2)
        self.assertContains(picked, '<input type="hidden" name="start"')

    def test_sensor_table_filters_sort_and_status_donut(self):
        busy = self.add_people_device("pc-busy", "Pintu Utama", count=9, events=3, scope=self.other_scope)
        quiet = self.add_people_device("pc-quiet", "Wastafel", count=1)
        SensorLog.objects.create(device=quiet, status=SensorLog.STATUS_OFFLINE, endpoint_url="x", response={})
        NotificationLog.objects.create(device=busy, endpoint_url="x", response_status="ERROR")
        response = self.client.get("/people-counting/")
        rows = response.context["sensor_rows"]
        # Needs attention: offline first, then closest to the trigger (busy 9/10 = 90%, main 8/20 = 40%).
        self.assertEqual([r["device"].id for r in rows], ["pc-quiet", "pc-busy", DEVICE_ID])
        busy_row = rows[1]
        self.assertEqual((busy_row["visitors"], busy_row["notifications"], busy_row["failed"], busy_row["near"]),
                         (3, 1, 1, True))
        self.assertEqual(busy_row["location"], "Gedung A · Floor 10 - Toilet Wanita West")
        self.assertEqual((response.context["online_count"], response.context["offline_count"]), (2, 1))
        self.assertEqual([seg["kind"] for seg in response.context["online_donut"]], ["online", "offline"])
        self.assertContains(response, "<th>Lokasi</th>")

        def ids(**params):
            return [r["device"].id for r in self.client.get("/people-counting/", params).context["sensor_rows"]]

        self.assertEqual(ids(st_status="offline"), ["pc-quiet"])
        self.assertEqual(ids(st_status="near"), ["pc-busy"])
        self.assertEqual(ids(st_q="WASTA"), ["pc-quiet"])
        self.assertEqual(ids(st_sort="visitors"), ["pc-busy", DEVICE_ID, "pc-quiet"])
        self.assertEqual(ids(st_sort="name"), [DEVICE_ID, "pc-busy", "pc-quiet"])  # unnamed: label = ID
        scoped = self.client.get("/people-counting/", {"scope": self.scope.pk})
        self.assertNotContains(scoped, "<th>Lokasi</th>")

    def test_overview_search_by_device_name_and_id(self):
        DeviceList.objects.create(id="soap-a", type="soap", name="Sabun Wastafel", scope=self.scope)
        DeviceList.objects.create(id="trash-x9", type="trash", name="Sampah", scope=self.scope)
        by_name = self.client.get("/", {"q": "wastafel"})
        self.assertEqual([c["device"].id for c in by_name.context["device_cards"]], ["soap-a"])
        self.assertFalse(by_name.context["show_people"])
        self.assertEqual(by_name.context["total_devices"], 1)
        by_id = self.client.get("/", {"q": "X9"})
        self.assertEqual([c["device"].id for c in by_id.context["device_cards"]], ["trash-x9"])
        people = self.client.get("/", {"q": DEVICE_ID[-6:]})
        self.assertTrue(people.context["show_people"])
        self.assertEqual(people.context["device_cards"], [])
        self.assertIn("q=", people.context["filter_query"])
        none = self.client.get("/", {"q": "zzz"})
        self.assertContains(none, "Tidak ada perangkat yang cocok")

    def test_overview_search_lists_matching_locations(self):
        response = self.client.get("/", {"q": "toilet wanita"})
        matches = response.context["location_matches"]
        self.assertEqual([(m["level"], m["label"]) for m in matches],
                         [("scope", "Gedung A · Floor 10 - Toilet Wanita West")])
        self.assertContains(response, f'href="?scope={self.other_scope.pk}&range=today"')
        self.assertEqual([m["level"] for m in self.client.get("/", {"q": "jakarta"}).context["location_matches"]],
                         ["region"])

    def _reading(self, device, level, severity, condition, minutes_ago=5, battery=80):
        return SensorReading.objects.create(device=device, time=timezone.now() - timedelta(minutes=minutes_ago),
                                            level=level, battery=battery, condition=condition, severity=severity)

    def test_sensor_summary_counts_statuses_and_lists(self):
        soap_a = DeviceList.objects.create(id="soap-a", type="soap", name="Sabun A", scope=self.scope)
        soap_b = DeviceList.objects.create(id="soap-b", type="soap", name="Sabun B", scope=self.scope)
        soap_c = DeviceList.objects.create(id="soap-c", type="soap", name="Sabun C", scope=self.scope)
        DeviceList.objects.create(id="soap-d", type="soap", name="Sabun D", scope=self.scope)  # never sent
        trash = DeviceList.objects.create(id="trash-a", type="trash", name="Sampah", scope=self.scope)
        self._reading(soap_a, 50, "normal", "Habis", minutes_ago=200)  # older reading, now critical:
        self._reading(soap_a, 0, "critical", "Habis", minutes_ago=90, battery=15)
        self._reading(soap_a, 0, "critical", "Habis", minutes_ago=10, battery=12)
        self._reading(soap_b, 20, "warning", "Hampir Habis")
        self._reading(soap_c, 80, "normal", "Terisi", minutes_ago=180)  # older than 2 h -> no data
        self._reading(trash, 80, "warning", "Hampir Penuh")
        response = self.client.get("/")
        summary = response.context["summary"]
        soap = summary["blocks"]["soap"]
        self.assertEqual(soap["counts"], {"critical": 1, "warning": 1, "normal": 0, "nodata": 2})
        self.assertEqual(soap["average_level"], 10)
        self.assertEqual(summary["blocks"]["trash"]["gauge"], "40")
        self.assertEqual((summary["kpis"]["critical"], summary["kpis"]["warning"], summary["kpis"]["nodata"],
                          summary["kpis"]["low_battery"]), (1, 2, 2, 1))
        self.assertEqual([row["device"].id for row in summary["critical"]], ["soap-a"])
        since = timezone.localtime(summary["critical"][0]["since"])
        self.assertAlmostEqual((timezone.now() - since).total_seconds() / 60, 90, delta=1)  # streak start
        self.assertEqual(summary["people"]["people_in"], 2)
        self.assertEqual(summary["people"]["trend"]["unit"], "hour")  # today: per hour up to now
        self.assertEqual(len(summary["people"]["trend"]["values"]), timezone.localtime().hour + 1)
        week = self.client.get("/", {"range": "7"}).context["summary"]["people"]["trend"]
        self.assertEqual((week["unit"], len(week["values"]), week["values"][-1]), ("day", 7, 2))
        self.assertContains(response, "Pengunjung per jam")
        self.assertContains(response, "Ringkasan sensor")
        self.assertContains(response, "Semua sensor")
        self.assertContains(response, 'href="?&amp;range=today&status=critical#all-sensors"')
        # Clicking a status narrows the cards below.
        only = self.client.get("/", {"status": "nodata", "sensor": "soap"})
        self.assertEqual(sorted(c["device"].id for c in only.context["device_cards"]), ["soap-c", "soap-d"])
        self.assertFalse(only.context["show_people"])
        self.assertContains(only, "Disaring: Tidak ada data")
        battery = self.client.get("/", {"status": "battery"})
        self.assertEqual([c["device"].id for c in battery.context["device_cards"]], ["soap-a"])
        # The summary ignores the type filter and the search.
        self.assertEqual(self.client.get("/", {"sensor": "trash", "q": "zzz"}).context["summary"]["kpis"]["devices"], 6)

    def test_sensor_summary_satisfaction_stars(self):
        feedback = DeviceList.objects.create(id="fb-a", type="satisfaction", scope=self.scope)
        for rating in (5, 5, 4, 1):
            CustomerResponse.objects.create(device=feedback, time=timezone.localtime(), rating=rating)
        satisfaction = self.client.get("/").context["summary"]["satisfaction"]
        self.assertEqual((satisfaction["average"], satisfaction["count"]), (3.75, 4))
        self.assertEqual([(s["stars"], s["count"], s["pct"]) for s in satisfaction["stars"]],
                         [(5, 2, "100.0"), (4, 1, "50.0"), (3, 0, "0.0"), (2, 0, "0.0"), (1, 1, "50.0")])

    def test_busiest_hour(self):
        chart = self.client.get("/people-counting/").context["chart"]
        self.assertEqual(chart["busiest_hour"], timezone.localtime().hour)
        self.assertEqual(chart["peak"], 2)

    # ---------- language ----------
    def test_indonesian_is_default_with_indonesian_dates_and_numbers(self):
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

    def test_english_via_lang_param_is_remembered(self):
        response = self.client.get("/people-counting/", {"lang": "en"})
        self.assertContains(response, '<html lang="en">')
        self.assertContains(response, "People Counting Overview")
        self.assertEqual(response.cookies["wd_lang"].value, "en")
        follow_up = self.client.get("/")
        self.assertContains(follow_up, "Sensor summary")
        self.assertContains(follow_up, "Total devices:")

    def test_english_export(self):
        self.client.cookies["wd_lang"] = "en"
        csv = self.client.get("/people-counting/detail/recap.csv")
        self.assertTrue(csv.content.decode().startswith("Date,Events Received,Visitors In,"))
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
        self.assertContains(response, "Halaman tidak ditemukan", status_code=404)

    def test_fonts_are_local(self):
        response = self.client.get("/")
        self.assertNotContains(response, "fonts.googleapis.com")
        self.assertContains(response, "favicon.svg")

    def test_no_template_comments_leak_into_pages(self):
        for url in ("/", "/people-counting/", "/people-counting/detail/", "/people-counting/detail/?tab=events"):
            self.assertNotContains(self.client.get(url), "{#", msg_prefix=url)


class SyncScopeTests(TestCase):
    def test_sync_all_skips_non_people_devices(self):
        DeviceList.objects.create(id="amonia-1", type="ammonia", **LOCATION)
        with mock.patch.object(services, "ZKClient"), mock.patch.object(services, "sync_device") as sync:
            services.sync_all()
        self.assertEqual([call.args[0].id for call in sync.call_args_list], [DEVICE_ID])
