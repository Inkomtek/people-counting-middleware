from django.db import migrations

DEVICE_ID = "2069691213314072577"


def set_location(apps, schema_editor):
    apps.get_model("core", "DeviceList").objects.filter(id=DEVICE_ID, building="").update(
        building="GRAHA ISS BINTARO", floor="2", gender="male"
    )


class Migration(migrations.Migration):
    dependencies = [("core", "0006_devicelist_location")]

    operations = [migrations.RunPython(set_location, migrations.RunPython.noop)]
