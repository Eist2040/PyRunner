"""
Webhook views for triggering scripts via HTTP.

Improvements:
  * Webhook body is read up to MAX_WEBHOOK_BODY_BYTES (default 10MB) —
    above that we truncate with a flag instead of letting Django OOM.
  * Body is read via Django's standard HttpRequest iterator
    (`for chunk in request:`) so we don't buffer the entire payload
    into RAM at once.
"""
import json
import logging

from django.conf import settings
from django.core.cache import cache
from django.http import JsonResponse, HttpRequest
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from core.models import Script, Run
from core.tasks import queue_script_run

logger = logging.getLogger(__name__)

WEBHOOK_RATE_LIMIT = 30
WEBHOOK_RATE_WINDOW = 60  # seconds

MAX_BODY_BYTES = getattr(settings, "MAX_WEBHOOK_BODY_BYTES", 10 * 1024 * 1024)


@csrf_exempt
@require_http_methods(["GET", "POST"])
def webhook_trigger_view(request: HttpRequest, token: str) -> JsonResponse:
    """
    Public endpoint to trigger script execution via webhook.
    """
    client_ip = request.META.get("REMOTE_ADDR", "unknown")
    rate_key = f"webhook_rate_{client_ip}"
    requests_count = cache.get(rate_key, 0)

    if requests_count >= WEBHOOK_RATE_LIMIT:
        logger.warning(f"Webhook rate limit exceeded for IP: {client_ip}")
        return JsonResponse(
            {"error": "Rate limit exceeded. Try again later."},
            status=429,
        )

    cache.set(rate_key, requests_count + 1, WEBHOOK_RATE_WINDOW)

    try:
        script = Script.objects.select_related("environment").get(webhook_token=token)
    except Script.DoesNotExist:
        logger.warning(f"Webhook trigger with invalid token: {token[:8]}...")
        return JsonResponse(
            {"error": "Invalid webhook token"},
            status=404,
        )

    if not script.can_run:
        reason = "archived" if script.is_archived else "disabled"
        logger.info(f"Webhook trigger rejected - script {reason}: {script.name}")
        return JsonResponse(
            {"error": f"Script is {reason}"},
            status=403,
        )

    webhook_data = _extract_webhook_data(request)

    run = Run.objects.create(
        script=script,
        status=Run.Status.PENDING,
        triggered_by=None,
        trigger_type=Run.TriggerType.API,
        code_snapshot=script.code,
        code_snapshot_sha256=script.code_sha256,
    )

    run._webhook_data = webhook_data

    try:
        queue_script_run(run, webhook_data=webhook_data)
        logger.info(f"Webhook triggered run {run.id} for script {script.name}")

        return JsonResponse({
            "status": "queued",
            "run_id": str(run.id),
            "script": script.name,
        })

    except Exception as e:
        run.status = Run.Status.FAILED
        run.stderr = f"Failed to queue task: {str(e)}"
        run.save()
        logger.error(f"Webhook failed to queue run {run.id}: {e}")

        return JsonResponse(
            {"error": "Failed to queue script execution"},
            status=500,
        )


def _extract_webhook_data(request: HttpRequest) -> dict:
    """
    Extract webhook data from the request, enforcing a body size cap.

    Uses Django's standard HttpRequest iteration (`for chunk in request:`)
    which yields the body in chunks without buffering the entire payload
    into RAM at once. If the body exceeds MAX_WEBHOOK_BODY_BYTES the
    excess is dropped and a `body_truncated=True` flag is set.

    Note: Django's `HttpRequest` does NOT have a `.stream()` method —
    that's a DRF API. Use the iterator protocol instead.
    """
    data = {
        "method": request.method,
        "query": dict(request.GET),
        "content_type": request.content_type or "",
    }

    if request.method != "POST":
        return data

    # Determine declared body size
    try:
        declared_len = int(request.META.get("CONTENT_LENGTH", 0) or 0)
    except ValueError:
        declared_len = 0

    truncated = declared_len > MAX_BODY_BYTES
    if declared_len and declared_len > MAX_BODY_BYTES:
        logger.warning(
            f"Webhook body truncation: declared {declared_len} > limit {MAX_BODY_BYTES}"
        )

    # Stream the body in chunks using Django's iterator protocol.
    # Each iteration yields a bytes chunk (typically 64KB depending on
    # the handler). We stop once we've collected MAX_BODY_BYTES.
    chunks = []
    received = 0
    for chunk in request:
        if not chunk:
            continue
        if received + len(chunk) > MAX_BODY_BYTES:
            allowed = MAX_BODY_BYTES - received
            if allowed > 0:
                chunks.append(chunk[:allowed])
                received += allowed
            truncated = True
            break
        chunks.append(chunk)
        received += len(chunk)

    raw = b"".join(chunks)
    try:
        body_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        body_text = raw.decode("utf-8", errors="replace")

    data["body"] = body_text
    data["body_truncated"] = truncated
    data["body_size"] = received

    # Try JSON parse for convenience
    if request.content_type == "application/json":
        try:
            data["body_json"] = json.loads(body_text)
        except json.JSONDecodeError:
            pass

    return data
