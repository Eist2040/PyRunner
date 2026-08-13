from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0022_add_one_minute_interval"),
    ]

    operations = [
        migrations.AddField(
            model_name="run",
            name="pid",
            field=models.IntegerField(
                null=True,
                blank=True,
                help_text="OS process ID of the running subprocess",
            ),
        ),
    ]
