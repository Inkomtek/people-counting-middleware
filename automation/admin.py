import json

from django.contrib import admin, messages
from django.utils.html import format_html
from import_export import fields, resources
from import_export.admin import ExportMixin

from core.resources import format_json, format_time

from . import channels, engine
from .models import AutomationLog, Channel, Recipient, Rule, Subchannel


def _pre(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    return format_html('<pre style="margin:0;white-space:pre-wrap;max-width:720px">{}</pre>', text)


def _preview(spec, channel_type, target):
    """Head/body (or email) filled with sample data; secrets shown as ••••."""
    context = {**channels.SAMPLE_CONTEXT, "target": target or channels.SAMPLE_CONTEXT["target"]}
    if channel_type == Channel.TYPE_EMAIL:
        return _pre(f"Subject: {channels.fill(spec['email_subject'], context, secrets=False)}\n\n"
                    f"{channels.fill(spec['email_body'], context, secrets=False)}")
    return _pre({"head": channels.fill(spec["head"] or {}, context, secrets=False),
                 "body": channels.fill(spec["body"] or {}, context, secrets=False)})


def _warn_unknown(request, *values):
    unknown = channels.unknown_placeholders(*values)
    if unknown:
        messages.warning(request, "Placeholder tidak dikenal (dibiarkan apa adanya saat kirim): "
                         + ", ".join("{{" + n + "}}" for n in unknown))


PLACEHOLDER_HELP = ("Placeholder: " + ", ".join("{{" + k + "}}" for k in channels.SAMPLE_CONTEXT)
                    + ", {{env:NAMA_VARIABEL}} (rahasia dari .env), {{algospection_token}}.")


class SubchannelInline(admin.TabularInline):
    model = Subchannel
    extra = 0
    fields = ("name", "target", "is_active", "mode")
    readonly_fields = ("mode",)
    show_change_link = True

    @admin.display(description="Head/body")
    def mode(self, obj):
        return "override" if obj.pk and obj.overrides else "mengikuti channel"


@admin.register(Channel)
class ChannelAdmin(admin.ModelAdmin):
    list_display = ("name", "type", "is_active", "retry_count", "retry_delay_minutes")
    list_filter = ("type", "is_active")
    list_editable = ("is_active",)
    inlines = [SubchannelInline]
    readonly_fields = ("preview",)
    fieldsets = (
        (None, {"fields": ("name", "type", "is_active", "config")}),
        ("Format default (dipakai subchannel yang tidak override)", {
            "fields": ("method", "head", "body", "email_subject", "email_body", "preview"),
            "description": PLACEHOLDER_HELP,
        }),
        ("Retry", {"fields": ("retry_count", "retry_delay_minutes")}),
    )

    @admin.display(description="Pratinjau (data contoh, rahasia disamarkan)")
    def preview(self, obj):
        if not obj.pk:
            return "Simpan dulu untuk melihat pratinjau (format default per tipe terisi otomatis)."
        spec = {"head": obj.head, "body": obj.body, "email_subject": obj.email_subject, "email_body": obj.email_body}
        return _preview(spec, obj.type, "")

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        _warn_unknown(request, obj.head, obj.body, obj.email_subject, obj.email_body)


@admin.register(Subchannel)
class SubchannelAdmin(admin.ModelAdmin):
    list_display = ("name", "channel", "target", "is_active", "mode")
    list_filter = ("channel", "is_active")
    search_fields = ("name", "target")
    readonly_fields = ("mode", "preview")
    actions = ["send_test"]
    fieldsets = (
        (None, {"fields": ("channel", "name", "target", "is_active")}),
        ("Override head/body (kosong = mengikuti channel)", {
            "fields": ("mode", "head", "body", "email_subject", "email_body", "preview"),
            "description": PLACEHOLDER_HELP,
        }),
    )

    @admin.display(description="Head/body")
    def mode(self, obj):
        return "override" if obj.pk and obj.overrides else "mengikuti channel"

    @admin.display(description="Pratinjau (data contoh, rahasia disamarkan)")
    def preview(self, obj):
        if not obj.pk:
            return "Simpan dulu untuk melihat pratinjau."
        return _preview(obj.effective(), obj.channel.type, obj.target)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        _warn_unknown(request, obj.head, obj.body, obj.email_subject, obj.email_body)

    @admin.action(description="Kirim pesan uji")
    def send_test(self, request, queryset):
        for sub in queryset.select_related("channel"):
            engine.test_send(sub)
        self.message_user(request, f"Pesan uji dikirim ke {queryset.count()} subchannel. Lihat hasilnya di Automation Log.")


@admin.register(Recipient)
class RecipientAdmin(admin.ModelAdmin):
    list_display = ("name", "is_active", "role", "whatsapp_number", "telegram_chat_id", "email")
    list_filter = ("is_active", "scopes__area", "areas")
    search_fields = ("name", "whatsapp_number", "telegram_chat_id", "email")
    filter_horizontal = ("scopes", "areas")
    actions = ["send_test"]
    fieldsets = (
        (None, {"fields": ("name", "is_active")}),
        ("Kontak", {"fields": ("whatsapp_number", "telegram_chat_id", "email", "contact_priority")}),
        ("Wilayah tugas", {
            "fields": ("scopes", "areas"),
            "description": "Ditugaskan ke Scope = cleaner. Ditugaskan ke Area = supervisor. Keduanya dikirimi bersamaan.",
        }),
    )

    @admin.display(description="Peran")
    def role(self, obj):
        roles = (["Cleaner"] if obj.scopes.exists() else []) + (["Supervisor"] if obj.areas.exists() else [])
        return " + ".join(roles) or "–"

    @admin.action(description="Kirim pesan uji (lewat semua channel aktif)")
    def send_test(self, request, queryset):
        active = list(Channel.objects.filter(is_active=True))
        for person in queryset:
            engine.test_send_person(active, person)
        self.message_user(request, "Pesan uji dikirim. Lihat hasilnya di Automation Log.")


@admin.register(Rule)
class RuleAdmin(admin.ModelAdmin):
    list_display = ("name", "condition", "is_active", "notify_on_duty", "device_count", "cooldown_minutes")
    list_filter = ("condition", "is_active", "notify_on_duty")
    list_editable = ("is_active",)
    search_fields = ("name",)
    filter_horizontal = ("devices", "areas", "scopes", "subchannels", "person_channels")
    readonly_fields = ("matching",)
    fieldsets = (
        (None, {"fields": ("name", "is_active", "condition")}),
        ("Parameter kondisi", {
            "fields": ("severities", "conditions", "battery_below", "offline_minutes"),
            "description": "Status sensor: severities dan/atau nama status. Baterai lemah: battery_below. "
                           "Offline: offline_minutes. Pengunjung capai batas memakai batas per device.",
        }),
        ("Device yang dipantau", {
            "fields": ("devices", "scopes", "areas", "device_types", "matching"),
            "description": "Device dipilih ATAU di bawah Scope/Area yang dipilih (kosong semua = semua device), "
                           "lalu disaring jenis sensor.",
        }),
        ("Tujuan", {
            "fields": ("subchannels", "notify_on_duty", "contact_mode", "person_channels"),
            "description": "Petugas yang bertugas: cleaner Scope dan supervisor Area device dikirimi bersamaan. "
                           "Kalau tidak ada keduanya, tidak dikirim (hanya dicatat di log).",
        }),
        ("Pesan", {"fields": ("message_template", "cooldown_minutes"), "description": PLACEHOLDER_HELP}),
    )

    @admin.display(description="Device")
    def device_count(self, obj):
        return obj.matching_devices().count()

    @admin.display(description="Device yang cocok")
    def matching(self, obj):
        if not obj.pk:
            return "Simpan dulu untuk melihat device yang cocok."
        devices = list(obj.matching_devices()[:30])
        total = obj.matching_devices().count()
        names = ", ".join(d.label for d in devices) + (" …" if total > len(devices) else "")
        return f"{total} device: {names}" if total else "Tidak ada device yang cocok."

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        _warn_unknown(request, form.instance.message_template)


class AutomationLogResource(resources.ModelResource):
    time = fields.Field(attribute="time", column_name="time")

    class Meta:
        model = AutomationLog
        fields = ("time", "rule__name", "device", "channel__name", "subchannel__name", "recipient__name",
                  "channel_type", "tier", "attempt", "status", "target", "message", "response_status",
                  "response", "note")
        export_order = fields

    def dehydrate_time(self, obj):
        return format_time(obj.time)

    def dehydrate_response(self, obj):
        return format_json(obj.response)


@admin.register(AutomationLog)
class AutomationLogAdmin(ExportMixin, admin.ModelAdmin):
    resource_classes = [AutomationLogResource]
    list_display = ("time", "status", "channel_type", "tier", "rule", "device", "destination", "attempt", "response_status")
    list_filter = ("status", "channel_type", "tier", "rule", "channel")
    search_fields = ("target", "message", "device__device_id", "recipient__name")
    date_hierarchy = "time"
    ordering = ("-time",)

    @admin.display(description="Tujuan")
    def destination(self, obj):
        who = obj.recipient.name if obj.recipient else (obj.subchannel.name if obj.subchannel else "")
        return f"{who} ({obj.target})" if obj.target else who

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
