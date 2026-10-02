from django.db import migrations

# (sensor_type, condition, severity, min_level inclusive, max_level exclusive). Editable later in Admin.
DISPENSER_RULES = [
    ("Habis", "critical", None, 1),
    ("Hampir Habis", "warning", 1, 31),
    ("Terisi", "normal", 31, None),
]
RULES = [
    *[(sensor_type, *rule) for sensor_type in ("soap", "toilet-paper", "tissue") for rule in DISPENSER_RULES],
    ("trash", "Normal", "normal", None, 70),
    ("trash", "Hampir Penuh", "warning", 70, 90),
    ("trash", "Penuh", "critical", 90, None),
    # Ammonia in ppm: odor is noticeable around 5-10 ppm, 25 ppm is the common exposure limit.
    ("ammonia", "Normal", "normal", None, 10),
    ("ammonia", "Bau", "warning", 10, 25),
    ("ammonia", "Bahaya", "critical", 25, None),
]


def seed(apps, schema_editor):
    StatusRule = apps.get_model("washroom", "StatusRule")
    for sensor_type, condition, severity, min_level, max_level in RULES:
        StatusRule.objects.get_or_create(
            sensor_type=sensor_type, condition=condition,
            defaults={"severity": severity, "min_level": min_level, "max_level": max_level},
        )


def unseed(apps, schema_editor):
    apps.get_model("washroom", "StatusRule").objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [("washroom", "0001_initial")]

    operations = [migrations.RunPython(seed, unseed)]
