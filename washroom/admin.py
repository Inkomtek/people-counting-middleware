from django.contrib import admin, messages

from .models import ApiClient, CustomerResponse, SensorDevice, SensorReading, StatusRule, Washroom, WashroomConfig


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


@admin.register(WashroomConfig)
class WashroomConfigAdmin(admin.ModelAdmin):
    list_display = ("__str__", "offline_after_minutes")

    def has_add_permission(self, request):
        return not WashroomConfig.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(StatusRule)
class StatusRuleAdmin(admin.ModelAdmin):
    list_display = ("sensor_type", "condition", "severity", "min_level", "max_level")
    list_editable = ("condition", "severity", "min_level", "max_level")
    list_filter = ("sensor_type", "severity")


@admin.register(Washroom)
class WashroomAdmin(admin.ModelAdmin):
    list_display = ("building", "floor", "gender", "device_count")
    list_filter = ("building", "floor", "gender")
    filter_horizontal = ("people_counters",)

    @admin.display(description="sensor devices")
    def device_count(self, obj):
        return obj.devices.count()


@admin.register(SensorDevice)
class SensorDeviceAdmin(admin.ModelAdmin):
    list_display = ("id", "type", "washroom", "name", "location", "last_seen", "last_battery", "last_level", "last_condition")
    list_editable = ("washroom",)
    list_filter = ("type", "washroom")
    search_fields = ("id", "name", "location")
    readonly_fields = (
        "last_seen", "last_battery", "last_level", "last_condition", "last_severity", "created_at",
    )


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(SensorReading)
class SensorReadingAdmin(ReadOnlyAdmin):
    list_display = ("time", "device", "battery", "level", "condition", "client")
    list_filter = ("device__type", "condition", "device")
    date_hierarchy = "time"
    ordering = ("-time",)


@admin.register(CustomerResponse)
class CustomerResponseAdmin(ReadOnlyAdmin):
    list_display = ("time", "device_id", "location", "rating", "comment", "client")
    list_filter = ("rating", "device_id")
    date_hierarchy = "time"
    ordering = ("-time",)
