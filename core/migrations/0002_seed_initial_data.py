import os

from django.db import migrations

ENDPOINTS = [
    {
        "id": "zk-token",
        "type": "token",
        "url": "https://gateway.zkdigimax.com/global/open/api/v1/oauth2/token",
        "head": {},
        # Placeholders are filled from ZK_CLIENT_ID / ZK_CLIENT_SECRET in .env at request time.
        "body": {
            "client_id": "{{client_id}}",
            "client_secret": "{{client_secret}}",
            "grant_type": "client_credentials",
        },
    },
    {
        "id": "zk-event",
        "type": "event",
        "url": "https://gateway.zkdigimax.com/zt/open/api/v1/event/page",
        "head": {},
        "body": {"pageSize": 10},
    },
    {
        # Points at the local dummy API with a dummy token. Switch url/body to Algospection once access is granted.
        "id": "work-order",
        "type": "notification",
        # Docker sets DUMMY_WO_URL to the web container address (http://web.internal:8000/...).
        "url": os.getenv("DUMMY_WO_URL", "http://127.0.0.1:8000/dummy/api_iot.php"),
        "head": {},
        "body": {
            "token": "dummy_local_test_token",
            "INSTANCE": "prod",
            "LOC_ID": "GRAHA ISS BINTARO",
            "ASSET_ID": "SPACE-GRAHAISS-0020",
            "REQ_TYP": "CHECK-ROOM-TOILET",
            "REQ_DESC": "Toilet Traffic Counter",
        },
    },
]


def seed(apps, schema_editor):
    Endpoint = apps.get_model("core", "Endpoint")
    DeviceList = apps.get_model("core", "DeviceList")
    SchedulerConfig = apps.get_model("core", "SchedulerConfig")
    for data in ENDPOINTS:
        Endpoint.objects.get_or_create(id=data["id"], defaults=data)
    DeviceList.objects.get_or_create(id="2069691213314072577", defaults={"type": "ZK People Counter"})
    SchedulerConfig.objects.get_or_create(pk=1)


def unseed(apps, schema_editor):
    apps.get_model("core", "Endpoint").objects.filter(id__in=[e["id"] for e in ENDPOINTS]).delete()
    apps.get_model("core", "DeviceList").objects.filter(id="2069691213314072577").delete()
    apps.get_model("core", "SchedulerConfig").objects.filter(pk=1).delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]

    operations = [migrations.RunPython(seed, unseed)]
