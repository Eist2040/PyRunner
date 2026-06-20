"""
Script views for the control panel.

Improvements for 100K+ line script support:
  * script_list_view annotates run_count + success_rate in ONE query
    (no per-row N+1 queries); defers the heavy `code` field from list rows;
    adds server-side pagination so listing 10,000 scripts stays fast.
  * script_run_view stores only a code_snapshot when the script's
    code_sha256 differs from the most recent run's; otherwise just the
    hash is recorded (huge DB savings for repeated runs of big scripts).
  * script_create_view / script_edit_view unchanged structurally — the
    `DATA_UPLOAD_MAX_MEMORY_SIZE` setting in pyrunner/settings.py is what
    unlocks accepting 50MB+ POST bodies.
"""
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import models
from django.views.decorators.http import require_POST
from django.http import HttpRequest, HttpResponse, JsonResponse, HttpResponseBadRequest

from core.models import Script, Run, ScriptSchedule, ScheduleHistory, Tag
from core.forms import ScriptForm, ScheduleForm
from core.tasks import queue_script_run
from core.services.schedule_service import ScheduleService


@login_required
def script_list_view(request: HttpRequest) -> HttpResponse:
    """List all scripts with optional filtering and pagination."""
    # Defer the heavy `code` field — list view never needs it.
    # Annotate run_count and success_rate so we don't issue one COUNT
    # query per row (classic N+1 with the old @property approach).
    scripts = (
        Script.objects
        .select_related("environment", "created_by")
        .defer("code", "description")
        .annotate(
            # Use names without leading underscore — Django templates
            # refuse to resolve variables starting with '_' (security:
            # prevents accessing things like _meta). Also avoid clashing
            # with the Script.run_count @property by using distinct names.
            runs_total=models.Count("runs", distinct=True),
            runs_success=models.Count(
                "runs", filter=models.Q(runs__status="success"), distinct=True
            ),
        )
        .order_by("-updated_at")
    )

    # Optional filtering by status
    status_filter = request.GET.get("status")
    if status_filter == "enabled":
        scripts = scripts.filter(is_enabled=True, archived_at__isnull=True)
    elif status_filter == "disabled":
        scripts = scripts.filter(is_enabled=False, archived_at__isnull=True)
    elif status_filter == "archived":
        scripts = scripts.filter(archived_at__isnull=False)
    else:
        # Default "All" excludes archived scripts
        scripts = scripts.filter(archived_at__isnull=True)

    # Filter by tag
    tag_filter = request.GET.get("tag")
    selected_tag = None
    if tag_filter:
        try:
            selected_tag = Tag.objects.get(pk=tag_filter)
            scripts = scripts.filter(tags=selected_tag)
        except (Tag.DoesNotExist, ValueError):
            pass

    # Search by name (handy when you have hundreds of scripts)
    q = request.GET.get("q", "").strip()
    if q:
        scripts = scripts.filter(name__icontains=q)

    # Server-side pagination — 25 per page
    paginator = Paginator(scripts, 25)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)

    # Get all tags for filter dropdown
    all_tags = Tag.objects.all().order_by("name")

    return render(request, "cpanel/scripts/list.html", {
        "scripts": page_obj,
        "page_obj": page_obj,
        "paginator": paginator,
        "status_filter": status_filter,
        "all_tags": all_tags,
        "selected_tag": selected_tag,
        "search_query": q,
    })


@login_required
def script_create_view(request: HttpRequest) -> HttpResponse:
    """Create a new script."""
    if request.method == "POST":
        form = ScriptForm(request.POST)
        if form.is_valid():
            script = form.save(commit=False)
            script.created_by = request.user
            script.save()
            form.save_m2m()  # Save M2M relationships (tags)
            messages.success(request, f'Script "{script.name}" created successfully.')
            return redirect("cpanel:script_detail", pk=script.pk)
    else:
        form = ScriptForm()

    available_tags = Tag.objects.all().order_by("name")
    return render(request, "cpanel/scripts/create.html", {
        "form": form,
        "available_tags": available_tags,
        "selected_tag_ids": [],
        "max_script_size_bytes": Script.max_size_bytes(),
    })


@login_required
def script_detail_view(request: HttpRequest, pk) -> HttpResponse:
    """View script details and recent runs."""
    script = get_object_or_404(
        Script.objects.select_related("environment", "created_by").prefetch_related("tags"),
        pk=pk
    )
    # Defer stdout/stderr/code_snapshot on the runs list — these can be
    # multi-MB each and we don't render them on the detail page.
    recent_runs = (
        script.runs
        .select_related("triggered_by")
        .defer("stdout", "stderr", "code_snapshot")
        .order_by("-created_at")[:10]
    )

    # Ensure schedule exists for this script
    schedule, _ = ScriptSchedule.objects.get_or_create(
        script=script,
        defaults={"created_by": request.user}
    )

    return render(request, "cpanel/scripts/detail.html", {
        "script": script,
        "recent_runs": recent_runs,
        "schedule": schedule,
    })


@login_required
def script_edit_view(request: HttpRequest, pk) -> HttpResponse:
    """Edit an existing script and its schedule."""
    script = get_object_or_404(Script, pk=pk)

    # Get or create schedule for this script
    schedule, created = ScriptSchedule.objects.get_or_create(
        script=script,
        defaults={"created_by": request.user}
    )

    if request.method == "POST":
        form = ScriptForm(request.POST, instance=script)
        schedule_form = ScheduleForm(request.POST, instance=schedule)

        if form.is_valid() and schedule_form.is_valid():
            # Capture previous config for history
            previous_config = {
                "run_mode": schedule.run_mode,
                "interval_minutes": schedule.interval_minutes,
                "daily_times": schedule.daily_times,
                "timezone": schedule.timezone,
                "is_active": schedule.is_active,
            }

            script = form.save(commit=False)
            script.save()
            form.save_m2m()
            schedule = schedule_form.save()

            # Capture new config
            new_config = {
                "run_mode": schedule.run_mode,
                "interval_minutes": schedule.interval_minutes,
                "daily_times": schedule.daily_times,
                "timezone": schedule.timezone,
                "is_active": schedule.is_active,
            }

            # Create history entry if changed
            if previous_config != new_config:
                change_type = (
                    ScheduleHistory.ChangeType.CREATED
                    if created
                    else ScheduleHistory.ChangeType.UPDATED
                )
                ScheduleHistory.objects.create(
                    schedule=schedule,
                    change_type=change_type,
                    previous_config=previous_config if not created else None,
                    new_config=new_config,
                    changed_by=request.user,
                )

            # Sync with django-q2
            ScheduleService.sync_schedule(schedule)

            messages.success(request, f'Script "{script.name}" updated successfully.')
            return redirect("cpanel:script_detail", pk=script.pk)
    else:
        form = ScriptForm(instance=script)
        schedule_form = ScheduleForm(instance=schedule)

    available_tags = Tag.objects.all().order_by("name")
    selected_tag_ids = list(script.tags.values_list("pk", flat=True))
    return render(request, "cpanel/scripts/edit.html", {
        "form": form,
        "schedule_form": schedule_form,
        "script": script,
        "available_tags": available_tags,
        "selected_tag_ids": selected_tag_ids,
        "max_script_size_bytes": Script.max_size_bytes(),
    })


@login_required
@require_POST
def script_run_view(request: HttpRequest, pk) -> HttpResponse:
    """Trigger a manual script run."""
    script = get_object_or_404(Script, pk=pk)

    if not script.can_run:
        if script.is_archived:
            messages.error(request, "Cannot run an archived script.")
        else:
            messages.error(request, "Cannot run a disabled script.")
        return redirect("cpanel:script_detail", pk=pk)

    run = _create_run(script, triggered_by=request.user, trigger_type=Run.TriggerType.MANUAL)

    # Queue for async execution via django-q2
    try:
        queue_script_run(run)
        messages.info(request, f'Script "{script.name}" has been queued for execution.')
    except Exception as e:
        run.status = Run.Status.FAILED
        run.stderr = f"Failed to queue task: {str(e)}"
        run.save()
        messages.error(request, f"Failed to queue script: {str(e)}")

    return redirect("cpanel:run_detail", pk=run.pk)


def _create_run(script: Script, *, triggered_by=None, trigger_type: Run.TriggerType = Run.TriggerType.MANUAL) -> Run:
    """
    Create a Run with a smart code snapshot.

    If the script's code_sha256 matches the most recent run's
    code_snapshot_sha256, we skip storing a duplicate of the code body
    (huge savings on repeated runs of 100K+ line scripts). We always
    store the hash so the snapshot can be reconstructed.
    """
    last_run = (
        Run.objects.filter(script=script)
        .order_by("-created_at")
        .only("code_snapshot_sha256")
        .first()
    )
    current_sha = script.code_sha256

    if last_run and last_run.code_snapshot_sha256 == current_sha:
        # Code hasn't changed — don't duplicate the body.
        return Run.objects.create(
            script=script,
            status=Run.Status.PENDING,
            triggered_by=triggered_by,
            trigger_type=trigger_type,
            code_snapshot="",  # empty body, hash below is authoritative
            code_snapshot_sha256=current_sha,
        )

    # First run OR code changed — store the snapshot body.
    return Run.objects.create(
        script=script,
        status=Run.Status.PENDING,
        triggered_by=triggered_by,
        trigger_type=trigger_type,
        code_snapshot=script.code,
        code_snapshot_sha256=current_sha,
    )


@login_required
@require_POST
def script_toggle_view(request: HttpRequest, pk) -> HttpResponse:
    """Toggle script enabled/disabled state."""
    script = get_object_or_404(Script, pk=pk)
    script.is_enabled = not script.is_enabled
    script.save(update_fields=["is_enabled", "updated_at"])

    status = "enabled" if script.is_enabled else "disabled"
    messages.success(request, f'Script "{script.name}" is now {status}.')
    return redirect("cpanel:script_detail", pk=pk)


@login_required
@require_POST
def schedule_toggle_view(request: HttpRequest, pk) -> HttpResponse:
    """Toggle schedule active/inactive state."""
    script = get_object_or_404(Script, pk=pk)

    try:
        schedule = script.schedule
    except ScriptSchedule.DoesNotExist:
        messages.error(request, "No schedule configured for this script.")
        return redirect("cpanel:script_detail", pk=pk)

    previous_active = schedule.is_active
    schedule.is_active = not schedule.is_active
    schedule.save(update_fields=["is_active", "updated_at"])

    # Record history
    ScheduleHistory.objects.create(
        schedule=schedule,
        change_type=(
            ScheduleHistory.ChangeType.ENABLED
            if schedule.is_active
            else ScheduleHistory.ChangeType.DISABLED
        ),
        previous_config={"is_active": previous_active},
        new_config={"is_active": schedule.is_active},
        changed_by=request.user,
    )

    # Sync with django-q2
    ScheduleService.sync_schedule(schedule)

    status = "enabled" if schedule.is_active else "paused"
    messages.success(request, f'Schedule for "{script.name}" is now {status}.')
    return redirect("cpanel:script_detail", pk=pk)


@login_required
def schedule_history_view(request: HttpRequest, pk) -> HttpResponse:
    """View schedule change history."""
    script = get_object_or_404(Script, pk=pk)

    try:
        schedule = script.schedule
        history = schedule.history.select_related("changed_by").order_by("-created_at")
    except ScriptSchedule.DoesNotExist:
        history = []
        schedule = None

    return render(request, "cpanel/scripts/schedule_history.html", {
        "script": script,
        "schedule": schedule,
        "history": history,
    })


@login_required
@require_POST
def webhook_enable_view(request: HttpRequest, pk) -> HttpResponse:
    """Enable webhook for a script (creates token if not exists)."""
    script = get_object_or_404(Script, pk=pk)

    if not script.webhook_token:
        script.create_webhook_token()
        messages.success(request, f'Webhook enabled for "{script.name}".')
    else:
        messages.info(request, "Webhook is already enabled.")

    return redirect("cpanel:script_detail", pk=pk)


@login_required
@require_POST
def webhook_disable_view(request: HttpRequest, pk) -> HttpResponse:
    """Disable webhook for a script (removes token)."""
    script = get_object_or_404(Script, pk=pk)

    if script.webhook_token:
        script.clear_webhook_token()
        messages.success(request, f'Webhook disabled for "{script.name}".')
    else:
        messages.info(request, "Webhook is already disabled.")

    return redirect("cpanel:script_detail", pk=pk)


@login_required
@require_POST
def webhook_regenerate_view(request: HttpRequest, pk) -> HttpResponse:
    """Regenerate webhook token (invalidates old URL)."""
    script = get_object_or_404(Script, pk=pk)

    script.regenerate_webhook_token()
    messages.success(request, f'Webhook URL regenerated for "{script.name}". The old URL is now invalid.')

    return redirect("cpanel:script_detail", pk=pk)


@login_required
@require_POST
def script_archive_view(request: HttpRequest, pk) -> HttpResponse:
    """Archive a script (soft delete)."""
    from django.utils import timezone

    script = get_object_or_404(Script, pk=pk)

    if script.is_archived:
        messages.info(request, f'Script "{script.name}" is already archived.')
        return redirect("cpanel:script_detail", pk=pk)

    # Archive the script
    script.archived_at = timezone.now()
    script.archived_by = request.user
    script.save(update_fields=["archived_at", "archived_by", "updated_at"])

    # Pause the schedule if it exists and is active
    try:
        schedule = script.schedule
        if schedule.is_active:
            schedule.is_active = False
            schedule.save(update_fields=["is_active", "updated_at"])
            ScheduleService.sync_schedule(schedule)
    except ScriptSchedule.DoesNotExist:
        pass

    messages.success(request, f'Script "{script.name}" has been archived.')
    return redirect("cpanel:script_list")


@login_required
@require_POST
def script_restore_view(request: HttpRequest, pk) -> HttpResponse:
    """Restore an archived script."""
    script = get_object_or_404(Script, pk=pk)

    if not script.is_archived:
        messages.info(request, f'Script "{script.name}" is not archived.')
        return redirect("cpanel:script_detail", pk=pk)

    # Restore the script
    script.archived_at = None
    script.archived_by = None
    script.save(update_fields=["archived_at", "archived_by", "updated_at"])

    messages.success(request, f'Script "{script.name}" has been restored.')
    return redirect("cpanel:script_detail", pk=pk)


@login_required
@require_POST
def script_delete_view(request: HttpRequest, pk) -> HttpResponse:
    """Permanently delete an archived script."""
    script = get_object_or_404(Script, pk=pk)

    if not script.is_archived:
        messages.error(request, "Only archived scripts can be permanently deleted.")
        return redirect("cpanel:script_detail", pk=pk)

    name = script.name
    script.delete()  # CASCADE will handle runs and schedule

    messages.success(request, f'Script "{name}" has been permanently deleted.')
    return redirect("cpanel:script_list")


# ---------------------------------------------------------------------------
# Chunked upload endpoint for very large scripts (optional alternative to
# the standard POST form). Used by the editor's "Save via chunked XHR"
# path when the script body exceeds ~5MB. Falls back transparently to the
# regular form for small scripts.
# ---------------------------------------------------------------------------

import json as _json
from django.views.decorators.csrf import csrf_exempt


@login_required
@require_POST
def script_chunked_upload_init(request: HttpRequest) -> JsonResponse:
    """
    Initialize a chunked upload session.

    POST body (JSON):
        { "name": "...", "description": "...", "total_size": 12345 }

    Returns:
        { "upload_id": "uuid", "chunk_size": 262144 }
    """
    try:
        body = _json.loads(request.body or b"{}")
    except _json.JSONDecodeError:
        return HttpResponseBadRequest("Invalid JSON")

    name = (body.get("name") or "").strip()
    total_size = int(body.get("total_size") or 0)
    if not name:
        return HttpResponseBadRequest("name is required")
    if total_size <= 0:
        return HttpResponseBadRequest("total_size must be positive")

    max_bytes = Script.max_size_bytes()
    if total_size > max_bytes:
        return JsonResponse(
            {"error": f"Total size {total_size} exceeds limit {max_bytes}"},
            status=413,
        )

    import uuid as _uuid
    upload_id = str(_uuid.uuid4())
    # Stash session metadata in the user's session (10 minute TTL)
    request.session[f"chunked_upload_{upload_id}"] = {
        "name": name[:200],
        "description": (body.get("description") or "")[:10000],
        "total_size": total_size,
        "received": 0,
        "environment_id": body.get("environment_id"),
        "tags": body.get("tags", []),
        "timeout_seconds": body.get("timeout_seconds", 3600),
        "is_enabled": body.get("is_enabled", True),
    }
    return JsonResponse({
        "upload_id": upload_id,
        "chunk_size": 262144,  # 256KB chunks
    })


@login_required
@require_POST
def script_chunked_upload_chunk(request: HttpRequest, upload_id: str) -> JsonResponse:
    """
    Append a chunk to an in-progress upload.

    Raw body = chunk bytes.
    Headers:
        X-Chunk-Offset: byte offset within the final file
        X-Chunk-Size:   chunk size in bytes (must match len(body))
        Content-Type:   application/octet-stream
    """
    session_key = f"chunked_upload_{upload_id}"
    meta = request.session.get(session_key)
    if not meta:
        return JsonResponse({"error": "Unknown or expired upload_id"}, status=404)

    try:
        offset = int(request.headers.get("X-Chunk-Offset", "-1"))
        chunk_size = int(request.headers.get("X-Chunk-Size", "-1"))
    except ValueError:
        return HttpResponseBadRequest("Invalid chunk headers")
    if offset < 0 or chunk_size <= 0:
        return HttpResponseBadRequest("Missing chunk headers")

    # Read raw body as bytes — Django has already accepted it under the
    # raised DATA_UPLOAD_MAX_MEMORY_SIZE for a single chunk.
    chunk = request.body
    if len(chunk) != chunk_size:
        return HttpResponseBadRequest(
            f"Chunk size mismatch: header={chunk_size} actual={len(chunk)}"
        )

    # Spool the chunk to disk
    from django.conf import settings as _settings
    from pathlib import Path
    import os as _os
    spool_dir = Path(_settings.DATA_DIR) / "chunked_uploads"
    spool_dir.mkdir(parents=True, exist_ok=True)
    spool_path = spool_dir / f"{upload_id}.bin"

    # Write at offset — open in r+b so we can seek and overwrite
    if not spool_path.exists() and offset == 0:
        # First chunk — create file
        with open(spool_path, "wb") as f:
            f.write(chunk)
    else:
        with open(spool_path, "r+b") as f:
            f.seek(offset)
            f.write(chunk)

    meta["received"] = meta.get("received", 0) + len(chunk)
    request.session[session_key] = meta
    request.session.modified = True

    return JsonResponse({"received": meta["received"], "total": meta["total_size"]})


@login_required
@require_POST
def script_chunked_upload_complete(request: HttpRequest, upload_id: str) -> JsonResponse:
    """
    Finalize a chunked upload: read the spooled file, validate, create Script.

    Returns:
        { "success": true, "script_id": "uuid" }
    """
    session_key = f"chunked_upload_{upload_id}"
    meta = request.session.get(session_key)
    if not meta:
        return JsonResponse({"error": "Unknown or expired upload_id"}, status=404)

    from pathlib import Path
    from django.conf import settings as _settings
    spool_path = Path(_settings.DATA_DIR) / "chunked_uploads" / f"{upload_id}.bin"
    if not spool_path.exists():
        return JsonResponse({"error": "Spool file missing"}, status=500)

    actual_size = spool_path.stat().st_size
    if actual_size != meta["total_size"]:
        return JsonResponse(
            {"error": f"Size mismatch: expected {meta['total_size']}, got {actual_size}"},
            status=400,
        )

    # Read the file in chunks to avoid peak-memory spikes on 50MB scripts.
    chunks = []
    with open(spool_path, "rb") as f:
        while True:
            b = f.read(262_144)
            if not b:
                break
            chunks.append(b)
    code_bytes = b"".join(chunks)
    code = code_bytes.decode("utf-8", errors="replace")
    del code_bytes, chunks  # free memory

    # Build the Script
    try:
        env_id = meta.get("environment_id")
        from core.models import Environment
        env = Environment.objects.filter(is_active=True, pk=env_id).first()
        if not env:
            return JsonResponse({"error": "Invalid environment"}, status=400)

        script = Script.objects.create(
            name=meta["name"],
            description=meta.get("description", ""),
            code=code,
            environment=env,
            timeout_seconds=meta.get("timeout_seconds", 3600),
            is_enabled=meta.get("is_enabled", True),
            created_by=request.user,
        )
        # Tags
        tag_ids = meta.get("tags", [])
        if tag_ids:
            script.tags.set(tag_ids)
    finally:
        # Cleanup
        try:
            spool_path.unlink()
        except OSError:
            pass
        del request.session[session_key]
        request.session.modified = True

    return JsonResponse({"success": True, "script_id": str(script.pk)})
