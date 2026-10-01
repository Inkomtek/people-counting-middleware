from django.contrib import admin

from .models import EventLog


@admin.register(EventLog)
class EventLogAdmin(admin.ModelAdmin):
    list_display = ("id", "event_type", "description", "logged_at")
    list_filter = ("event_type", "logged_at")
    search_fields = ("event_type", "description")
    ordering = ("-logged_at",)
