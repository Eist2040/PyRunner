# Bump default q_workers 2 -> 4. Only affects fresh installs (new
# global_settings rows) — existing rows are unaffected by a default change.
# Use `python manage.py scale_workers N` to change a live value.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0024_large_script_support"),
    ]

    operations = [
        migrations.AlterField(
            model_name="globalsettings",
            name="q_workers",
            field=models.PositiveIntegerField(
                default=4,
                help_text="Number of worker processes for task queue",
            ),
        ),
    ]
