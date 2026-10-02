from django.db import migrations

# The existing ZK people counter is at Graha ISS Bintaro, Lantai 2, Pria (same location as the Work Order body).
BUILDING, FLOOR, GENDER = "GRAHA ISS BINTARO", "Lantai 2", "pria"
ZK_DEVICE_ID = "2069691213314072577"


def seed(apps, schema_editor):
    Washroom = apps.get_model("washroom", "Washroom")
    DeviceList = apps.get_model("core", "DeviceList")
    washroom, _ = Washroom.objects.get_or_create(building=BUILDING, floor=FLOOR, gender=GENDER)
    counter = DeviceList.objects.filter(id=ZK_DEVICE_ID).first()
    if counter:
        washroom.people_counters.add(counter)


def unseed(apps, schema_editor):
    apps.get_model("washroom", "Washroom").objects.filter(building=BUILDING, floor=FLOOR, gender=GENDER).delete()


class Migration(migrations.Migration):
    dependencies = [("washroom", "0003_washroom"), ("core", "0005_devicelist_count_date")]

    operations = [migrations.RunPython(seed, unseed)]
