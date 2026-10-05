from datetime import timedelta
from functools import wraps
from urllib.parse import urlencode

import tablib
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.utils.text import slugify
from django.utils.translation import override

from core.models import DeviceList, SchedulerConfig

from . import i18n, queries

PAGE_SIZE = 25
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


def _selection(request, lang):
    """Resolve the building/floor/gender filters to one toilet, plus the selected date.

    Returns None when no device is registered at all.
    """
    params = request.GET
    devices = DeviceList.objects.order_by("building", "floor", "gender", "id")
    if not devices.exists():
        return None
    located = devices.exclude(building="")
    chosen = located.filter(
        **{field: params[field] for field in ("building", "floor", "gender") if params.get(field)}
    ).first()
    anchor = chosen or located.first() or devices.first()
    toilet = {
        "building": anchor.building,
        "floor": anchor.floor,
        "gender": anchor.gender,
        "gender_label": i18n.GENDER_LABELS[lang].get(anchor.gender, ""),
    }

    today = timezone.localdate()
    day = min(parse_date(params.get("date") or "") or today, today)
    toilet_query = urlencode({k: toilet[k] for k in ("building", "floor", "gender")})
    config = SchedulerConfig.get()
    options = queries.location_options()
    options["genders"] = [(value, i18n.GENDER_LABELS[lang][value]) for value, _ in options["genders"]]
    return {
        **_base_context(lang, config),
        "toilet": toilet,
        "day": day,
        "is_today": day == today,
        "prev_day": day - timedelta(days=1),
        "next_day": day + timedelta(days=1) if day < today else None,
        "options": options,
        # Query strings: nav_query keeps the toilet (+ device on module pages) for date links.
        "nav_query": toilet_query,
        "filter_query": f"{toilet_query}&date={day:%Y-%m-%d}",
    }


def _base_context(lang, config=None):
    config = config or SchedulerConfig.get()
    return {
        "lang": lang,
        "languages": i18n.LANGUAGES,
        "t": i18n.STRINGS[lang],
        "refresh_seconds": config.dashboard_refresh_seconds,
        "sync_interval_seconds": config.interval_seconds,
    }


def _module_devices(ctx, module, selected_id=""):
    """Devices of one module (DeviceList.type) in the selected toilet; `selected_id` narrows to one."""
    toilet = ctx["toilet"]
    devices = list(DeviceList.objects.filter(
        type=module, building=toilet["building"], floor=toilet["floor"], gender=toilet["gender"],
    ).order_by("name", "id"))
    selected = next((d for d in devices if d.id == selected_id), None)
    if selected:
        ctx["nav_query"] += f"&device={selected.id}"
        ctx["filter_query"] += f"&device={selected.id}"
    ctx.update(module_devices=devices, selected_device=selected)
    return [selected] if selected else devices


def _stale_alert(ctx, devices):
    """Minutes since sensor data last arrived, when that is too long ago to trust today's figures."""
    if not ctx["is_today"] or not devices:
        return None
    last = queries.last_successful_sync(devices)
    # Stale after three missed syncs, but never sooner than 5 minutes.
    threshold = max(5, 3 * ctx["sync_interval_seconds"] / 60)
    if last is None:
        return {"never": True}
    minutes = int((timezone.now() - last).total_seconds() // 60)
    return {"time": last, "minutes": minutes} if minutes > threshold else None


def _parse_local(value):
    parsed = parse_datetime(value) if value else None
    if parsed is not None and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def _no_devices(request, lang):
    return render(request, "dashboard/no_devices.html", _base_context(lang))


@localized
def overview(request, lang):
    ctx = _selection(request, lang)
    if ctx is None:
        return _no_devices(request, lang)
    devices = _module_devices(ctx, DeviceList.TYPE_PEOPLE)
    ctx["people_devices"] = len(devices)
    ctx["kpis"] = queries.kpis(devices, ctx["day"])
    ctx["stale"] = _stale_alert(ctx, devices)
    ctx["sections"] = [
        {
            **section,
            "name": ctx["t"][f"module_{section['key']}"],
            "summary": None if section.get("active") else queries.module_summary(
                section["key"], ctx["toilet"], ctx["day"],
            ),
        }
        for section in SECTIONS
    ]
    return render(request, "dashboard/overview.html", ctx)


@localized
def people_counting(request, lang):
    ctx = _selection(request, lang)
    if ctx is None:
        return _no_devices(request, lang)
    params = request.GET
    devices, day = _module_devices(ctx, DeviceList.TYPE_PEOPLE, params.get("device", "")), ctx["day"]

    recap_period = _recap_period(params)
    wo_status = params.get("status", "")
    if wo_status not in (queries.STATUS_SUCCESS, queries.STATUS_FAILED):
        wo_status = ""
    wo_from, wo_to = params.get("from", ""), params.get("to", "")
    wo_page = Paginator(
        queries.work_orders(devices, wo_status, _parse_local(wo_from), _parse_local(wo_to)), PAGE_SIZE
    ).get_page(params.get("wo_page"))

    ctx.update(
        module_name=ctx["t"]["module_people"],
        kpis=queries.kpis(devices, day),
        stale=_stale_alert(ctx, devices),
        chart=queries.hourly(devices, day),
        recap=queries.recap(devices, day, recap_period),
        recap_period=recap_period,
        recap_days=queries.RECAP_DAYS,
        recap_months=queries.RECAP_MONTHS,
        wo_page=wo_page,
        wo_rows=[{"log": log, "success": queries.is_success(log), "wo_number": queries.wo_number(log)}
                 for log in wo_page],
        wo_status=wo_status, wo_from=wo_from, wo_to=wo_to,
    )
    return render(request, "dashboard/people_counting.html", ctx)


def _recap_period(params):
    return queries.RECAP_MONTHLY if params.get("recap") == queries.RECAP_MONTHLY else queries.RECAP_DAILY


@localized
def export_recap(request, lang, fmt):
    ctx = _selection(request, lang)
    if ctx is None:
        return _no_devices(request, lang)
    t = ctx["t"]
    devices = _module_devices(ctx, DeviceList.TYPE_PEOPLE, request.GET.get("device", ""))
    monthly = _recap_period(request.GET) == queries.RECAP_MONTHLY
    data = tablib.Dataset(headers=[t["month"] if monthly else t["date"], t["visitors_in"], t["wo_sent"]])
    for row in queries.recap(devices, ctx["day"], queries.RECAP_MONTHLY if monthly else queries.RECAP_DAILY):
        data.append([row["period"].strftime("%Y-%m") if monthly else row["period"].isoformat(),
                     row["people_in"], row["sent"]])

    # rekap-<periode>-<cakupan>-<tanggal>; the scope is the device name (falls back to its id).
    scope = slugify(ctx["selected_device"].label) if ctx["selected_device"] else t["export_all_devices"]
    period = t["export_monthly"] if monthly else t["export_daily"]
    name = f"{t['export_recap']}-{period}-{scope}-{ctx['day']:%Y%m%d}"
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
