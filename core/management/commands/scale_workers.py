"""
Management command to change the django-q2 worker pool size.

ROOT CONTEXT: Q_CLUSTER["workers"] (core/models/settings.py: GlobalSettings.q_workers)
is a PLATFORM-WIDE cap on how many script executions can run simultaneously
across the ENTIRE PyRunner instance — every scheduled run, every webhook
dispatch, every manual run, for every script, shares this one pool. It
defaults to 2. This is a second, independent concurrency layer sitting
*above* any individual script's own internal concurrency (e.g. a script's
own ThreadPoolExecutor / MAX_CONCURRENT_JOBS) — raising the script's own
internal limit does nothing if the platform only ever hands it 1-2 worker
slots to begin with.

The value lives in the DB (global_settings.q_workers) but Q_CLUSTER is built
once at Django process start (pyrunner/settings.py:_get_q_cluster_config()),
so a DB change alone does NOT take effect until workers restart. This
command updates the DB value AND triggers the existing restart_workers
command so the change actually applies immediately.
"""

from django.core.management.base import BaseCommand
from django.core.management import call_command
from django.utils import timezone

from core.models import GlobalSettings


class Command(BaseCommand):
    help = (
        "Set the django-q2 worker pool size (platform-wide concurrent script "
        "execution cap) and restart workers so it takes effect."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "workers", type=int,
            help="New worker count. Rule of thumb: 1 worker per ~1.5 vCPU, "
                 "reserving headroom for the OS/DB/webserver. On a 6 vCPU box "
                 "already running WordPress+MariaDB+Traefik alongside PyRunner, "
                 "4 is a reasonable ceiling — go higher only after watching "
                 "actual CPU/RAM headroom under load.",
        )
        parser.add_argument(
            "--no-restart", action="store_true",
            help="Update the DB value only; skip the automatic worker restart "
                 "(the change won't take effect until you restart manually).",
        )

    def handle(self, *args, **options):
        workers = options["workers"]
        if workers < 1:
            self.stdout.write(self.style.ERROR("Worker count must be >= 1"))
            return

        settings = GlobalSettings.get_settings()
        old = settings.q_workers
        settings.q_workers = workers
        settings.worker_settings_updated_at = timezone.now()
        settings.save(update_fields=["q_workers", "worker_settings_updated_at"])

        self.stdout.write(self.style.SUCCESS(f"q_workers: {old} → {workers}"))

        if options["no_restart"]:
            self.stdout.write(self.style.WARNING(
                "Skipped restart — change is saved but NOT active yet. "
                "Run `python manage.py restart_workers` to apply it."
            ))
            return

        self.stdout.write("Restarting workers to apply new pool size...")
        try:
            call_command("restart_workers")
        except Exception as exc:
            self.stdout.write(self.style.WARNING(
                f"Auto-restart failed ({exc}) — DB value is saved, but you "
                "must restart workers manually for it to take effect."
            ))
