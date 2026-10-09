"""Move the hard-coded Work Order flow into Automation.

Every notification Endpoint becomes a Subchannel of an "Algospection" channel (same URL, head, body and
active flag), and the rule "Pengunjung mencapai batas" sends people counters that reach their threshold
there, plus to the cleaners of the toilet (once staff are added). The Endpoints stay for history.
"""

from django.db import migrations


def forward(apps, schema_editor):
    Endpoint = apps.get_model("core", "Endpoint")
    Channel = apps.get_model("automation", "Channel")
    Subchannel = apps.get_model("automation", "Subchannel")
    Rule = apps.get_model("automation", "Rule")

    endpoints = list(Endpoint.objects.filter(type="notification").order_by("-is_active", "id"))
    default = endpoints[0] if endpoints else None
    channel, _ = Channel.objects.get_or_create(
        name="Algospection",
        defaults={"type": "algospection", "is_active": True, "retry_count": 0,
                  "head": default.head if default else {}, "body": default.body if default else {}},
    )
    subchannels = []
    for endpoint in endpoints:
        sub, _ = Subchannel.objects.get_or_create(
            channel=channel, name=endpoint.id,
            defaults={"target": endpoint.url, "is_active": endpoint.is_active,
                      "head": endpoint.head, "body": endpoint.body},
        )
        subchannels.append(sub)
    rule, created = Rule.objects.get_or_create(
        name="Pengunjung mencapai batas",
        defaults={"condition": "people_threshold", "is_active": True, "device_types": ["people"],
                  "notify_on_duty": True, "cooldown_minutes": 0},
    )
    if created:
        rule.subchannels.set(subchannels)


def backward(apps, schema_editor):
    apps.get_model("automation", "Rule").objects.filter(name="Pengunjung mencapai batas").delete()
    apps.get_model("automation", "Channel").objects.filter(name="Algospection").delete()


class Migration(migrations.Migration):
    dependencies = [
        ("automation", "0001_initial"),
        ("core", "0016_devicelist_numeric_pk"),
    ]

    operations = [migrations.RunPython(forward, backward)]
