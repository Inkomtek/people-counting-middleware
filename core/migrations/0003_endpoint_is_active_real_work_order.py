from django.db import migrations, models

REAL_WORK_ORDER = {
    "id": "work-order-real",
    "type": "notification",
    "url": "https://issid-inspection.com/api_iot.php",
    "head": {},
    # {{algospection_token}} is filled from ALGOSPECTION_TOKEN in .env at request time.
    "body": {
        "token": "{{algospection_token}}",
        "INSTANCE": "prod",
        "LOC_ID": "GRAHA ISS BINTARO",
        "ASSET_ID": "SPACE-GRAHAISS-0020",
        "REQ_TYP": "CHECK-ROOM-TOILET",
        "REQ_DESC": "Toilet Traffic Counter",
    },
    # Inactive until access to Algospection is granted, so nothing is POSTed there by accident.
    "is_active": False,
}


def seed(apps, schema_editor):
    apps.get_model("core", "Endpoint").objects.get_or_create(id=REAL_WORK_ORDER["id"], defaults=REAL_WORK_ORDER)


def unseed(apps, schema_editor):
    apps.get_model("core", "Endpoint").objects.filter(id=REAL_WORK_ORDER["id"]).delete()


class Migration(migrations.Migration):
    dependencies = [("core", "0002_seed_initial_data")]

    operations = [
        migrations.AddField(
            model_name="endpoint",
            name="is_active",
            field=models.BooleanField(default=True),
        ),
        migrations.RunPython(seed, unseed),
    ]
