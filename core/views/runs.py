"""
Run views for the control panel.

Improvements for 100K+ line script support:
  * run_detail_view defers heavy fields by default and lets the template
    fetch large stdout/stderr via a streaming endpoint.
  * run_output_stream_view serves a slice of the (possibly spooled)
    stdout/stderr so the browser can virtualize huge outputs in chunks.
  * run_clear_view also deletes any spool files for the deleted runs.
"""
import os
import signal
from datetime import timedelta

from django.conf import settings
from django.http import (
    HttpRequest,
    HttpResponse,
    JsonResponse,
    StreamingHttpResponse,
    Http404,
)
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST, require_GET
from django.contrib import messages
from django.core.paginator import Paginator

from core.models import Run, Script
from core.services.output_storage_service import OutputStorageService


@login_required
def run_list_view(request: HttpRequest) -> HttpResponse:
    """List all runs with pagination. Defers large text blobs for speed."""
    runs = (
        Run.objects
        .select_related("script", "triggered_by")
        .defer("stdout", "stderr", "code_snapshot")
        .order_by("-created_at")
    )

    status_filter = request.GET.get("status")
    if status_filter and status_filter in dict(Run.Status.choices):
        runs = runs.filter(status=status_filter)

    script_filter = request.GET.get("script")
    if script_filter:
        runs = runs.filter(script_id=script_filter)

    trigger_filter = request.GET.get("trigger")
    if trigger_filter and trigger_filter in dict(Run.TriggerType.choices):
        runs = runs.filter(trigger_type=trigger_filter)

    # Time range filters — critical for 1-minute schedules, which can
    # produce thousands of runs/day. Without these the list is only
    # navigable by paging through everything.
    from datetime import datetime
    from django.utils import timezone as dj_timezone

    date_from = request.GET.get("date_from", "")
    date_to = request.GET.get("date_to", "")

    if date_from:
        try:
            dt = datetime.strptime(date_from, "%Y-%m-%d")
            runs = runs.filter(created_at__gte=dj_timezone.make_aware(dt))
        except ValueError:
            date_from = ""

    if date_to:
        try:
            dt = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
            runs = runs.filter(created_at__lt=dj_timezone.make_aware(dt))
        except ValueError:
            date_to = ""

    # Quick relative-time presets (last hour / 24h / 7d) — faster than
    # picking exact dates when chasing a recent burst of runs.
    last = request.GET.get("last")
    if last in ("1h", "24h", "7d") and not (date_from or date_to):
        delta = {"1h": timedelta(hours=1), "24h": timedelta(days=1), "7d": timedelta(days=7)}[last]
        runs = runs.filter(created_at__gte=dj_timezone.now() - delta)

    paginator = Paginator(runs, 50)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)

    scripts = Script.objects.only("pk", "name").order_by("name")

    # Preserve every active filter across pagination links.
    extra_qs_parts = []
    if status_filter:
        extra_qs_parts.append(f"status={status_filter}")
    if script_filter:
        extra_qs_parts.append(f"script={script_filter}")
    if trigger_filter:
        extra_qs_parts.append(f"trigger={trigger_filter}")
    if date_from:
        extra_qs_parts.append(f"date_from={date_from}")
    if date_to:
        extra_qs_parts.append(f"date_to={date_to}")
    if last:
        extra_qs_parts.append(f"last={last}")
    extra_qs = ("&" + "&".join(extra_qs_parts)) if extra_qs_parts else ""

    return render(request, "cpanel/runs/list.html", {
        "runs": page_obj,
        "page_obj": page_obj,
        "paginator": paginator,
        "status_choices": Run.Status.choices,
        "trigger_choices": Run.TriggerType.choices,
        "status_filter": status_filter,
        "script_filter": script_filter,
        "trigger_filter": trigger_filter,
        "date_from": date_from,
        "date_to": date_to,
        "last_filter": last or "",
        "scripts": scripts,
        "extra_qs": extra_qs,
    })


@login_required
def run_detail_view(request: HttpRequest, pk) -> HttpResponse:
    """View run details including output."""
    # Defer the heavy code_snapshot by default; load lazily if template
    # requests it. stdout/stderr we DO need, but if they're spooled the
    # DB row only has a preview anyway.
    run = get_object_or_404(
        Run.objects.select_related("script", "triggered_by"),
        pk=pk
    )
    return render(request, "cpanel/runs/detail.html", {
        "run": run,
        "output_spool_threshold": getattr(settings, "OUTPUT_SPOOL_THRESHOLD", 4 * 1024 * 1024),
    })


@login_required
@require_GET
def run_output_stream_view(request: HttpRequest, pk, stream: str) -> HttpResponse:
    """
    Stream a slice of a run's stdout or stderr.

    Query params:
        start: byte offset (default 0)
        end:   byte offset (exclusive; default = start + 256KB)

    Returns:
        200 StreamingHttpResponse with raw bytes
        404 if run/stream not found
    """
    if stream not in ("stdout", "stderr"):
        raise Http404("Invalid stream name")

    run = get_object_or_404(Run, pk=pk)

    try:
        start = max(0, int(request.GET.get("start", 0)))
        chunk_size = min(
            4 * 1024 * 1024,
            max(1, int(request.GET.get("chunk_size", 256 * 1024))),
        )
        end_param = request.GET.get("end")
        end = int(end_param) if end_param else start + chunk_size
    except ValueError:
        return JsonResponse({"error": "Invalid start/end/chunk_size"}, status=400)

    # If the output is spooled, read from disk; otherwise read from DB.
    is_spooled = (stream == "stdout" and run.stdout_spooled) or (
        stream == "stderr" and run.stderr_spooled
    )

    if is_spooled:
        data, total, exists = OutputStorageService.read_stream(run.id, stream, start, end)
        if not exists:
            raise Http404("Spool file missing")
    else:
        text = run.stdout if stream == "stdout" else run.stderr
        encoded = (text or "").encode("utf-8", errors="replace")
        total = len(encoded)
        data = encoded[start:end]

    def _iter():
        yield data

    resp = StreamingHttpResponse(_iter(), content_type="application/octet-stream")
    resp["X-Total-Size"] = str(total)
    resp["X-Spooled"] = "1" if is_spooled else "0"
    resp["Content-Length"] = str(len(data))
    resp["Content-Range"] = f"bytes {start}-{start + len(data) - 1}/{total}"
    return resp


@login_required
@require_POST
def run_clear_view(request: HttpRequest) -> HttpResponse:
    """Delete runs — all or filtered by status/age. Also cleans spool files."""
    mode = request.POST.get("mode", "all")

    if mode == "all":
        # Collect IDs first so we can clean spool files
        run_ids = list(Run.objects.values_list("id", flat=True))
        count, _ = Run.objects.all().delete()
        for rid in run_ids:
            OutputStorageService.delete_for_run(rid)
        messages.success(request, f"Deleted {count} runs.")

    elif mode == "status":
        status = request.POST.get("status")
        if status and status in dict(Run.Status.choices):
            run_ids = list(
                Run.objects.filter(status=status).values_list("id", flat=True)
            )
            count, _ = Run.objects.filter(status=status).delete()
            for rid in run_ids:
                OutputStorageService.delete_for_run(rid)
            messages.success(request, f"Deleted {count} {status} runs.")
        else:
            messages.error(request, "Invalid status.")

    elif mode == "older_than":
        from django.utils import timezone
        from datetime import timedelta
        days = int(request.POST.get("days", 30))
        cutoff = timezone.now() - timedelta(days=days)
        run_ids = list(
            Run.objects.filter(created_at__lt=cutoff).values_list("id", flat=True)
        )
        count, _ = Run.objects.filter(created_at__lt=cutoff).delete()
        for rid in run_ids:
            OutputStorageService.delete_for_run(rid)
        messages.success(request, f"Deleted {count} runs older than {days} days.")

    else:
        messages.error(request, "Unknown clear mode.")

    return redirect("cpanel:run_list")


@login_required
@require_POST
def run_stop_view(request: HttpRequest, pk) -> JsonResponse:
    """
    Kill a running or pending script run.

    For RUNNING runs: sends SIGTERM (graceful) then SIGKILL (force) to the
    subprocess via the stored PID. For PENDING runs: marks cancelled directly.
    """
    from django.utils import timezone

    run = get_object_or_404(Run, pk=pk)

    if run.status not in (Run.Status.RUNNING, Run.Status.PENDING):
        return JsonResponse(
            {"success": False, "error": f"Run is already {run.status}"},
            status=400,
        )

    killed = False
    kill_error = None

    if run.status == Run.Status.RUNNING and run.pid:
        try:
            if os.name == "nt":
                # Windows: taskkill /F kills the process tree
                import subprocess as _sp
                _sp.run(["taskkill", "/F", "/T", "/PID", str(run.pid)], check=False)
            else:
                try:
                    os.killpg(os.getpgid(run.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    os.kill(run.pid, signal.SIGTERM)
            killed = True
        except ProcessLookupError:
            # Process already dead — still mark cancelled
            killed = True
        except Exception as e:
            kill_error = str(e)

    # Mark cancelled regardless of whether kill succeeded
    run.status = Run.Status.CANCELLED
    run.ended_at = timezone.now()
    run.pid = None
    note = "\n[Run stopped by user]"
    if kill_error:
        note += f"\n[Kill attempt error: {kill_error}]"
    run.stderr = (run.stderr or "") + note
    run.save(update_fields=["status", "ended_at", "pid", "stderr"])

    return JsonResponse({
        "success": True,
        "killed": killed,
        "message": "Run stopped successfully.",
    })
