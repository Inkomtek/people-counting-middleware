from datetime import timedelta
from functools import wraps
from urllib.parse import urlencode

import tablib
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.urls import reverse
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
    devices = list(DeviceList.objects.filter(type=module, **ctx["location"]["device_filter"]).order_by("name", "device_id"))
    selected = next((d for d in devices if d.device_id == selected_id), None)
    ctx.update(module_devices=devices, selected_device=selected)
    return [selected] if selected else devices


def _sensor_context(ctx, devices):
    statuses = queries.sensor_statuses(devices)
    ctx.update(
        sensors=statuses,
        online_count=sum(s["online"] for s in statuses),
        offline_sensors=[s for s in statuses if not s["online"]],
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


def _column_sort(request, columns, prefix="", anchor=""):
    """Sorting by clicking a column header (?<prefix>sort=<column>&<prefix>dir=asc|desc), so several tables on
    one page sort independently. Returns (sort, direction, headers); headers[column] = {"href", "active", "dir"}
    where href sorts by that column ascending, or flips the direction when it is already the active one, and
    goes back to the first page."""
    sort = request.GET.get(f"{prefix}sort", "")
    if sort not in columns:
        sort = ""
    direction = "desc" if request.GET.get(f"{prefix}dir") == "desc" else "asc"
    suffix = f"#{anchor}" if anchor else ""
    headers = {}
    for column in columns:
        query = request.GET.copy()
        query[f"{prefix}sort"] = column
        query[f"{prefix}dir"] = "desc" if column == sort and direction == "asc" else "asc"
        query.pop(f"{prefix}page", None)
        headers[column] = {"href": f"?{query.urlencode()}{suffix}", "active": column == sort, "dir": direction}
    return sort, direction, headers


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
        devices = [device for device in devices if q.lower() in device.device_id.lower() or q.lower() in (device.name or "").lower()]
        ctx["location_query"] += f"&{urlencode({'q': q})}"
        ctx["filter_query"] += f"&{urlencode({'q': q})}"
    _sensor_context(ctx, devices)
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

    # One card per device (people counting first, then the other types; by name), paginated.
    # Sensor Summary: location, dates and the type filter (the search only narrows the cards below).
    summary = queries.sensor_summary(device_filter, ctx["start"], ctx["end"], sensor)
    # Clicking a status in the summary narrows the cards to it (?status=critical|warning|normal|nodata|battery).
    status = request.GET.get("status", "")
    if status not in (*queries.STATUSES, "battery"):
        status = ""

    cards = queries.device_cards(device_filter, ctx["start"], ctx["end"], types)
    if DeviceList.TYPE_PEOPLE in types:
        people_cards = [{"device": row["device"], "type": DeviceList.TYPE_PEOPLE, "location": row["location"],
                         "has_data": True, "people": row, "url": _people_card_url(row["device"], ctx)}
                        for row in sorted(queries.sensor_table(devices, ctx["start"], ctx["end"]),
                                          key=lambda r: (r["device"].name or r["device"].device_id, r["device"].device_id))]
        cards = people_cards + cards
    # People counters have no reading: ONLINE counts as normal, OFFLINE as no data.
    card_status = dict(summary["status_by_device"])
    card_status.update({card["device"].id: "normal" if card["people"]["online"] else "nodata"
                        for card in cards if card["type"] == DeviceList.TYPE_PEOPLE})
    if q:
        needle = q.lower()
        cards = [card for card in cards if needle in card["device"].device_id.lower() or needle in (card["device"].name or "").lower()]
    # The 6 status boxes above the cards count what the cards show before the status filter
    # (location, dates, type and search all apply), so a box's number matches its result.
    statuses = [card_status.get(card["device"].id) for card in cards]
    status_counts = {
        "devices": len(cards),
        **{key: statuses.count(key) for key in queries.STATUSES},
        "battery": sum(card["device"].id in summary["low_battery_ids"] for card in cards),
    }
    if status == "battery":
        cards = [card for card in cards if card["device"].id in summary["low_battery_ids"]]
    elif status:
        cards = [card for card in cards if card_status.get(card["device"].id) == status]
    page, pager = _paginate(request, cards, "dev_", DETAIL_PAGE_SIZE, "devices")
    icons = {section["key"]: section["icon"] for section in SECTIONS}
    for card in page:
        card["icon"] = icons[card["type"]]
    ctx.update(
        sensor=sensor,
        sensor_options=[(section["key"], t[f"module_{section['key']}"]) for section in SECTIONS],
        device_cards=list(page),
        device_pager=pager,
        status_counts=status_counts,
        status_boxes=[("devices", ""), ("critical", "critical"), ("warning", "warning"), ("normal", "normal"),
                      ("nodata", "nodata"), ("battery", "battery")],
        active_filters=_active_filters(request, ctx, sensor, status, q),
        # Inside one toilet the cards drop the location line (the title already names it).
        scope_selected=ctx["location"]["selected"]["scope"] is not None,
        q=q,
        # The search form (no-JS fallback) resubmits every other parameter unchanged.
        search_hidden=[(key, value) for key, values in request.GET.lists() if key not in ("q", "dev_page")
                       for value in values],
        location_matches=queries.search_locations(q) if q else [],
        summary=summary,
        sensor_all=not sensor,  # "Semua jenis": the summary lists always show, even when empty
        low_battery_limit=queries.LOW_BATTERY,
        status=status,
        status_label=t[f"status_{status}"] if status else "",
        # Base for the summary's status links: location + dates (+ type), never the search.
        summary_query=f"{ctx['location']['query']}&{ctx['range_query']}" + (f"&sensor={sensor}" if sensor else ""),
    )
    return render(request, "dashboard/overview.html", ctx)


DEMO_PREFIXES = ("DEMO-", "DUMMY-")


def _people_card_url(device, ctx):
    """A real people counter's card opens People Counting for its toilet (or the unassigned group) at the
    sensor table; demo/dummy devices are not links."""
    if device.device_id.upper().startswith(DEMO_PREFIXES):
        return None
    where = f"scope={device.scope_id}" if device.scope_id else f"client={locations.UNASSIGNED}"
    return f"{reverse('dashboard:people_counting')}?{where}&{ctx['range_query']}#sensor-table"


def _active_filters(request, ctx, sensor, status, q):
    """Chips for the "Disaring" line above All Devices: each active filter with a link that drops just
    that filter. The period is always shown; "today" is the default, so its chip has no link."""
    t = ctx["t"]
    location = ctx["location"]

    def without(*keys):
        return f"?{_query_without(request, (*keys, 'dev_page'))}#all-sensors"

    chips = []
    if status:
        chips.append({"label": t["filter_status"], "value": t[f"status_{status}"], "url": without("status")})
    if sensor:
        chips.append({"label": t["filter_type"], "value": t[f"module_{sensor}"], "url": without("sensor")})
    if location["unassigned"]:
        chips.append({"label": t["filter_location"], "value": t["unassigned"], "url": without(*locations.LEVELS)})
    elif location["title"]:
        names = [obj.name for obj in location["selected"].values() if obj]
        chips.append({"label": t["filter_location"], "value": " › ".join(names), "url": without(*locations.LEVELS)})
    preset = "today" if ctx["is_today"] else ctx["preset"]
    value = (t[f"preset_{preset}"] if preset else
             f"{date_format(ctx['start'], 'd M Y')} – {date_format(ctx['end'], 'd M Y')}")
    chips.append({"label": t["filter_period"], "value": value,
                  "url": None if preset == "today" else without("range", "start", "end")})
    if q:
        chips.append({"label": t["filter_search"], "value": f"“{q}”", "url": without("q")})
    return chips


def _notification_traffic(devices, start, end):
    """One notification chart (queries.notification_chart) of every people device in the location filter, summed
    (no device picker: the location filter, down to one Scope, chooses the devices). The average gap between
    notifications is the average of each device's own average (each device weighs the same)."""
    chart = queries.notification_chart(devices, start, end) if devices else None
    return {
        "nt_chart": chart,
        "nt_gap": {
            "combined": chart["average"] if chart else None,
            "devices": len(chart["device_averages"]) if chart else 0,
        },
    }


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
        **_notification_traffic(devices, start, end),
        # Inside one toilet the sensors drop the location line (the title already names it).
        scope_selected=ctx["location"]["selected"]["scope"] is not None,
    )
    wo_sort, wo_dir, ctx["wo_headers"] = _column_sort(request, queries.WO_SORTS, "wo_", "notification-log")
    ev_sort, ev_dir, ctx["ev_headers"] = _column_sort(request, queries.EVENT_SORTS, "ev_", "event-log")
    wo_page, ctx["wo_pager"] = _paginate(request, queries.work_orders(devices, start, end, sort=wo_sort,
                                                                      direction=wo_dir), "wo_",
                                         PREVIEW_PAGE_SIZE, "notification-log")
    ev_page, ctx["event_pager"] = _paginate(request, queries.events(devices, start, end, sort=ev_sort,
                                                                    direction=ev_dir), "ev_",
                                            PREVIEW_PAGE_SIZE, "event-log")
    # Sensor table: no filters, sorted by clicking a column header (st_sort + st_dir), paginated (st_page/st_size).
    st_sort, st_dir, ctx["st_headers"] = _column_sort(request, queries.SENSOR_SORTS, "st_", "sensor-table")
    rows = queries.sensor_table(devices, start, end, st_sort, st_dir)
    st_page, ctx["sensor_pager"] = _paginate(request, rows, "st_", PREVIEW_PAGE_SIZE, "sensor-table")
    online = ctx["online_count"]
    ctx.update(
        sensor_rows=list(st_page),
        near_pct=queries.NEAR_THRESHOLD,
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
        sort, direction, ctx["recap_headers"] = _column_sort(request, queries.RECAP_SORTS, anchor="table")
        recap_page, ctx["pager"] = _paginate(request, queries.sort_recap(rows, sort, direction), anchor="table")
        ctx.update(recap=list(recap_page), recap_totals=totals, recap_period=period,
                   traffic=_label_days(queries.period_traffic(rows, start, end, period),
                                       monthly=period == queries.RECAP_MONTHLY))
    elif tab == "wo":
        status = params.get("status", "")
        if status not in (queries.STATUS_SUCCESS, queries.STATUS_FAILED):
            status = ""
        query = params.get("q", "").strip()
        sort, direction, ctx["wo_headers"] = _column_sort(request, queries.WO_SORTS, anchor="table")
        page, ctx["pager"] = _paginate(request, queries.work_orders(devices, start, end, status, query, sort, direction),
                                       anchor="table")
        ctx.update(wo_rows=[queries.work_order_row(log) for log in page], wo_status=status, wo_query=query)
    else:
        time_from, time_to = parse_time(params.get("time_from") or ""), parse_time(params.get("time_to") or "")
        event_type = params.get("event_type", "")
        sort, direction, ctx["ev_headers"] = _column_sort(request, queries.EVENT_SORTS, anchor="table")
        page, ctx["pager"] = _paginate(
            request, queries.events(devices, start, end, time_from, time_to, event_type, sort, direction), anchor="table",
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
        sort, direction, _ = _column_sort(request, queries.RECAP_SORTS)
        for row in queries.sort_recap(rows, sort, direction):
            data.append([row["period"].strftime("%Y-%m") if monthly else row["period"].isoformat(),
                         row["total"], row["people_in"], row["sent"], row["success"], row["failed"]])
        prefix = f"{t['export_recap']}-{t['export_monthly'] if monthly else t['export_daily']}"
    elif kind == "wo":
        status = params.get("status", "")
        data = tablib.Dataset(headers=[t["time"], t["wo_number"], t["device_id"], t["destination"], t["status"]])
        sort, direction, _ = _column_sort(request, queries.WO_SORTS)
        for log in queries.work_orders(devices, start, end, status, params.get("q", "").strip(), sort, direction):
            row = queries.work_order_row(log)
            data.append([_local(log.time), row["wo_number"], log.device.device_id, row["destination"], log.response_status])
        prefix = t["export_wo"]
    else:
        data = tablib.Dataset(headers=[t["time"], t["device_id"], t["event_id"], t["event_type"],
                                       t["recognition_target"], t["counted"]])
        sort, direction, _ = _column_sort(request, queries.EVENT_SORTS)
        logs = queries.events(devices, start, end, parse_time(params.get("time_from") or ""),
                              parse_time(params.get("time_to") or ""), params.get("event_type", ""), sort, direction)
        for log in logs:
            data.append([_local(log.time), log.device.device_id, log.id, log.event_type,
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
