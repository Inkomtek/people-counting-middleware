from django.contrib import admin, messages

from .models import ApiClient, CustomerResponse, SensorReading, StatusRule


@admin.register(ApiClient)
class ApiClientAdmin(admin.ModelAdmin):
    list_display = ("name", "key_prefix", "is_active", "created_at", "last_used_at")
    list_editable = ("is_active",)
    readonly_fields = ("key_prefix", "created_at", "last_used_at")
    actions = ["regenerate_key"]

    def save_model(self, request, obj, form, change):
        raw_key = None if change else obj.set_new_key()
        super().save_model(request, obj, form, change)
        if raw_key:
            self._show_key(request, obj, raw_key)

    @admin.action(description="Regenerate API key (old key stops working)")
    def regenerate_key(self, request, queryset):
        for client in queryset:
            raw_key = client.set_new_key()
            client.save(update_fields=["key_hash", "key_prefix"])
            self._show_key(request, client, raw_key)

    def _show_key(self, request, client, raw_key):
        self.message_user(
            request,
            f"API key for {client.name}: {raw_key} — copy it now, it will not be shown again.",
            messages.WARNING,
        )


@admin.register(StatusRule)
class StatusRuleAdmin(admin.ModelAdmin):
    list_display = ("sensor_type", "condition", "severity", "min_level", "max_level")
    list_editable = ("condition", "severity", "min_level", "max_level")
    list_filter = ("sensor_type", "severity")


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(SensorReading)
class SensorReadingAdmin(ReadOnlyAdmin):
    list_display = ("time", "device", "battery", "level", "condition", "client")
    list_filter = ("device__type", "condition", "device__building", "device__floor", "device__gender", "device")
    date_hierarchy = "time"
    ordering = ("-time",)


@admin.register(CustomerResponse)
class CustomerResponseAdmin(ReadOnlyAdmin):
    list_display = ("time", "device", "rating", "comment", "client")
    list_filter = ("rating", "device__building", "device__floor", "device__gender", "device")
    date_hierarchy = "time"
    ordering = ("-time",)
