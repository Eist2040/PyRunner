"""
Script model for user-created Python scripts.
"""

import hashlib
import secrets
import uuid

from django.conf import settings
from django.db import models

from .environment import Environment


# Hard cap on script body size, read from settings (default 50MB).
def _max_script_size_bytes() -> int:
    return getattr(settings, "MAX_SCRIPT_SIZE_BYTES", 50 * 1024 * 1024)


class Script(models.Model):
    """
    Represents a Python script that can be executed.
    Scripts are associated with an environment and can be run manually or on schedule.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    description = models.TextField(blank=True)

    # The actual Python code. TextField has no inherent size limit in
    # SQLite/Postgres, so this can hold 100K+ line scripts without issue.
    code = models.TextField(help_text="Python code to execute")

    # Pre-computed code hash for fast dedup / change detection. Updated on
    # every save() — used by Run.code_snapshot logic to avoid re-storing
    # identical code blobs on every run.
    code_sha256 = models.CharField(
        max_length=64,
        blank=True,
        db_index=True,
        help_text="SHA-256 of the code (for dedup / change detection)",
    )
    code_size = models.BigIntegerField(
        default=0,
        help_text="Size of the code field in bytes",
    )
    code_line_count = models.IntegerField(
        default=0,
        help_text="Number of lines in the code (for display)",
    )

    # Execution settings
    environment = models.ForeignKey(
        Environment,
        on_delete=models.PROTECT,
        related_name="scripts",
        help_text="Python environment to use for execution",
    )

    # Tags for categorization
    tags = models.ManyToManyField(
        "Tag",
        blank=True,
        related_name="scripts",
        help_text="Tags for organizing and filtering scripts",
    )

    timeout_seconds = models.PositiveIntegerField(
        default=3600,  # 1 hour default
        help_text="Maximum execution time in seconds (default: 1 hour, max: 72 hours)",
    )

    # Status
    is_enabled = models.BooleanField(
        default=True,
        help_text="Whether this script can be executed",
    )

    # Webhook
    webhook_token = models.CharField(
        max_length=64,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
        help_text="Unique token for webhook URL (auto-generated)",
    )

    # Notification settings
    class NotifyOn(models.TextChoices):
        NEVER = "never", "Never"
        FAILURE = "failure", "On Failure"
        SUCCESS = "success", "On Success"
        BOTH = "both", "On Success and Failure"

    notify_on = models.CharField(
        max_length=20,
        choices=NotifyOn.choices,
        default=NotifyOn.NEVER,
        help_text="When to send notifications for this script",
    )
    notify_email = models.EmailField(
        blank=True,
        help_text="Override email for this script (uses global default if empty)",
    )
    notify_webhook_url = models.URLField(
        blank=True,
        max_length=500,
        help_text="URL to POST notification webhooks to",
    )
    notify_webhook_enabled = models.BooleanField(
        default=False,
        help_text="Enable webhook notifications for this script",
    )

    # Retention overrides (null = use global settings)
    retention_days_override = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Override global retention days for this script",
    )
    retention_count_override = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Override global retention count for this script",
    )

    # Archive fields (soft delete)
    archived_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text="When this script was archived (null = not archived)",
    )
    archived_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="archived_scripts",
    )

    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="scripts",
    )

    class Meta:
        db_table = "scripts"
        verbose_name = "script"
        verbose_name_plural = "scripts"
        ordering = ["-updated_at"]
        indexes = [
            # Helpful for list views that filter by archived + ordering
            models.Index(fields=["archived_at", "-updated_at"]),
        ]

    def __str__(self):
        if self.is_archived:
            return f"{self.name} (archived)"
        status = "enabled" if self.is_enabled else "disabled"
        return f"{self.name} ({status})"

    @property
    def is_archived(self) -> bool:
        """Check if this script is archived."""
        return self.archived_at is not None

    @property
    def can_run(self) -> bool:
        """Check if this script can be executed (enabled and not archived)."""
        return self.is_enabled and not self.is_archived

    @property
    def last_run(self):
        """Return the most recent run for this script."""
        return self.runs.order_by("-created_at").first()

    @property
    def last_successful_run(self):
        """Return the most recent successful run for this script."""
        return self.runs.filter(status="success").order_by("-created_at").first()

    @property
    def run_count(self) -> int:
        """Return the total number of runs for this script."""
        return self.runs.count()

    @property
    def success_rate(self) -> float | None:
        """Return the success rate as a percentage, or None if no runs."""
        total = self.run_count
        if total == 0:
            return None
        successful = self.runs.filter(status="success").count()
        return (successful / total) * 100

    def get_code_preview(self, max_lines: int = 5) -> str:
        """Return a preview of the script code (first N lines)."""
        lines = self.code.split("\n", max_lines + 1)
        if len(lines) > max_lines:
            return "\n".join(lines[:max_lines]) + "\n..."
        return "\n".join(lines)

    @staticmethod
    def generate_webhook_token() -> str:
        """Generate a secure random webhook token (64 chars, URL-safe)."""
        return secrets.token_urlsafe(48)  # 48 bytes = 64 chars in base64

    def create_webhook_token(self) -> str:
        """Create and save a new webhook token for this script."""
        self.webhook_token = self.generate_webhook_token()
        self.save(update_fields=["webhook_token", "updated_at"])
        return self.webhook_token

    def regenerate_webhook_token(self) -> str:
        """Regenerate the webhook token, invalidating the old one."""
        return self.create_webhook_token()

    def clear_webhook_token(self) -> None:
        """Remove the webhook token, disabling webhook access."""
        self.webhook_token = None
        self.save(update_fields=["webhook_token", "updated_at"])

    @property
    def has_webhook(self) -> bool:
        """Check if this script has a webhook token configured."""
        return bool(self.webhook_token)

    # ----- New helpers for large-script support -----

    @classmethod
    def max_size_bytes(cls) -> int:
        """The server-side hard cap on script body size."""
        return _max_script_size_bytes()

    @staticmethod
    def compute_hash(code: str) -> str:
        """Compute SHA-256 hex digest of code (UTF-8 encoded)."""
        return hashlib.sha256(code.encode("utf-8", errors="replace")).hexdigest()

    def save(self, *args, **kwargs):
        """
        Override save() to maintain code_sha256 / code_size / code_line_count.
        This is cheap (one hash + one split) and saves a lot of work later
        for the run-list and dedup paths.
        """
        if self.code is None:
            self.code = ""
        # Avoid expensive recompute on partial saves that didn't touch `code`.
        update_fields = kwargs.get("update_fields")
        if update_fields is None or "code" in update_fields:
            self.code_sha256 = self.compute_hash(self.code)
            self.code_size = len(self.code.encode("utf-8", errors="replace"))
            # Cheap line count (no full split into a list)
            self.code_line_count = self.code.count("\n") + (0 if not self.code or self.code.endswith("\n") else 1)
            if update_fields is not None:
                # Make sure these computed fields get persisted too
                for f in ("code_sha256", "code_size", "code_line_count"):
                    if f not in update_fields:
                        update_fields = list(update_fields) + [f]
                kwargs["update_fields"] = update_fields
        super().save(*args, **kwargs)
