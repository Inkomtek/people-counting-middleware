import os

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static
from django.utils import translation

register = template.Library()


@register.simple_tag(takes_context=True)
def tr(context, key, **values):
    """Text for `key` in the page's language with {placeholders} filled, e.g. {% tr "showing" start=1 end=20 total=255 %}."""
    text = context["t"].get(key, key)
    return text.format(**values) if values else text


@register.simple_tag
def static_v(path):
    """Static URL with the file's modification time appended, so browsers fetch a changed CSS/JS file
    instead of a cached old copy (e.g. dashboard.js after an update)."""
    url = static(path)
    found = finders.find(path)
    return f"{url}?v={int(os.path.getmtime(found))}" if found else url


@register.filter
def duration(minutes):
    """Minutes as "45m" or "1j 42m" ("1h 42m" in English; follows the page language set by @localized)."""
    # A missing value (None, or "" when the template looked up e.g. `fastest.gap` on None) shows a dash.
    if minutes is None or minutes == "":
        return "–"
    hours, rest = divmod(int(minutes), 60)
    if not hours:
        return f"{rest}m"
    unit = "h" if (translation.get_language() or "").startswith("en") else "j"
    return f"{hours}{unit} {rest}m" if rest else f"{hours}{unit}"


@register.filter
def get(mapping, key):
    """mapping[key] for keys a template can't spell (e.g. "toilet-paper"), None when missing."""
    return mapping.get(key) if mapping else None
