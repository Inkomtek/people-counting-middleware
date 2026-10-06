import os

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static

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
