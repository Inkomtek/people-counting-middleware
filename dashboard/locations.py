"""Resolve the dashboard's Client -> Region -> Site -> Area -> Scope filters.

Every level can be "all" (the default), which stops the selection there: the dashboard then sums
every device under the deepest chosen level. Parameters that don't fit the levels above them (e.g.
a site left over after switching client) are ignored, and a lower level given alone (e.g. only
?scope=) fills in its parents. Devices without a Scope are the "unassigned" group (client=none);
"all" includes them.
"""

from urllib.parse import urlencode

from core.models import Area, Client, DeviceList, Region, Scope, Site

ALL = "all"
UNASSIGNED = "none"
LEVELS = ("client", "region", "site", "area", "scope")
MODELS = {"client": Client, "region": Region, "site": Site, "area": Area, "scope": Scope}


def _lookup(level, raw):
    if not raw or not raw.isdigit():
        return None
    return MODELS[level].objects.filter(pk=raw).first()


def _chain(obj, level):
    """The full selection implied by one object: itself plus every level above it."""
    chain = dict.fromkeys(LEVELS)
    if level == "scope":
        chain["scope"], obj, level = obj, obj.area, "area"
    if level == "area":
        chain["area"], obj, level = obj, obj.site, "site"
    if level == "site":
        chain.update(site=obj, region=obj.region, client=obj.client)
    return chain


def _select(params):
    """Deepest level whose object agrees with every level given above it. A level set to "all"
    ignores every level below it, so picking "Semua" higher up always wins over stale lower values."""
    given = {}
    for level in LEVELS:
        if params.get(level) == ALL:
            given.update(dict.fromkeys(LEVELS[LEVELS.index(level):]))
            break
        given[level] = _lookup(level, params.get(level))
    for level in ("scope", "area", "site"):
        if given[level]:
            chain = _chain(given[level], level)
            if all(given[upper] in (None, chain[upper]) for upper in LEVELS[:LEVELS.index(level)]):
                return chain
    client, region = given["client"], given["region"]
    if client and region and Site.objects.filter(client=client, region=region).exists():
        return {**dict.fromkeys(LEVELS), "client": client, "region": region}
    if client:
        return {**dict.fromkeys(LEVELS), "client": client}
    return dict.fromkeys(LEVELS)


def resolve(params):
    """The selected location, the dropdown choices per level, the device filter and the query string
    that keeps the location across links. None when no device exists at all."""
    if not DeviceList.objects.exists():
        return None
    has_unassigned = DeviceList.objects.filter(scope__isnull=True).exists()
    unassigned = params.get("client") == UNASSIGNED and has_unassigned
    selected = dict.fromkeys(LEVELS) if unassigned else _select(params)
    client, region, site, area, scope = (selected[level] for level in LEVELS)

    # Only choices that lead down to at least one Scope; a level is listed once its parent is chosen.
    choices = {level: [] for level in LEVELS}
    choices["client"] = list(Client.objects.filter(sites__areas__scopes__isnull=False).distinct())
    if client:
        choices["region"] = list(Region.objects.filter(sites__client=client, sites__areas__scopes__isnull=False).distinct())
    if region:
        choices["site"] = list(Site.objects.filter(client=client, region=region, areas__scopes__isnull=False).distinct())
    if site:
        choices["area"] = list(Area.objects.filter(site=site, scopes__isnull=False).distinct())
    if area:
        choices["scope"] = list(Scope.objects.filter(area=area))

    if unassigned:
        device_filter, query = {"scope__isnull": True}, urlencode({"client": UNASSIGNED})
    else:
        if scope:
            device_filter = {"scope": scope}
        elif area:
            device_filter = {"scope__area": area}
        elif site:
            device_filter = {"scope__area__site": site}
        elif region:
            device_filter = {"scope__area__site__client": client, "scope__area__site__region": region}
        elif client:
            device_filter = {"scope__area__site__client": client}
        else:
            device_filter = {}
        query = urlencode({level: obj.pk for level, obj in selected.items() if obj})

    # Header: the deepest chosen level names the page ("Area · Scope" for a toilet); the levels
    # above it become chips. None means "all locations" (the template supplies the wording).
    if scope:
        title, chips = f"{area.name} · {scope.name}", [client, region, site]
    elif area:
        title, chips = area.name, [client, region, site]
    elif site:
        title, chips = site.name, [client, region]
    elif region:
        title, chips = region.name, [client]
    elif client:
        title, chips = client.name, []
    else:
        title, chips = None, []
    return {
        "unassigned": unassigned,
        "has_unassigned": has_unassigned,
        "selected": selected,
        "choices": choices,
        "device_filter": device_filter,
        "query": query,
        "title": title,
        "chips": [chip.name for chip in chips],
    }
