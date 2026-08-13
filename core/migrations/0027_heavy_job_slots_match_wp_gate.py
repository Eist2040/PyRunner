# ROOT FIX: heavy_job_slots was defaulting to 1 while the WP-side dispatch
# gate (ELITEX_DL_MAX_CONCURRENT_PER_TOOL in elitex-download-manager.php)
# already assumes 3 concurrent heavy jobs per tool. That mismatch let the
# 2nd/3rd job that passed the WP gate sit in _acquire_heavy_slot() polling
# for up to the script's full timeout without ever claiming its WP job row,
# leaving the WP job invisibly stuck 'pending' with zero feedback.
#
# This migration both (a) changes the field default for any future/new
# GlobalSettings row, and (b) backfills the existing singleton row (pk=1)
# if it's still sitting at the old default of 1 — it does NOT stomp a value
# an operator already changed intentionally.

from django.db import migrations, models


def bump_existing_singleton(apps, schema_editor):
    GlobalSettings = apps.get_model("core", "GlobalSettings")
    GlobalSettings.objects.filter(pk=1, heavy_job_slots=1).update(heavy_job_slots=3)


def revert_existing_singleton(apps, schema_editor):
    # Best-effort revert only; won't distinguish "we set it" from "operator
    # separately set it to 3" — intentionally a no-op to avoid clobbering.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0026_memory_limits"),
    ]

    operations = [
        migrations.AlterField(
            model_name="globalsettings",
            name="heavy_job_slots",
            field=models.PositiveIntegerField(
                default=3,
                help_text="Max number of 'heavy' scripts allowed to run concurrently, regardless of worker count",
            ),
        ),
        migrations.RunPython(bump_existing_singleton, revert_existing_singleton),
    ]
