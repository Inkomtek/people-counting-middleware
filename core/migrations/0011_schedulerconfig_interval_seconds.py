import django.core.validators
from django.db import migrations, models


def minutes_to_seconds(apps, schema_editor):
    SchedulerConfig = apps.get_model("core", "SchedulerConfig")
    for config in SchedulerConfig.objects.all():
        config.interval_seconds = max(10, config.interval_minutes * 60)
        config.save(update_fields=["interval_seconds"])


def seconds_to_minutes(apps, schema_editor):
    SchedulerConfig = apps.get_model("core", "SchedulerConfig")
    for config in SchedulerConfig.objects.all():
        config.interval_minutes = max(1, round(config.interval_seconds / 60))
        config.save(update_fields=["interval_minutes"])


class Migration(migrations.Migration):
    dependencies = [("core", "0010_schedulerconfig_dashboard_refresh")]

    operations = [
        migrations.AddField(
            model_name="schedulerconfig",
            name="interval_seconds",
            field=models.PositiveIntegerField(
                default=60,
                help_text="How often the scheduler pulls new events from ZK (10-86400 seconds).",
                validators=[
                    django.core.validators.MinValueValidator(10),
                    django.core.validators.MaxValueValidator(86400),
                ],
            ),
        ),
        migrations.RunPython(minutes_to_seconds, seconds_to_minutes),
        migrations.RemoveField(model_name="schedulerconfig", name="interval_minutes"),
    ]
