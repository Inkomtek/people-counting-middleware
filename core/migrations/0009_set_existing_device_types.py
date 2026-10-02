from django.db import migrations


def set_types(apps, schema_editor):
    # Before 0008 `type` was free text ("ZK People Counter"); every existing device is a people counter.
    apps.get_model("core", "DeviceList").objects.all().update(type="people")


class Migration(migrations.Migration):
    dependencies = [("core", "0008_devicelist_type_name")]

    operations = [migrations.RunPython(set_types, migrations.RunPython.noop)]
