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
        self.device = DeviceList.objects.get(device_id=DEVICE_ID)
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
            device_id=device_id, name=name, type=DeviceList.TYPE_PEOPLE, current_count=count, maximum_trigger=10,
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
        # Every device is its own card, people counters included (first, ONLINE badge, visitors).
        self.assertEqual([c["device"].device_id for c in response.context["device_cards"]], [DEVICE_ID])
        people = response.context["device_cards"][0]["people"]
        self.assertEqual((people["visitors"], people["current_count"], people["online"]), (2, 8, True))
        self.assertContains(response, "Hitungan 8/20")
        self.assertNotContains(response, "Segera Hadir")
        self.assertEqual(response.context["summary"]["people"]["people_in"], 2)

    def test_one_card_per_device_grouped_by_type(self):
        soap_a = DeviceList.objects.create(device_id="soap-a", type="soap", name="Sabun A", scope=self.scope)
        DeviceList.objects.create(device_id="soap-b", type="soap", name="Sabun B", scope=self.other_scope)
        DeviceList.objects.create(device_id="trash-a", type="trash", name="Sampah A", scope=self.scope)
        SensorReading.objects.create(device=soap_a, time=timezone.localtime(), level=44, battery=92,
                                     condition="Terisi", severity="normal")
        response = self.client.get("/")
        cards = response.context["device_cards"]
        # One flowing grid ordered by type, then name; no per-type headings.
        self.assertEqual([c["device"].device_id for c in cards], [DEVICE_ID, "soap-a", "soap-b", "trash-a"])
        self.assertNotContains(response, "group-title")
        soap_cards = {card["device"].device_id: card for card in cards}
        self.assertEqual(soap_cards["soap-a"]["latest"].level, 44)
        self.assertEqual(soap_cards["soap-a"]["location"], "Gedung A · Floor 10 - Toilet Pria West")
        self.assertFalse(soap_cards["soap-b"]["has_data"])
        self.assertNotContains(response, 'aria-label="Baterai Sabun A"')  # no bars in the device cards
        self.assertContains(response, "44<small>%</small>")
        self.assertContains(response, "Menunggu data")
        self.assertContains(response, "Gedung A · Floor 10 - Toilet Wanita West")
        # Inside one toilet the location line is dropped.
        scoped = self.client.get("/", {"scope": self.scope.pk})
        self.assertEqual([c["device"].device_id for c in scoped.context["device_cards"]], [DEVICE_ID, "soap-a", "trash-a"])
        self.assertNotContains(scoped, "Gedung A · Floor 10 - Toilet Pria West</span>")

    def test_satisfaction_card_per_device(self):
        feedback = DeviceList.objects.create(device_id="fb-a", type="satisfaction", name="Tombol Rating A", scope=self.scope)
        CustomerResponse.objects.create(device=feedback, time=timezone.localtime(), rating=4, comment="Cukup bersih")
        CustomerResponse.objects.create(device=feedback, time=timezone.localtime(), rating=5, comment="Bersih")
        response = self.client.get("/")
        card = response.context["device_cards"][1]  # after the people counter
        self.assertEqual((card["device"].label, card["ratings"]["total"], card["ratings"]["excellent_pct"],
                          card["latest_level"]), ("Tombol Rating A", 2, 100, "excellent"))
        self.assertNotContains(response, "level-rows level-bar")
        self.assertContains(response, "100%<small> Sangat Baik</small>")
        self.assertContains(response, "dari 2 rating")

    def test_device_cards_are_paginated(self):
        DeviceList.objects.bulk_create([
            DeviceList(device_id=f"soap-{i:02d}", type="soap", scope=self.scope) for i in range(25)
        ])
        response = self.client.get("/")
        self.assertEqual(len(response.context["device_cards"]), 20)
        self.assertEqual(response.context["device_pager"]["page"].paginator.count, 26)  # + the people counter
        page2 = self.client.get("/", {"dev_page": "2"})
        self.assertEqual(len(page2.context["device_cards"]), 6)

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
        statuses = {s["device"].device_id: s["online"] for s in response.context["sensors"]}
        self.assertEqual(statuses, {DEVICE_ID: True, "dev-b": False})
        self.assertContains(response, "Sensor tidak dapat dihubungi")
        overview = self.client.get("/")
        self.assertContains(overview, "badge badge--bad")  # the offline counter's card

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
        self.assertEqual(self.client.get("/").context["summary"]["people"]["people_in"], 2)
        self.assertEqual(self.client.get("/", {"range": "7"}).context["summary"]["people"]["people_in"], 3)
        day = old.date().isoformat()
        custom = self.client.get("/", {"start": day, "end": day})
        self.assertEqual(custom.context["summary"]["people"]["people_in"], 1)
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
        self.assertEqual(response.context["selected_device"].device_id, "dev-b")
        self.assertEqual(response.context["recap_totals"]["people_in"], 5)

    def test_device_filter_only_lists_this_module_and_toilet(self):
        DeviceList.objects.create(device_id="amonia-1", name="Sensor Amonia", type="ammonia", scope=self.scope, **LOCATION)
        self.add_people_device("dev-female", "Wanita", scope=self.other_scope)
        response = self.client.get("/people-counting/detail/", {"scope": self.scope.pk})
        self.assertEqual([d.device_id for d in response.context["module_devices"]], [DEVICE_ID])

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

    def test_recap_table_sorts_by_column_but_chart_stays_chronological(self):
        today = timezone.localtime()
        for days_ago, visitors in ((2, 5), (1, 1), (0, 3)):
            for i in range(visitors):
                EventLog.objects.create(id=f"rc-{days_ago}-{i}", time=today - timedelta(days=days_ago),
                                        device=self.device, event_type="in", recognition_target="Cross Line")

        def recap(**params):
            response = self.client.get("/people-counting/detail/", {"range": "7", **params})
            return response, [row["people_in"] for row in response.context["recap"]]

        response, default = recap()
        days = [row["period"] for row in response.context["recap"]]
        self.assertEqual(days, sorted(days, reverse=True))  # newest first without a sort
        self.assertEqual(recap(sort="people_in", dir="desc")[1], sorted(default, reverse=True))
        response, ascending = recap(sort="people_in", dir="asc")
        self.assertEqual(ascending, sorted(default))
        self.assertTrue(response.context["recap_headers"]["people_in"]["active"])
        self.assertContains(response, 'aria-sort="ascending"')
        # The chart keeps its own day order whatever the table sort is.
        chart = [bar["period"] for bar in response.context["traffic"]["bars"]]
        self.assertEqual(chart, sorted(chart))
        # The export follows the table sort.
        export = self.client.get("/people-counting/detail/recap.csv", {"range": "7", "sort": "people_in", "dir": "desc"})
        visitors_column = [int(line.split(",")[2]) for line in export.content.decode().splitlines()[1:] if line]
        self.assertEqual(visitors_column, sorted(visitors_column, reverse=True))

    def test_impossible_dates_and_times_in_the_url_do_not_crash(self):
        # Well-formed but impossible values used to raise ValueError (500) on this public dashboard.
        for url, params in (
            ("/", {"start": "2026-02-30"}),
            ("/", {"end": "2026-13-01"}),
            ("/people-counting/", {"start": "2026-02-30"}),
            ("/people-counting/detail/", {"start": "2026-02-31", "end": "2026-03-01"}),
            ("/people-counting/detail/", {"tab": "events", "time_from": "25:99", "time_to": "24:61"}),
            ("/people-counting/detail/events.csv", {"time_from": "25:99"}),
        ):
            self.assertEqual(self.client.get(url, params).status_code, 200, (url, params))

    def test_custom_range_is_capped(self):
        from dashboard.views import MAX_RANGE_DAYS

        response = self.client.get("/people-counting/detail/", {"start": "1900-01-01", "end": "2026-10-09"})
        self.assertEqual(response.status_code, 200)
        start, end = response.context["start"], response.context["end"]
        self.assertEqual((end - start).days + 1, MAX_RANGE_DAYS)
        self.assertLessEqual(len(response.context["traffic"]["bars"]), MAX_RANGE_DAYS)

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
        self.assertEqual(self.client.get("/").context["summary"]["people"]["people_in"], 7)
        self.assertEqual(self.client.get("/", {"area": self.area.pk}).context["summary"]["people"]["people_in"], 7)
        by_scope = self.client.get("/people-counting/", {"scope": self.other_scope.pk})
        self.assertEqual([s["device"].device_id for s in by_scope.context["sensors"]], ["dev-female"])
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
        self.assertEqual(response.context["summary"]["people"]["people_in"], 3)
        self.assertContains(response, "Belum diatur")
        # "All locations" includes devices without a Scope.
        self.assertEqual(self.client.get("/").context["summary"]["people"]["people_in"], 5)

    # ---------- overview: device lists, sensor filter, ids, total ----------
    def test_sensor_type_filter_and_total_devices(self):
        DeviceList.objects.create(device_id="soap-a", type="soap", scope=self.scope)
        DeviceList.objects.create(device_id="trash-a", type="trash", scope=self.scope)
        everything = self.client.get("/")
        self.assertEqual([c["type"] for c in everything.context["device_cards"]], ["people", "soap", "trash"])
        self.assertEqual(everything.context["status_counts"]["devices"], 3)
        self.assertNotContains(everything, "jenis sensor aktif")
        self.assertContains(everything, "<strong>3</strong>")
        self.assertEqual([(f["label"], f["value"], f["url"]) for f in everything.context["active_filters"]],
                         [("Periode", "Hari ini", None)])  # the period always shows; today can't be removed
        self.assertNotContains(everything, "Hapus semua filter")
        soap_only = self.client.get("/", {"sensor": "soap"})
        self.assertEqual([c["type"] for c in soap_only.context["device_cards"]], ["soap"])
        self.assertNotIn(DEVICE_ID, [c["device"].device_id for c in soap_only.context["device_cards"]])
        self.assertEqual(soap_only.context["status_counts"]["devices"], 1)
        self.assertEqual([(f["label"], f["value"]) for f in soap_only.context["active_filters"]],
                         [("Jenis", "Sabun"), ("Periode", "Hari ini")])
        self.assertIn("sensor=soap", soap_only.context["filter_query"])
        self.assertEqual(len(self.client.get("/", {"sensor": "bogus"}).context["device_cards"]), 3)  # ignored: all types

    def test_people_counter_card_status_counts(self):
        offline = self.add_people_device("pc-off", "Pintu Belakang")
        SensorLog.objects.create(device=offline, status=SensorLog.STATUS_OFFLINE, endpoint_url="x", response={})
        response = self.client.get("/")
        self.assertContains(response, "Pintu Belakang")
        # ONLINE counts as normal, OFFLINE as no data.
        self.assertEqual((response.context["status_counts"]["normal"], response.context["status_counts"]["nodata"]), (1, 1))
        self.assertEqual([c["device"].device_id for c in self.client.get("/", {"status": "nodata"}).context["device_cards"]],
                         ["pc-off"])
        cards = {c["device"].device_id: c for c in response.context["device_cards"]}
        self.assertEqual(cards[DEVICE_ID]["url"],
                         f"/people-counting/?scope={self.scope.pk}&range=today")
        demo = self.add_people_device("DEMO-PEOPLE-01", "Demo")
        demo_card = next(c for c in self.client.get("/").context["device_cards"] if c["device"].pk == demo.pk)
        self.assertIsNone(demo_card["url"])  # demo/dummy counters are not links

    # ---------- auto-refresh ----------
    def test_refresh_interval_comes_from_scheduler_config(self):
        config = SchedulerConfig.get()
        config.dashboard_refresh_seconds = 45
        config.save()
        self.assertContains(self.client.get("/people-counting/"), 'data-refresh-seconds="45"')

    def test_no_banner_when_sensor_data_is_old(self):
        log = SensorLog.objects.create(device=self.device, status=SensorLog.STATUS_ONLINE, endpoint_url="zk")
        SensorLog.objects.filter(pk=log.pk).update(time=timezone.now() - timedelta(minutes=30))
        response = self.client.get("/")
        self.assertNotIn("stale", response.context)
        self.assertNotContains(response, "menit lalu")
        self.assertContains(response, "Semua Perangkat")
        self.assertContains(response, 'class="section-divider"')

    def test_hourly_axis_ticks_are_split_from_the_peak(self):
        response = self.client.get("/people-counting/")
        ticks = response.context["chart"]["ticks"]
        self.assertEqual([t["value"] for t in ticks], [0, 0, 1, 2, 2])  # peak 2 split in four
        self.assertEqual([t["pct"] for t in ticks], ["0", "22", "44", "66", "88"])
        self.assertContains(response, 'style="--p: 22"')  # never "22,0" (invalid CSS)

    def _notify(self, day, hour, minute, status="200 OK", done_after=None):
        """A Work Order notification at day hh:mm; done_after = minutes until Algospection finished it."""
        tz = timezone.get_current_timezone()
        when = timezone.datetime.combine(day, timezone.datetime.min.time().replace(hour=hour, minute=minute), tz)
        log = NotificationLog.objects.create(device=self.device, endpoint_url="http://web.internal:8000/dummy/api_iot.php",
                                             response_status=status)
        NotificationLog.objects.filter(pk=log.pk).update(
            time=when, completed_at=when + timedelta(minutes=done_after) if done_after is not None else None)
        return log

    def test_notification_chart_without_finish_times(self):
        from dashboard import queries

        NotificationLog.objects.all().delete()
        today = timezone.localdate()
        yesterday = today - timedelta(days=1)
        self._notify(yesterday, 9, 15)
        self._notify(yesterday, 9, 57, "ERROR")
        self._notify(yesterday, 11, 30)
        self._notify(today, 9, 0)
        chart = queries.notification_chart([self.device], yesterday, today)
        self.assertFalse(chart["completion"])
        self.assertEqual(chart["series"], ("success", "failed"))  # a failed send adds the pink bar
        # Busy hours 09-11 widened to 8 hours around them.
        self.assertEqual([h["hour"] for h in chart["hours"]], list(range(7, 15)))
        nine = next(h for h in chart["hours"] if h["hour"] == 9)
        self.assertEqual([(b["key"], b["count"]) for b in nine["bars"]], [("success", 2), ("failed", 1)])
        self.assertEqual(nine["bars"][0]["pct"], "88")  # the tallest bar
        self.assertEqual([t["value"] for t in chart["count_ticks"]], [0, 1, 2])  # whole numbers, no repeats
        self.assertEqual((chart["total"], chart["failed"], chart["busiest_hour"], chart["busiest_count"]), (4, 1, 9, 3))
        self.assertEqual((chart["count"], chart["average"]), (2, 68))  # 42 + 93 min, nothing across midnight
        self.assertEqual(chart["line"], "")

        response = self.client.get("/people-counting/", {"start": yesterday.isoformat(), "end": today.isoformat(),
                                                         "scope": self.scope.pk})
        self.assertEqual(response.context["nt_chart"]["total"], 4)
        self.assertContains(response, "bar ntbar--failed")
        self.assertContains(response, "Jam tersibuk")
        self.assertNotContains(response, "ntline-area")

    def test_notification_chart_has_no_failed_bar_when_everything_was_sent(self):
        from dashboard import queries

        NotificationLog.objects.all().delete()
        day = timezone.localdate()
        self._notify(day, 9, 0)
        chart = queries.notification_chart([self.device], day, day)
        self.assertEqual(chart["series"], ("success",))
        self.assertEqual(len(chart["hours"][0]["bars"]), 1)

    def test_notification_chart_shows_average_time_to_finish(self):
        from dashboard import queries

        NotificationLog.objects.all().delete()
        day = timezone.localdate() - timedelta(days=1)
        self._notify(day, 9, 0, done_after=20)
        self._notify(day, 9, 40, done_after=50)
        self._notify(day, 12, 0, done_after=None)   # still open
        self._notify(day, 12, 30, "ERROR")          # failed send
        self._notify(day, 13, 0, done_after=10)
        chart = queries.notification_chart([self.device], day, day)
        self.assertTrue(chart["completion"])
        self.assertEqual(chart["series"], ("success", "failed"))
        by_hour = {h["hour"]: h for h in chart["hours"]}
        self.assertEqual([b["count"] for b in by_hour[9]["bars"]], [2, 0])
        self.assertEqual([b["count"] for b in by_hour[12]["bars"]], [1, 1])
        self.assertEqual((by_hour[9]["average"], by_hour[9]["finished"]), (35, 2))
        self.assertEqual(by_hour[13]["average"], 10)
        self.assertIsNone(by_hour[12]["average"])
        self.assertEqual(len(chart["line"].split()), 2)  # one point per hour with finished Work Orders
        self.assertEqual((chart["average_completion"], chart["finished"], chart["open"], chart["failed"]), (27, 3, 1, 1))
        self.assertEqual((chart["slowest_hour"], chart["slowest_average"]), (9, 35))
        self.assertEqual((chart["fastest_hour"], chart["fastest_average"]), (13, 10))
        self.assertEqual([t["value"] for t in chart["minute_ticks"]], [0, 10, 20, 30, 40])
        self.assertNotIn("target", chart)  # no SLA

        response = self.client.get("/people-counting/", {"start": day.isoformat(), "end": day.isoformat(),
                                                         "scope": self.scope.pk})
        self.assertContains(response, "ntline-area")
        self.assertContains(response, "Rata-rata waktu tuntas")
        self.assertContains(response, "27m")
        self.assertContains(response, "1 gagal terkirim")
        self.assertContains(response, "Waktu Tuntas")
        self.assertContains(response, "rata-rata 35m · 2 notifikasi")  # slowest hour 09:00
        self.assertEqual((chart["slowest_count"], chart["fastest_count"]), (2, 1))
        self.assertNotContains(response, "ntline-target")  # no SLA line

    def test_average_gap_is_combined_from_each_device_average(self):
        NotificationLog.objects.all().delete()
        other = self.add_people_device("pc-2", "Toilet Pria 2", scope=self.scope)
        day = timezone.localdate() - timedelta(days=1)
        tz = timezone.get_current_timezone()

        def notify(device, hour, minute):
            when = timezone.datetime.combine(day, timezone.datetime.min.time().replace(hour=hour, minute=minute), tz)
            log = NotificationLog.objects.create(device=device, endpoint_url="x", response_status="200 OK")
            NotificationLog.objects.filter(pk=log.pk).update(time=when)

        for hour, minute in [(8, 0), (8, 40), (9, 20)]:   # this device: gaps 40 + 40 -> 40m
            notify(self.device, hour, minute)
        for hour, minute in [(10, 0), (11, 10)]:          # other device: one gap of 70m
            notify(other, hour, minute)
        response = self.client.get("/people-counting/", {"start": day.isoformat(), "end": day.isoformat(),
                                                         "scope": self.scope.pk})
        # Each device weighs the same: (40 + 70) / 2, not the 50m average of all three gaps.
        self.assertEqual(response.context["nt_gap"], {"combined": 55, "devices": 2})
        # "All devices" is one chart with both devices summed.
        self.assertEqual(response.context["nt_chart"]["total"], 5)
        # device_averages is keyed by the numeric device pk.
        self.assertEqual(response.context["nt_chart"]["device_averages"], {self.device.pk: 40, other.pk: 70})
        # No device picker: the chart follows the location filter only.
        self.assertNotContains(response, 'name="nt_device"')
        self.assertContains(response, "gabungan rata-rata 2 device")

    def test_simulate_wo_completion_only_touches_dummy_notifications(self):
        from io import StringIO

        from django.core.management import call_command

        NotificationLog.objects.all().delete()
        day = timezone.localdate() - timedelta(days=1)
        dummy = [self._notify(day, 8 + i, 0) for i in range(4)]
        failed = self._notify(day, 13, 0, "ERROR")
        real = NotificationLog.objects.create(device=self.device, endpoint_url="https://issid-inspection.com/api_iot.php",
                                              response_status="200 OK")
        call_command("simulate_wo_completion", "--open", "1", "--seed", "1", stdout=StringIO())
        done = {log.pk: log.completed_at for log in NotificationLog.objects.all()}
        self.assertTrue(all(done[log.pk] for log in dummy[:3]))
        self.assertIsNone(done[dummy[3].pk])  # newest stays open
        self.assertIsNone(done[failed.pk])
        self.assertIsNone(done[real.pk])  # never fakes a real Algospection Work Order
        call_command("simulate_wo_completion", "--reset", stdout=StringIO())
        self.assertFalse(NotificationLog.objects.exclude(completed_at=None).exists())

    def test_gap_only_uses_notifications_from_01_to_23(self):
        from dashboard import queries

        NotificationLog.objects.all().delete()
        day = timezone.localdate() - timedelta(days=1)
        for hour, minute in [(0, 10), (0, 50), (1, 0), (1, 30), (22, 30), (23, 10), (23, 40)]:
            self._notify(day, hour, minute)
        chart = queries.notification_chart([self.device], day, day)
        # 00:xx and 23:xx are left out: only 01:00 -> 01:30 (30m) and 01:30 -> 22:30 (21h) count.
        self.assertEqual(chart["count"], 2)
        self.assertEqual((chart["fastest"]["gap"], chart["slowest"]["gap"]), (30, 1260))
        self.assertEqual(chart["total"], 7)  # the bars still count every notification

    def test_notification_chart_empty(self):
        NotificationLog.objects.all().delete()
        response = self.client.get("/people-counting/", {"scope": self.scope.pk})
        self.assertContains(response, "Belum ada notifikasi pada periode ini.")
        self.assertNotContains(response, "ntchart-bars")

    def test_all_devices_above_scope_is_one_combined_chart(self):
        NotificationLog.objects.all().delete()
        other = self.add_people_device("pc-2", "Toilet Wanita", scope=self.other_scope)
        day = timezone.localdate() - timedelta(days=1)
        self._notify(day, 9, 0)
        log = NotificationLog.objects.create(device=other, endpoint_url="x", response_status="200 OK")
        NotificationLog.objects.filter(pk=log.pk).update(time=NotificationLog.objects.exclude(pk=log.pk).get().time)
        params = {"start": day.isoformat(), "end": day.isoformat()}
        response = self.client.get("/people-counting/", params)
        self.assertNotIn("bubbles", response.context)
        self.assertEqual(response.context["nt_chart"]["total"], 2)
        self.assertEqual(next(h for h in response.context["nt_chart"]["hours"] if h["hour"] == 9)["total"], 2)
        # Narrowing the location filter to one Scope narrows the chart to its devices.
        scoped = self.client.get("/people-counting/", {**params, "scope": self.other_scope.pk})
        self.assertEqual(scoped.context["nt_chart"]["total"], 1)

    def test_sensor_table_sorts_by_column_and_status_donut(self):
        busy = self.add_people_device("pc-busy", "Pintu Utama", count=9, events=3, scope=self.other_scope)
        quiet = self.add_people_device("pc-quiet", "Wastafel", count=1)
        SensorLog.objects.create(device=quiet, status=SensorLog.STATUS_OFFLINE, endpoint_url="x", response={})
        NotificationLog.objects.create(device=busy, endpoint_url="x", response_status="ERROR")
        response = self.client.get("/people-counting/")
        rows = response.context["sensor_rows"]
        # Needs attention: offline first, then closest to the trigger (busy 9/10 = 90%, main 8/20 = 40%).
        self.assertEqual([r["device"].device_id for r in rows], ["pc-quiet", "pc-busy", DEVICE_ID])
        busy_row = rows[1]
        self.assertEqual((busy_row["visitors"], busy_row["notifications"], busy_row["failed"], busy_row["near"]),
                         (3, 1, 1, True))
        self.assertEqual(busy_row["location"], "Gedung A · Floor 10 - Toilet Wanita West")
        self.assertEqual((response.context["online_count"], response.context["offline_count"]), (2, 1))
        self.assertEqual([seg["kind"] for seg in response.context["online_donut"]], ["online", "offline"])
        # No search, status filter or sort dropdown; no "synced HH:MM" under the status.
        for gone in ('name="st_q"', 'name="st_status"', '<select name="st_sort"', "Sinkron"):
            self.assertNotContains(response, gone)

        def ids(**params):
            return [r["device"].device_id for r in self.client.get("/people-counting/", params).context["sensor_rows"]]

        self.assertEqual(ids(st_sort="sensor"), [DEVICE_ID, "pc-busy", "pc-quiet"])  # unnamed: label = ID
        self.assertEqual(ids(st_sort="sensor", st_dir="desc"), ["pc-quiet", "pc-busy", DEVICE_ID])
        self.assertEqual(ids(st_sort="location"), [DEVICE_ID, "pc-quiet", "pc-busy"])  # same toilet: by name
        self.assertEqual(ids(st_sort="count"), ["pc-quiet", DEVICE_ID, "pc-busy"])  # 10 %, 40 %, 90 % of threshold
        self.assertEqual(ids(st_sort="visitors", st_dir="desc"), ["pc-busy", DEVICE_ID, "pc-quiet"])
        self.assertEqual(ids(st_sort="status"), ["pc-quiet", DEVICE_ID, "pc-busy"])  # offline first
        self.assertEqual(ids(st_sort="notifications"), ids())  # not a sortable column: default order
        # The active header links to the opposite direction; the others start ascending.
        sorted_page = self.client.get("/people-counting/", {"st_sort": "visitors"})
        self.assertIn("st_dir=desc", sorted_page.context["st_headers"]["visitors"]["href"])
        self.assertIn("st_dir=asc", sorted_page.context["st_headers"]["sensor"]["href"])
        self.assertContains(sorted_page, 'aria-sort="ascending"')
        scoped = self.client.get("/people-counting/", {"scope": self.scope.pk})
        self.assertNotContains(scoped, "st_sort=location")

    def test_notification_and_event_logs_sort_by_column_on_both_pages(self):
        NotificationLog.objects.all().delete()
        EventLog.objects.all().delete()
        day = timezone.localdate()
        late = self._notify(day, 11, 0)
        early = self._notify(day, 9, 0, "ERROR")
        NotificationLog.objects.filter(pk=late.pk).update(wo_number="000200")
        NotificationLog.objects.filter(pk=early.pk).update(wo_number="000100")
        tz = timezone.get_current_timezone()
        for event_id, hour, event_type in [("ev-b", 8, "out"), ("ev-a", 10, "in")]:
            EventLog.objects.create(id=event_id, device=self.device, event_type=event_type, recognition_target="Cross Line",
                                    time=timezone.datetime.combine(day, timezone.datetime.min.time().replace(hour=hour), tz))

        def wo(url, **params):
            return [r["wo_number"] for r in self.client.get(url, {"range": "today", **params}).context["wo_rows"]]

        def ev(url, **params):
            return [e.id for e in self.client.get(url, {"range": "today", **params}).context["event_rows"]]

        page, detail = "/people-counting/", "/people-counting/detail/"
        # Default: newest first. People Counting uses wo_/ev_ prefixes, the detail tabs plain sort/dir.
        self.assertEqual(wo(page), ["000200", "000100"])
        self.assertEqual(wo(page, wo_sort="time"), ["000100", "000200"])
        self.assertEqual(wo(page, wo_sort="number", wo_dir="desc"), ["000200", "000100"])
        self.assertEqual(wo(page, wo_sort="status"), ["000100", "000200"])  # failed first
        self.assertEqual(ev(page, ev_sort="event"), ["ev-a", "ev-b"])
        self.assertEqual(ev(page, ev_sort="type", ev_dir="desc"), ["ev-b", "ev-a"])  # out before in
        self.assertEqual(wo(detail, tab="wo", sort="number"), ["000100", "000200"])
        self.assertEqual(ev(detail, tab="events", sort="time"), ["ev-b", "ev-a"])
        # Sorting one table leaves the other alone and returns to its first page.
        response = self.client.get(page, {"range": "today", "wo_sort": "time", "wo_page": "2"})
        headers = response.context["wo_headers"]
        self.assertIn("wo_dir=desc", headers["time"]["href"])
        self.assertNotIn("wo_page", headers["time"]["href"])
        self.assertTrue(headers["time"]["href"].endswith("#notification-log"))
        self.assertFalse(any(h["active"] for h in response.context["ev_headers"].values()))
        # The export follows the on-screen sort.
        csv = self.client.get("/people-counting/detail/wo.csv", {"range": "today", "sort": "number"}).content.decode()
        self.assertLess(csv.index("000100"), csv.index("000200"))

    def test_overview_search_by_device_name_and_id(self):
        DeviceList.objects.create(device_id="soap-a", type="soap", name="Sabun Wastafel", scope=self.scope)
        DeviceList.objects.create(device_id="trash-x9", type="trash", name="Sampah", scope=self.scope)
        by_name = self.client.get("/", {"q": "wastafel"})
        self.assertEqual([c["device"].device_id for c in by_name.context["device_cards"]], ["soap-a"])

        self.assertEqual(by_name.context["status_counts"]["devices"], 1)
        by_id = self.client.get("/", {"q": "X9"})
        self.assertEqual([c["device"].device_id for c in by_id.context["device_cards"]], ["trash-x9"])
        people = self.client.get("/", {"q": DEVICE_ID[-6:]})
        self.assertEqual([c["device"].device_id for c in people.context["device_cards"]], [DEVICE_ID])
        self.assertIn("q=", people.context["filter_query"])
        none = self.client.get("/", {"q": "zzz"})
        self.assertContains(none, "Tidak ada perangkat yang cocok")

    def test_active_filter_chips_for_location_period_and_search(self):
        response = self.client.get("/", {"scope": self.scope.pk, "range": "7", "q": "sab"})
        chips = {f["label"]: f for f in response.context["active_filters"]}
        self.assertEqual(chips["Lokasi"]["value"], "BCA › Jakarta › Thamrin › Gedung A › Floor 10 - Toilet Pria West")
        self.assertEqual(chips["Periode"]["value"], "7 hari")
        self.assertEqual(chips["Cari"]["value"], "“sab”")
        self.assertNotIn("scope=", chips["Lokasi"]["url"])
        self.assertContains(response, "Hapus semua filter")

    def test_search_forms_have_a_button_and_keep_other_params(self):
        response = self.client.get("/", {"range": "7", "q": "abc", "dev_page": "2"})
        self.assertContains(response, '<button type="submit" class="btn btn--primary">Cari</button>')
        self.assertContains(response, '<input type="hidden" name="range" value="7">')
        self.assertNotContains(response, '<input type="hidden" name="dev_page"')
        # People Counting's sensor table has no search box any more.
        self.assertNotContains(self.client.get("/people-counting/"), '<button type="submit" class="btn btn--primary">Cari</button>')

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
        soap_a = DeviceList.objects.create(device_id="soap-a", type="soap", name="Sabun A", scope=self.scope)
        soap_b = DeviceList.objects.create(device_id="soap-b", type="soap", name="Sabun B", scope=self.scope)
        soap_c = DeviceList.objects.create(device_id="soap-c", type="soap", name="Sabun C", scope=self.scope)
        DeviceList.objects.create(device_id="soap-d", type="soap", name="Sabun D", scope=self.scope)  # never sent
        trash = DeviceList.objects.create(device_id="trash-a", type="trash", name="Sampah", scope=self.scope)
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
        self.assertEqual(summary["blocks"]["trash"]["average_level"], 80)
        self.assertEqual(summary["blocks"]["soap"]["notifications"], 0)  # only people counting sends today
        self.assertNotContains(response, "berhasil ·")  # people card: total only
        self.assertNotContains(response, "Terakhir ")  # device cards: no last-time line
        self.assertEqual((summary["kpis"]["critical"], summary["kpis"]["warning"], summary["kpis"]["nodata"],
                          summary["kpis"]["low_battery"]), (1, 2, 2, 1))
        self.assertEqual([row["device"].device_id for row in summary["critical"]], ["soap-a"])
        since = timezone.localtime(summary["critical"][0]["since"])
        self.assertAlmostEqual((timezone.now() - since).total_seconds() / 60, 90, delta=1)  # streak start
        self.assertEqual(summary["people"]["people_in"], 2)
        trend = summary["people"]["trend"]
        self.assertEqual(trend["unit"], "hour")  # today: per hour up to now
        self.assertEqual(len(trend["bars"]), timezone.localtime().hour + 1)
        self.assertEqual((trend["peak"]["count"], trend["peak"]["pct"]), (2, "100.0"))
        self.assertEqual(sum(bar["show_label"] for bar in trend["bars"]), len({0, len(trend["bars"]) // 2, len(trend["bars"]) - 1}))
        week = self.client.get("/", {"range": "7"}).context["summary"]["people"]["trend"]
        self.assertEqual((week["unit"], len(week["bars"]), week["bars"][-1]["count"]), ("day", 7, 2))
        self.assertContains(response, "Jam tersibuk:")
        self.assertContains(response, "Rata-rata kepenuhan 80%")
        self.assertContains(response, "Ringkasan sensor")
        self.assertContains(response, "Semua Perangkat")
        self.assertContains(response, 'href="?&amp;range=today&status=critical#all-sensors"')
        self.assertEqual(summary["kpis"]["normal"], 0)
        self.assertContains(response, 'href="?&amp;range=today#all-sensors"')  # total -> all sensors, no status
        self.assertContains(response, 'href="?&amp;range=today&status=normal#all-sensors"')
        # Clicking a status narrows the cards below.
        only = self.client.get("/", {"status": "nodata", "sensor": "soap"})
        self.assertEqual(sorted(c["device"].device_id for c in only.context["device_cards"]), ["soap-c", "soap-d"])
        self.assertNotIn(DEVICE_ID, [c["device"].device_id for c in only.context["device_cards"]])
        self.assertEqual([(f["label"], f["value"]) for f in only.context["active_filters"]],
                         [("Status", "Tidak ada data"), ("Jenis", "Sabun"), ("Periode", "Hari ini")])
        self.assertEqual(only.context["status_counts"]["nodata"], 2)
        self.assertContains(only, "summary-kpi--nodata is-active")
        # Each chip drops only its own filter.
        self.assertEqual(only.context["active_filters"][0]["url"], "?sensor=soap#all-sensors")
        battery = self.client.get("/", {"status": "battery"})
        self.assertEqual([c["device"].device_id for c in battery.context["device_cards"]], ["soap-a"])
        self.assertContains(battery, "🔋 12%")
        self.assertNotContains(battery, "diukur")
        everything = self.client.get("/")  # the battery badge shows on every reading card, any filter
        self.assertContains(everything, "badge badge--warn\">🔋 12%")
        self.assertContains(everything, "badge badge--ok\">🔋 80%")
        SensorReading.objects.create(device=trash, time=timezone.now(), level=10, severity="normal", condition="Normal")
        self.assertContains(self.client.get("/"), "🔋 –")  # a reading without battery
        # The type filter narrows the summary too (only that type's card); the search doesn't.
        trash_only = self.client.get("/", {"sensor": "trash", "q": "zzz"}).context["summary"]
        self.assertEqual((trash_only["kpis"]["devices"], sorted(trash_only["blocks"]), trash_only["people"]),
                         (1, ["trash"], None))
        self.assertIn("sensor=trash", self.client.get("/", {"sensor": "trash"}).context["summary_query"])
        # With one type selected, empty lists are hidden; with all types they always show.
        self.assertContains(self.client.get("/", {"sensor": ""}), "Tidak ada sensor yang perlu tindakan.", count=0)
        self.assertContains(self.client.get("/", {"range": "today"}), "class=\"summary-lists\"")
        trash_page = self.client.get("/", {"sensor": "trash"})
        self.assertNotContains(trash_page, 'class="summary-lists')  # trash: nothing critical, battery fine
        soap_page = self.client.get("/", {"sensor": "soap"})
        self.assertContains(soap_page, 'class="summary-lists"')  # soap-a: critical and low battery
        DeviceList.objects.filter(device_id="soap-a").delete()
        self._reading(soap_b, 0, "critical", "Habis")
        single = self.client.get("/", {"sensor": "soap"})
        self.assertContains(single, "summary-lists summary-lists--single")
        self.assertNotContains(single, "Semua baterai aman.")

    def test_sensor_summary_satisfaction_levels(self):
        from dashboard import queries

        self.assertEqual([queries.rating_level(r) for r in (5, 4, 3, 2, 1)],
                         ["excellent", "excellent", "average", "bad", "bad"])
        feedback = DeviceList.objects.create(device_id="fb-a", type="satisfaction", scope=self.scope)
        for rating in (5, 5, 4, 3, 1):
            CustomerResponse.objects.create(device=feedback, time=timezone.localtime(), rating=rating)
        response = self.client.get("/")
        satisfaction = response.context["summary"]["satisfaction"]
        self.assertEqual((satisfaction["total"], satisfaction["excellent_pct"]), (5, 60))
        self.assertEqual([(l["level"], l["count"], l["pct"]) for l in satisfaction["levels"]],
                         [("excellent", 3, 60), ("average", 1, 20), ("bad", 1, 20)])
        self.assertContains(response, "60%<small> Sangat Baik</small>")
        self.assertContains(response, "Buruk")

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
        self.assertContains(follow_up, "Total devices")

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
        DeviceList.objects.create(device_id="amonia-1", type="ammonia", **LOCATION)
        with mock.patch.object(services, "ZKClient"), mock.patch.object(services, "sync_device") as sync:
            services.sync_all()
        self.assertEqual([call.args[0].device_id for call in sync.call_args_list], [DEVICE_ID])
