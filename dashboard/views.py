from datetime import timedelta
from urllib.parse import urlencode

import tablib
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from core.models import DeviceList

from . import queries

PAGE_SIZE = 25

SECTIONS = [
    {"key": "people", "name": "People Counting", "icon": "people", "active": True},
    {"key": "satisfaction", "name": "Customer Satisfaction", "icon": "smile"},
    {"key": "soap", "name": "Soap", "icon": "soap"},
    {"key": "toilet-paper", "name": "Toilet Paper", "icon": "paper"},
    {"key": "tissue", "name": "Tissue Roll", "icon": "tissue"},
    {"key": "trash", "name": "Trash Bin", "icon": "trash"},
    {"key": "ammonia", "name": "Amonia", "icon": "air"},
]


def _selection(request):
    """Resolve the building/floor/gender filters to one toilet, plus the selected date."""
    params = request.GET
    devices = DeviceList.objects.order_by("building", "floor", "gender", "id")
    if not devices.exists():
        raise Http404("Belum ada device yang terdaftar.")
    located = devices.exclude(building="")
    chosen = located.filter(
        **{field: params[field] for field in ("building", "floor", "gender") if params.get(field)}
    ).first()
    anchor = chosen or located.first() or devices.first()
    toilet = {
        "building": anchor.building,
        "floor": anchor.floor,
        "gender": anchor.gender,
        "gender_label": anchor.get_gender_display(),
    }

    today = timezone.localdate()
    day = min(parse_date(params.get("date") or "") or today, today)
    toilet_query = urlencode({k: toilet[k] for k in ("building", "floor", "gender")})
    return {
        "toilet": toilet,
        "day": day,
        "is_today": day == today,
        "prev_day": day - timedelta(days=1),
        "next_day": day + timedelta(days=1) if day < today else None,
        "options": queries.location_options(),
        # Query strings: nav_query keeps the toilet (+ device on module pages) for date links.
        "nav_query": toilet_query,
        "filter_query": f"{toilet_query}&date={day:%Y-%m-%d}",
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


def _parse_local(value):
    parsed = parse_datetime(value) if value else None
    if parsed is not None and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def overview(request):
    ctx = _selection(request)
    devices = _module_devices(ctx, DeviceList.TYPE_PEOPLE)
    ctx["people_devices"] = len(devices)
    ctx["kpis"] = queries.kpis(devices, ctx["day"])
    ctx["sections"] = SECTIONS
    return render(request, "dashboard/overview.html", ctx)


def people_counting(request):
    ctx = _selection(request)
    params = request.GET
    devices, day = _module_devices(ctx, DeviceList.TYPE_PEOPLE, params.get("device", "")), ctx["day"]

    wo_query = params.get("q", "").strip()
    wo_from, wo_to = params.get("from", ""), params.get("to", "")
    wo_page = Paginator(
        queries.work_orders(devices, wo_query, _parse_local(wo_from), _parse_local(wo_to)), PAGE_SIZE
    ).get_page(params.get("wo_page"))

    ctx.update(
        kpis=queries.kpis(devices, day),
        chart=queries.hourly(devices, day),
        recap=queries.daily_recap(devices, day),
        recap_days=queries.RECAP_DAYS,
        wo_page=wo_page,
        wo_rows=[
            {"log": log, "wo": queries.wo_number(log), "success": queries.is_success(log),
             "detail": queries.work_order_detail(log)}
            for log in wo_page
        ],
        wo_query=wo_query, wo_from=wo_from, wo_to=wo_to,
    )
    return render(request, "dashboard/people_counting.html", ctx)


def export_recap(request, fmt):
    ctx = _selection(request)
    devices = _module_devices(ctx, DeviceList.TYPE_PEOPLE, request.GET.get("device", ""))
    data = tablib.Dataset(headers=[
        "Tanggal", "Event Diterima", "Orang Masuk (IN)", "Work Order Terkirim", "Berhasil", "Gagal",
    ])
    for row in queries.daily_recap(devices, ctx["day"]):
        data.append([row["day"].isoformat(), row["total"], row["people_in"], row["sent"], row["success"], row["failed"]])

    scope = ctx["selected_device"].id if ctx["selected_device"] else "semua-device"
    name = f"rekap-harian-{scope}-{ctx['day']:%Y%m%d}"
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
