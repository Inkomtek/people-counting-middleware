"""Create location hierarchy rows (Client / Region / Site / Area / Scope) and attach devices.

Shared by the add_location and seed_locations management commands. Everything is get-or-create,
so running a command again never duplicates rows.
"""

from .models import Area, Client, DeviceList, Region, Scope, Site

LEVELS = ("client", "region", "site", "area", "scope")


def ensure_location(client, region, site, area, scope):
    """Get or create each level; returns the Scope and {level: created?}."""
    names = {"client": client, "region": region, "site": site, "area": area, "scope": scope}
    names = {level: (value or "").strip() for level, value in names.items()}
    empty = [level for level in LEVELS if not names[level]]
    if empty:
        raise ValueError(f"Empty value for: {', '.join(empty)}")
    client_obj, client_new = Client.objects.get_or_create(name=names["client"])
    region_obj, region_new = Region.objects.get_or_create(name=names["region"])
    site_obj, site_new = Site.objects.get_or_create(client=client_obj, region=region_obj, name=names["site"])
    area_obj, area_new = Area.objects.get_or_create(site=site_obj, name=names["area"])
    scope_obj, scope_new = Scope.objects.get_or_create(area=area_obj, name=names["scope"])
    created = {"client": client_new, "region": region_new, "site": site_new, "area": area_new, "scope": scope_new}
    return scope_obj, created


def assign_devices(scope, device_ids):
    """Point the given devices at `scope`. Returns ([(device, previous scope)], [missing ids])."""
    devices = {device.device_id: device
               for device in DeviceList.objects.filter(device_id__in=device_ids).select_related("scope")}
    assigned = []
    for device_id in device_ids:
        device = devices.get(device_id)
        if device is None:
            continue
        previous = device.scope
        device.scope = scope
        device.save(update_fields=["scope"])
        assigned.append((device, previous))
    missing = [device_id for device_id in device_ids if device_id not in devices]
    return assigned, missing
