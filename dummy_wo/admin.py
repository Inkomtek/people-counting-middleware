from django.contrib import admin

from .models import WorkOrder


@admin.register(WorkOrder)
class WorkOrderAdmin(admin.ModelAdmin):
    list_display = ("id", "wo_id", "received_at", "accepted", "response_status", "message")
    list_filter = ("accepted",)
    readonly_fields = ("received_at", "payload", "accepted", "response_status", "message")
    ordering = ("-received_at",)
