# Generated to add 1-minute interval scheduling option

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0021_field_updates"),
    ]

    operations = [
        migrations.AlterField(
            model_name="scriptschedule",
            name="interval_minutes",
            field=models.PositiveIntegerField(
                blank=True,
                choices=[
                    (1, "Every 1 minute"),
                    (5, "Every 5 minutes"),
                    (10, "Every 10 minutes"),
                    (15, "Every 15 minutes"),
                    (30, "Every 30 minutes"),
                    (60, "Every hour"),
                    (120, "Every 2 hours"),
                    (360, "Every 6 hours"),
                    (720, "Every 12 hours"),
                ],
                help_text="Interval in minutes (for interval mode)",
                null=True,
            ),
        ),
    ]
