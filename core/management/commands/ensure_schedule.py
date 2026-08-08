"""
Management command to create/update a Script's recurring schedule.

ROOT CONTEXT: scripts like bulk_seo_articles.py have TWO run modes baked in —
"webhook mode" (one specific job, triggered by WP on submission) and "poll
mode" (no webhook args → fetches ALL pending jobs, drains up to
MAX_CONCURRENT_JOBS of them). Poll mode is what actually keeps the queue
moving once the first couple of webhook-dispatched jobs finish — but nothing
was ever configured to trigger it automatically. It only ran when a human
clicked "Run" by hand.

PyRunner already has a full scheduling system (ScriptSchedule + ScheduleService,
backed by django-q2) that supports exactly this — down to 1-minute intervals —
it just needed a schedule row created for this script. This command does that
idempotently, safe to re-run.
"""

from django.core.management.base import BaseCommand, CommandError

from core.models import Script, ScriptSchedule
from core.services.schedule_service import ScheduleService


class Command(BaseCommand):
    help = "Create or update an interval-based recurring schedule for a script."

    def add_arguments(self, parser):
        parser.add_argument(
            "script_name", type=str,
            help="Exact or partial Script.name to match (e.g. 'bulk_seo_articles').",
        )
        parser.add_argument(
            "--interval", type=int, default=1,
            choices=[c.value for c in ScriptSchedule.IntervalChoice],
            help="Minutes between runs (default: 1 — matches poll-mode's own "
                 "job-draining design; this only fires the platform-side "
                 "trigger, actual concurrency is capped by the script's own "
                 "MAX_CONCURRENT_JOBS and the django-q2 worker pool).",
        )
        parser.add_argument(
            "--disable", action="store_true",
            help="Set the schedule back to manual (disables auto-run) instead "
                 "of creating/updating an interval schedule.",
        )

    def handle(self, *args, **options):
        name = options["script_name"]
        scripts = Script.objects.filter(name__icontains=name)
        count = scripts.count()

        if count == 0:
            raise CommandError(f"No script found matching '{name}'")
        if count > 1:
            names = ", ".join(s.name for s in scripts)
            raise CommandError(
                f"'{name}' matches {count} scripts ({names}) — be more specific."
            )

        script = scripts.first()
        schedule, created = ScriptSchedule.objects.get_or_create(script=script)

        if options["disable"]:
            schedule.run_mode = ScriptSchedule.RunMode.MANUAL
            schedule.is_active = True
            schedule.save(update_fields=["run_mode", "is_active"])
            ScheduleService.sync_schedule(schedule)
            self.stdout.write(self.style.SUCCESS(
                f"'{script.name}' schedule set to MANUAL (auto-run disabled)."
            ))
            return

        schedule.run_mode = ScriptSchedule.RunMode.INTERVAL
        schedule.interval_minutes = options["interval"]
        schedule.is_active = True
        schedule.save(update_fields=["run_mode", "interval_minutes", "is_active"])

        q_ids = ScheduleService.sync_schedule(schedule)

        verb = "Created" if created else "Updated"
        self.stdout.write(self.style.SUCCESS(
            f"{verb} schedule for '{script.name}': every {options['interval']} "
            f"minute(s), django-q2 schedule id(s) {q_ids}. Next run: {schedule.next_run}."
        ))
        self.stdout.write(
            "Note: this only controls how often the platform TRIGGERS a run. "
            "Actual concurrent job count per run is MAX_CONCURRENT_JOBS inside "
            "the script, and total concurrent runs platform-wide is capped by "
            "q_workers (see `manage.py scale_workers`)."
        )
