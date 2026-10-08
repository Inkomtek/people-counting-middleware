from datetime import timedelta
from functools import wraps
from urllib.parse import urlencode

import tablib
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_time
from django.utils.formats import date_format
from django.utils.text import slugify
from django.utils.translation import override

from core.models import DeviceList, SchedulerConfig

from . import i18n, locations, queries

# Rows per page: the user picks one of PAGE_SIZES per table (not remembered between visits).
PAGE_SIZES = (10, 20, 50, 100)
DETAIL_PAGE_SIZE = 20
PREVIEW_PAGE_SIZE = 10
# Sensor types in display order (overview groups, "Jenis Sensor" filter); names come from i18n ("module_<key>").
SECTIONS = [
    {"key": "people", "icon": "people"},
    {"key": "satisfaction", "icon": "smile"},
    {"key": "soap", "icon": "soap"},
    {"key": "toilet-paper", "icon": "paper"},
    {"key": "tissue", "icon": "tissue"},
    {"key": "trash", "icon": "trash"},
    {"key": "ammonia", "icon": "air"},
]
# Quick date ranges: key -> number of days ending today.
PRESETS = {"today": 1, "7": 7, "30": 30}
DETAIL_TABS = ("recap", "wo", "events")
# Default date range per detail tab when none is given (the recap chart reads best over a week).
DETAIL_DEFAULT_RANGE = {"recap": "7", "wo": "30", "events": "30"}
LANGUAGE_COOKIE_AGE = 365 * 24 * 60 * 60


def localized(view):
    """Render the view in the visitor's language (dates, numbers, UI text) and remember ?lang=."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        lang = i18n.resolve_language(request)
        with override(lang):
            response = view(request, lang, *args, **kwargs)
        if request.GET.get("lang") == lang:
            response.set_cookie(i18n.LANGUAGE_COOKIE, lang, max_age=LANGUAGE_COOKIE_AGE, samesite="Lax")
        return response
    return wrapper


def _base_context(lang, config=None):
    config = config or SchedulerConfig.get()
    return {
        "lang": lang,
        "languages": i18n.LANGUAGES,
        "t": i18n.STRINGS[lang],
        "refresh_seconds": config.dashboard_refresh_seconds,
        "sync_interval_seconds": config.interval_seconds,
    }


def _date_range(params, default_preset):
    """Resolve ?range=today|7|30 or ?start=&end= (WIB dates, inclusive) to (start, end, preset)."""
    today = timezone.localdate()
    preset = params.get("range")
    start, end = parse_date(params.get("start") or ""), parse_date(params.get("end") or "")
    if preset not in PRESETS and not (start or end):
        preset = default_preset
    if preset in PRESETS:
        return today - timedelta(days=PRESETS[preset] - 1), today, preset
    end = min(end or today, today)
    start = min(start or end, end)
    return start, end, ""


def _selection(request, lang, default_preset="today"):
    """Resolve the Client/Region/Site/Area/Scope filters to one toilet, plus the date range.

    Returns None when no device is registered at all.
    """
    params = request.GET
    location = locations.resolve(params)
    if location is None:
        return None
    start, end, preset = _date_range(params, default_preset)
    today = timezone.localdate()
    config = SchedulerConfig.get()
    range_query = f"range={preset}" if preset else f"start={start:%Y-%m-%d}&end={end:%Y-%m-%d}"
    return {
        **_base_context(lang, config),
        "location": location,
        "start": start,
        "end": end,
        "preset": preset,
        "presets": PRESETS,
        "single_day": start == end,
        "is_today": start == end == today,
        "includes_today": end == today,
        "location_query": location["query"],
        # Keeps the location and the date range when moving between pages.
        "filter_query": f"{location['query']}&{range_query}",
        "range_query": range_query,
    }


def _module_devices(ctx, module, selected_id=""):
    """Devices of one module (DeviceList.type) at the selected toilet; `selected_id` narrows to one."""
    devices = list(DeviceList.objects.filter(type=module, **ctx["location"]["device_filter"]).order_by("name", "id"))
    selected = next((d for d in devices if d.id == selected_id), None)
    ctx.update(module_devices=devices, selected_device=selected)
    return [selected] if selected else devices


def _stale_alert(ctx, devices):
    """Minutes since sensor data last arrived, when that is too long ago to trust today's figures."""
    if not ctx["includes_today"] or not devices:
        return None
    last = queries.last_successful_sync(devices)
    # Stale after three missed syncs, but never sooner than 5 minutes.
    threshold = max(5, 3 * ctx["sync_interval_seconds"] / 60)
    if last is None:
        return {"never": True}
    minutes = int((timezone.now() - last).total_seconds() // 60)
    return {"time": last, "minutes": minutes} if minutes > threshold else None


def _sensor_context(ctx, devices):
    statuses = queries.sensor_statuses(devices)
    ctx.update(
        sensors=statuses,
        online_count=sum(s["online"] for s in statuses),
        offline_sensors=[s for s in statuses if not s["online"]],
        stale=_stale_alert(ctx, devices),
    )


MAX_CHART_LABELS = 15


def _label_days(traffic, monthly):
    """Axis labels for the daily/monthly traffic chart: just the day number (or month name);
    long ranges only label every Nth bar so labels never overlap."""
    bars = traffic["bars"]
    every = max(1, -(-len(bars) // MAX_CHART_LABELS))
    for i, bar in enumerate(bars):
        bar["label"] = date_format(bar["period"], "M" if monthly else "d")
        bar["show_label"] = i % every == 0
    return traffic


def _query_without(request, keys):
    """The current query string without `keys` (for links that set one of them)."""
    query = request.GET.copy()
    for key in keys:
        query.pop(key, None)
    return query.urlencode()


def _paginate(request, items, prefix="", default_size=DETAIL_PAGE_SIZE, anchor=""):
    """Paginate `items` with ?<prefix>page= and ?<prefix>size= (one of PAGE_SIZES), so several tables
    on one page page independently. Returns (page, pager) where pager drives _pager.html."""
    params = request.GET
    page_param, size_param = f"{prefix}page", f"{prefix}size"
    try:
        size = int(params.get(size_param, default_size))
    except ValueError:
        size = default_size
    if size not in PAGE_SIZES:
        size = default_size
    paginator = Paginator(items, size)
    page = paginator.get_page(params.get(page_param))
    suffix = f"#{anchor}" if anchor else ""

    def url(number):
        query = params.copy()
        query[page_param] = number
        return f"?{query.urlencode()}{suffix}"

    links = []
    for number in paginator.get_elided_page_range(page.number, on_each_side=1, on_ends=1):
        if number == Paginator.ELLIPSIS:
            links.append({"gap": True})
        else:
            links.append({"number": number, "url": url(number), "current": number == page.number})
    # The size picker resubmits every other filter and starts again at page 1.
    hidden = [(key, value) for key, values in params.lists() if key not in (page_param, size_param)
              for value in values]
    pager = {
        "page": page,
        "links": links,
        "prev_url": url(page.previous_page_number()) if page.has_previous() else "",
        "next_url": url(page.next_page_number()) if page.has_next() else "",
        "size": size,
        "sizes": PAGE_SIZES,
        "size_param": size_param,
        "hidden": hidden,
        "anchor": anchor,
    }
    return page, pager


def _no_devices(request, lang):
    return render(request, "dashboard/no_devices.html", _base_context(lang))


@localized
def overview(request, lang):
    ctx = _selection(request, lang)
    if ctx is None:
        return _no_devices(request, lang)
    # Search by device name/ID (narrows the people card and the device cards) and by location name
    # (chips that switch the header filter). Kept in the location/date links like the type filter.
    q = request.GET.get("q", "").strip()[:100]
    devices = _module_devices(ctx, DeviceList.TYPE_PEOPLE)
    if q:
        devices = [device for device in devices if q.lower() in device.id.lower() or q.lower() in (device.name or "").lower()]
        ctx["location_query"] += f"&{urlencode({'q': q})}"
        ctx["filter_query"] += f"&{urlencode({'q': q})}"
    _sensor_context(ctx, devices)
    ctx["kpis"] = queries.kpis(devices, ctx["start"], ctx["end"])
    device_filter = ctx["location"]["device_filter"]
    t = ctx["t"]

    # "Jenis Sensor" filter: one module or all; kept in the links that change location or dates.
    sensor = request.GET.get("sensor", "")
    if sensor not in {section["key"] for section in SECTIONS}:
        sensor = ""
    if sensor:
        ctx["location_query"] += f"&sensor={sensor}"
        ctx["filter_query"] += f"&sensor={sensor}"
    types = [section["key"] for section in SECTIONS if not sensor or section["key"] == sensor]

    # People counting stays one combined card shown first; every other sensor gets its own card in the
    # same grid (ordered by type, then name) and is paginated. Types without devices are not shown.
    # Sensor Summary: location + dates only (the type filter and search narrow the cards below).
    summary = queries.sensor_summary(device_filter, ctx["start"], ctx["end"])
    # Clicking a status in the summary narrows the cards to it (?status=critical|warning|normal|nodata|battery).
    status = request.GET.get("status", "")
    if status not in (*queries.STATUSES, "battery"):
        status = ""

    cards = queries.device_cards(device_filter, ctx["start"], ctx["end"], types)
    if status == "battery":
        cards = [card for card in cards if card["device"].id in summary["low_battery_ids"]]
    elif status:
        cards = [card for card in cards if summary["status_by_device"].get(card["device"].id) == status]
    if q:
        needle = q.lower()
        cards = [card for card in cards if needle in card["device"].id.lower() or needle in (card["device"].name or "").lower()]
    page, pager = _paginate(request, cards, "dev_", DETAIL_PAGE_SIZE, "devices")
    icons = {section["key"]: section["icon"] for section in SECTIONS}
    for card in page:
        card["icon"] = icons[card["type"]]
    total = DeviceList.objects.filter(**device_filter)
    if q:
        total = total.filter(queries.search_devices(q))
    ctx.update(
        sensor=sensor,
        sensor_options=[(section["key"], t[f"module_{section['key']}"]) for section in SECTIONS],
        show_people=DeviceList.TYPE_PEOPLE in types and bool(devices) and not status,
        people_name=t["module_people"],
        device_cards=list(page),
        device_pager=pager,
        total_devices=(total.filter(type=sensor) if sensor else total).count(),
        # Inside one toilet the cards drop the location line (the title already names it).
        scope_selected=ctx["location"]["selected"]["scope"] is not None,
        q=q,
        location_matches=queries.search_locations(q) if q else [],
        summary=summary,
        paper_types=["toilet-paper", "tissue"],
        low_battery_limit=queries.LOW_BATTERY,
        status=status,
        status_label=t[f"status_{status}"] if status else "",
        # Base for the summary's status links: location + dates (+ type), never the search.
        summary_query=ctx["filter_query"].split("&q=")[0],
    )
    return render(request, "dashboard/overview.html", ctx)


def _notification_traffic(request, devices, start, end, scope_selected):
    """Above Scope level (several toilets): a bubble chart of device x hour plus a summary per device.
    Inside one Scope, or for one device picked with ?nt_device=: the per-day timeline."""
    picked = next((d for d in devices if d.id == request.GET.get("nt_device")), None)
    shown = [picked] if picked else devices
    ctx = {
        "nt_devices": devices if len(devices) > 1 else [],
        "nt_device": picked,
        # The device picker is a small GET form: it resubmits every other parameter unchanged.
        "nt_hidden": [(key, value) for key, values in request.GET.lists() if key != "nt_device" for value in values],
    }
    if not picked and not scope_selected:
        ctx["bubbles"] = queries.notification_bubbles(shown, start, end)
    else:
        ctx["timeline"] = queries.notification_timeline(shown, start, end)
    return ctx


@localized
def people_counting(request, lang):
    ctx = _selection(request, lang)
    if ctx is None:
        return _no_devices(request, lang)
    devices = _module_devices(ctx, DeviceList.TYPE_PEOPLE)
    start, end = ctx["start"], ctx["end"]
    _sensor_context(ctx, devices)
    ctx.update(
        module_name=ctx["t"]["module_people"],
        kpis=queries.kpis(devices, start, end),
        chart=queries.hourly(devices, start, end),
        **_notification_traffic(request, devices, start, end, ctx["location"]["selected"]["scope"] is not None),
        # Inside one toilet the sensors drop the location line (the title already names it).
        scope_selected=ctx["location"]["selected"]["scope"] is not None,
    )
    wo_page, ctx["wo_pager"] = _paginate(request, queries.work_orders(devices, start, end), "wo_",
                                         PREVIEW_PAGE_SIZE, "notification-log")
    ev_page, ctx["event_pager"] = _paginate(request, queries.events(devices, start, end), "ev_",
                                            PREVIEW_PAGE_SIZE, "event-log")
    # Sensor table with its own filters (st_q name/ID, st_status, st_sort) and pagination (st_page/st_size).
    st_status = request.GET.get("st_status", "")
    if st_status not in ("online", "offline", "near"):
        st_status = ""
    st_sort = request.GET.get("st_sort", "attention")
    if st_sort not in queries.SENSOR_SORTS:
        st_sort = "attention"
    st_q = request.GET.get("st_q", "").strip()[:100]
    rows = queries.sensor_table(devices, start, end, st_status, st_q, st_sort)
    st_page, ctx["sensor_pager"] = _paginate(request, rows, "st_", PREVIEW_PAGE_SIZE, "sensor-table")
    online = ctx["online_count"]
    ctx.update(
        sensor_rows=list(st_page), st_status=st_status, st_sort=st_sort, st_q=st_q,
        st_sorts=queries.SENSOR_SORTS,
        near_pct=queries.NEAR_THRESHOLD,
        st_hidden=[(key, value) for key, values in request.GET.lists()
                   if key not in ("st_status", "st_sort", "st_q", "st_page") for value in values],
        st_base=_query_without(request, ("st_status", "st_page")),
        online_donut=queries.online_donut(online, len(ctx["sensors"])),
        offline_count=len(ctx["sensors"]) - online,
    )
    ctx.update(wo_rows=[queries.work_order_row(log) for log in wo_page], event_rows=list(ev_page))
    return render(request, "dashboard/people_counting.html", ctx)


@localized
def people_counting_detail(request, lang):
    # Detail tabs have their own date filters, defaulting per tab (recap 7 days, the rest 30).
    params = request.GET
    tab = params.get("tab") if params.get("tab") in DETAIL_TABS else "recap"
    ctx = _selection(request, lang, default_preset=DETAIL_DEFAULT_RANGE[tab])
    if ctx is None:
        return _no_devices(request, lang)
    devices = _module_devices(ctx, DeviceList.TYPE_PEOPLE, params.get("device", ""))
    start, end = ctx["start"], ctx["end"]
    ctx.update(module_name=ctx["t"]["module_people"], tab=tab)

    if tab == "recap":
        period = _recap_period(params)
        rows, totals = queries.recap(devices, start, end, period)
        recap_page, ctx["pager"] = _paginate(request, rows, anchor="table")
        ctx.update(recap=list(recap_page), recap_totals=totals, recap_period=period,
                   traffic=_label_days(queries.period_traffic(rows, start, end, period),
                                       monthly=period == queries.RECAP_MONTHLY))
    elif tab == "wo":
        status = params.get("status", "")
        if status not in (queries.STATUS_SUCCESS, queries.STATUS_FAILED):
            status = ""
        query = params.get("q", "").strip()
        page, ctx["pager"] = _paginate(request, queries.work_orders(devices, start, end, status, query), anchor="table")
        ctx.update(wo_rows=[queries.work_order_row(log) for log in page], wo_status=status, wo_query=query)
    else:
        time_from, time_to = parse_time(params.get("time_from") or ""), parse_time(params.get("time_to") or "")
        event_type = params.get("event_type", "")
        page, ctx["pager"] = _paginate(
            request, queries.events(devices, start, end, time_from, time_to, event_type), anchor="table",
        )
        ctx.update(event_rows=list(page), event_types=queries.EVENT_LOG_TYPES,
                   event_type=event_type, time_from=params.get("time_from", ""), time_to=params.get("time_to", ""))
    return render(request, "dashboard/people_counting_detail.html", ctx)


def _recap_period(params):
    return queries.RECAP_MONTHLY if params.get("recap") == queries.RECAP_MONTHLY else queries.RECAP_DAILY


@localized
def export(request, lang, kind, fmt):
    """CSV/XLSX of the detail tab's data with the same filters as on screen."""
    if kind not in DETAIL_TABS:
        raise Http404
    ctx = _selection(request, lang, default_preset=DETAIL_DEFAULT_RANGE[kind])
    if ctx is None:
        raise Http404
    params, t = request.GET, ctx["t"]
    devices = _module_devices(ctx, DeviceList.TYPE_PEOPLE, params.get("device", ""))
    start, end = ctx["start"], ctx["end"]

    if kind == "recap":
        monthly = _recap_period(params) == queries.RECAP_MONTHLY
        data = tablib.Dataset(headers=[t["month"] if monthly else t["date"], t["events_received"], t["visitors_in"],
                                       t["wo_sent"], t["success"], t["failed"]])
        rows, _ = queries.recap(devices, start, end, queries.RECAP_MONTHLY if monthly else queries.RECAP_DAILY)
        for row in rows:
            data.append([row["period"].strftime("%Y-%m") if monthly else row["period"].isoformat(),
                         row["total"], row["people_in"], row["sent"], row["success"], row["failed"]])
        prefix = f"{t['export_recap']}-{t['export_monthly'] if monthly else t['export_daily']}"
    elif kind == "wo":
        status = params.get("status", "")
        data = tablib.Dataset(headers=[t["time"], t["wo_number"], t["device_id"], t["destination"], t["status"]])
        for log in queries.work_orders(devices, start, end, status, params.get("q", "").strip()):
            row = queries.work_order_row(log)
            data.append([_local(log.time), row["wo_number"], log.device_id, row["destination"], log.response_status])
        prefix = t["export_wo"]
    else:
        data = tablib.Dataset(headers=[t["time"], t["device_id"], t["event_id"], t["event_type"],
                                       t["recognition_target"], t["counted"]])
        logs = queries.events(devices, start, end, parse_time(params.get("time_from") or ""),
                              parse_time(params.get("time_to") or ""), params.get("event_type", ""))
        for log in logs:
            data.append([_local(log.time), log.device_id, log.id, log.event_type,
                         log.recognition_target, t["yes"] if log.counted else t["no"]])
        prefix = t["export_events"]

    # <jenis>-<cakupan>-<dari>-<sampai>; the scope is the device name (falls back to its id).
    scope = slugify(ctx["selected_device"].label) if ctx["selected_device"] else t["export_all_devices"]
    name = f"{prefix}-{scope}-{start:%Y%m%d}-{end:%Y%m%d}"
    if fmt == "xlsx":
        response = HttpResponse(
            data.export("xlsx"),
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    else:
        fmt = "csv"
        response = HttpResponse(data.export("csv"), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{name}.{fmt}"'
    return response


def _local(value):
    return timezone.localtime(value).strftime("%Y-%m-%d %H:%M:%S")
