# GOAT STRATEGY: decouple worker count (throughput) from RAM (per-job cap).
# - Script.memory_limit_mb: hard RLIMIT_AS cap per script subprocess.
# - GlobalSettings.heavy_job_*: concurrency gate so N workers doesn't mean
#   N heavy jobs running simultaneously.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0025_q_workers_default"),
    ]

    operations = [
        migrations.AddField(
            model_name="script",
            name="memory_limit_mb",
            field=models.PositiveIntegerField(
                default=512,
                help_text=(
                    "Hard RAM cap for this script's process, in MB (0 = unlimited). "
                    "Exceeding it kills the process immediately (MemoryError / OOM), "
                    "independent of how many workers are running."
                ),
            ),
        ),
        migrations.AddField(
            model_name="globalsettings",
            name="heavy_job_threshold_mb",
            field=models.PositiveIntegerField(
                default=512,
                help_text="Scripts with memory_limit_mb at/above this are 'heavy' and gated by heavy_job_slots",
            ),
        ),
        migrations.AddField(
            model_name="globalsettings",
            name="heavy_job_slots",
            field=models.PositiveIntegerField(
                default=1,
                help_text="Max number of 'heavy' scripts allowed to run concurrently, regardless of worker count",
            ),
        ),
        migrations.AddField(
            model_name="globalsettings",
            name="heavy_job_running_count",
            field=models.PositiveIntegerField(
                default=0,
                help_text="Internal counter of currently-running heavy jobs (managed by executor.py)",
            ),
        ),
    ]
