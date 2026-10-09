from django.contrib import admin

from .models import InboxMessage, WorkOrder


@admin.register(WorkOrder)
class WorkOrderAdmin(admin.ModelAdmin):
    list_display = ("id", "wo_id", "received_at", "accepted", "response_status", "message")
    list_filter = ("accepted",)
    readonly_fields = ("received_at", "payload", "accepted", "response_status", "message")
    ordering = ("-received_at",)


@admin.register(InboxMessage)
class InboxMessageAdmin(admin.ModelAdmin):
    list_display = ("id", "received_at", "kind", "method", "response_status", "path")
    list_filter = ("kind", "response_status")
    readonly_fields = ("received_at", "kind", "path", "method", "headers", "body", "response_status")
    ordering = ("-received_at",)
