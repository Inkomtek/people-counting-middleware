from datetime import timedelta
from functools import wraps

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
# Overview cards, in display order; the name comes from i18n ("module_<key>").
SECTIONS = [
    {"key": "people", "icon": "people", "active": True},
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
    devices = _module_devices(ctx, DeviceList.TYPE_PEOPLE)
    _sensor_context(ctx, devices)
    ctx["kpis"] = queries.kpis(devices, ctx["start"], ctx["end"])
    sections = []
    for section in SECTIONS:
        summary = None if section.get("active") else queries.module_summary(
            section["key"], ctx["location"]["device_filter"], ctx["start"], ctx["end"],
        )
        sections.append({**section, "name": ctx["t"][f"module_{section['key']}"], "summary": summary})
    ctx["sections"] = sections
    ctx["active_modules"] = (1 if devices else 0) + sum(1 for s in sections if s["summary"] and s["summary"]["has_data"])
    return render(request, "dashboard/overview.html", ctx)


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
    )
    wo_page, ctx["wo_pager"] = _paginate(request, queries.work_orders(devices, start, end), "wo_",
                                         PREVIEW_PAGE_SIZE, "notification-log")
    ev_page, ctx["event_pager"] = _paginate(request, queries.events(devices, start, end), "ev_",
                                            PREVIEW_PAGE_SIZE, "event-log")
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
