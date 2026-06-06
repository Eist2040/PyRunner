"""
Run views for the control panel.
"""
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.contrib import messages
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse

from core.models import Run, Script


@login_required
def run_list_view(request: HttpRequest) -> HttpResponse:
    """List all runs with pagination. Defers large text blobs for speed."""
    # Defer stdout/stderr/code_snapshot — can be MBs per row, not needed in list
    runs = (
        Run.objects
        .select_related("script", "triggered_by")
        .defer("stdout", "stderr", "code_snapshot")
        .order_by("-created_at")
    )

    # Filter by status
    status_filter = request.GET.get("status")
    if status_filter and status_filter in dict(Run.Status.choices):
        runs = runs.filter(status=status_filter)

    # Filter by script
    script_filter = request.GET.get("script")
    if script_filter:
        runs = runs.filter(script_id=script_filter)

    # Paginate — 50 rows per page
    paginator = Paginator(runs, 50)
    page_number = request.GET.get("page", 1)
    page_obj = paginator.get_page(page_number)

    # Scripts for filter dropdown (name + pk only)
    scripts = Script.objects.only("pk", "name").order_by("name")

    return render(request, "cpanel/runs/list.html", {
        "runs": page_obj,
        "page_obj": page_obj,
        "paginator": paginator,
        "status_choices": Run.Status.choices,
        "status_filter": status_filter,
        "script_filter": script_filter,
        "scripts": scripts,
    })


@login_required
def run_detail_view(request: HttpRequest, pk) -> HttpResponse:
    """View run details including output."""
    run = get_object_or_404(
        Run.objects.select_related("script", "triggered_by"),
        pk=pk
    )
    return render(request, "cpanel/runs/detail.html", {"run": run})


@login_required
@require_POST
def run_clear_view(request: HttpRequest) -> HttpResponse:
    """Delete runs — all or filtered by status/age."""
    mode = request.POST.get("mode", "all")

    if mode == "all":
        count, _ = Run.objects.all().delete()
        messages.success(request, f"Deleted {count} runs.")

    elif mode == "status":
        status = request.POST.get("status")
        if status and status in dict(Run.Status.choices):
            count, _ = Run.objects.filter(status=status).delete()
            messages.success(request, f"Deleted {count} {status} runs.")
        else:
            messages.error(request, "Invalid status.")

    elif mode == "older_than":
        from django.utils import timezone
        from datetime import timedelta
        days = int(request.POST.get("days", 30))
        cutoff = timezone.now() - timedelta(days=days)
        count, _ = Run.objects.filter(created_at__lt=cutoff).delete()
        messages.success(request, f"Deleted {count} runs older than {days} days.")

    else:
        messages.error(request, "Unknown clear mode.")

    return redirect("cpanel:run_list")
