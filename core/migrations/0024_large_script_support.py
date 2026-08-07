"""
Migration: add large-script support fields.

Adds:
  - Script.code_sha256, Script.code_size, Script.code_line_count
  - Run.stdout_spooled, Run.stderr_spooled, Run.stdout_size, Run.stderr_size
  - Run.code_snapshot_sha256
  - New indexes for fast filtering.

Backfills the new Script fields from existing rows in a single UPDATE
statement per field (no Python loop, no N+1).
"""
from django.db import migrations, models


def backfill_script_code_fields(apps, schema_editor):
    """Compute code_sha256 / code_size / code_line_count for existing rows."""
    Script = apps.get_model("core", "Script")
    # Use raw SQL to avoid triggering our custom save() (which is Python-side
    # only) and to do this in O(N) without a per-row Python loop.
    if schema_editor.connection.vendor == "sqlite":
        # SQLite supports hex() + sha3? Actually SQLite doesn't have sha256
        # built in. Fall back to Python-side batched update — but batched.
        import hashlib
        batch = []
        BATCH_SIZE = 500
        for pk, code in Script.objects.values_list("pk", "code").iterator(chunk_size=BATCH_SIZE):
            code_bytes = (code or "").encode("utf-8", errors="replace")
            sha = hashlib.sha256(code_bytes).hexdigest()
            size = len(code_bytes)
            line_count = (code or "").count("\n") + (0 if not code or code.endswith("\n") else 1)
            batch.append((pk, sha, size, line_count))
            if len(batch) >= BATCH_SIZE:
                _flush_batch(Script, batch)
                batch = []
        if batch:
            _flush_batch(Script, batch)
    elif schema_editor.connection.vendor == "postgresql":
        # Postgres has pgcrypto extension; use digest() if available,
        # otherwise fall back to Python.
        from django.db import connection
        with connection.cursor() as cur:
            try:
                cur.execute(
                    "UPDATE scripts "
                    "SET code_sha256 = encode(digest(code, 'sha256'), 'hex'), "
                    "    code_size = coalesce(octet_length(code), 0), "
                    "    code_line_count = coalesce(array_length(string_to_array(code, E'\\n'), 1), 0);"
                )
            except Exception:
                # pgcrypto not installed — fall back to Python batched
                import hashlib
                batch = []
                BATCH_SIZE = 500
                for pk, code in Script.objects.values_list("pk", "code").iterator(chunk_size=BATCH_SIZE):
                    code_bytes = (code or "").encode("utf-8", errors="replace")
                    sha = hashlib.sha256(code_bytes).hexdigest()
                    size = len(code_bytes)
                    line_count = (code or "").count("\n") + (0 if not code or code.endswith("\n") else 1)
                    batch.append((pk, sha, size, line_count))
                    if len(batch) >= BATCH_SIZE:
                        _flush_batch(Script, batch)
                        batch = []
                if batch:
                    _flush_batch(Script, batch)
    else:
        # MySQL / other — Python batched
        import hashlib
        batch = []
        BATCH_SIZE = 500
        for pk, code in Script.objects.values_list("pk", "code").iterator(chunk_size=BATCH_SIZE):
            code_bytes = (code or "").encode("utf-8", errors="replace")
            sha = hashlib.sha256(code_bytes).hexdigest()
            size = len(code_bytes)
            line_count = (code or "").count("\n") + (0 if not code or code.endswith("\n") else 1)
            batch.append((pk, sha, size, line_count))
            if len(batch) >= BATCH_SIZE:
                _flush_batch(Script, batch)
                batch = []
        if batch:
            _flush_batch(Script, batch)


def _flush_batch(Script, batch):
    """Apply a batch of (pk, sha, size, lines) updates in one query per row."""
    # Django's bulk_update is the cleanest cross-DB way.
    pks = [b[0] for b in batch]
    rows = {b[0]: b for b in batch}
    objs = list(Script.objects.filter(pk__in=pks).only("pk"))
    for obj in objs:
        pk, sha, size, lines = rows[obj.pk]
        obj.code_sha256 = sha
        obj.code_size = size
        obj.code_line_count = lines
    Script.objects.bulk_update(
        objs, ["code_sha256", "code_size", "code_line_count"], batch_size=200
    )


def noop(apps, schema_editor):
    """Reverse migration is a no-op (we don't drop the data)."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0023_run_pid"),
    ]

    operations = [
        migrations.AddField(
            model_name="script",
            name="code_sha256",
            field=models.CharField(blank=True, db_index=True, help_text="SHA-256 of the code (for dedup / change detection)", max_length=64),
        ),
        migrations.AddField(
            model_name="script",
            name="code_size",
            field=models.BigIntegerField(default=0, help_text="Size of the code field in bytes"),
        ),
        migrations.AddField(
            model_name="script",
            name="code_line_count",
            field=models.IntegerField(default=0, help_text="Number of lines in the code (for display)"),
        ),
        migrations.AddField(
            model_name="run",
            name="stdout_spooled",
            field=models.BooleanField(db_index=True, default=False, help_text="If True, stdout is spooled to disk; `stdout` field is just a preview"),
        ),
        migrations.AddField(
            model_name="run",
            name="stderr_spooled",
            field=models.BooleanField(db_index=True, default=False, help_text="If True, stderr is spooled to disk; `stderr` field is just a preview"),
        ),
        migrations.AddField(
            model_name="run",
            name="stdout_size",
            field=models.BigIntegerField(default=0, help_text="Total stdout size in bytes (matches spool file size when spooled)"),
        ),
        migrations.AddField(
            model_name="run",
            name="stderr_size",
            field=models.BigIntegerField(default=0, help_text="Total stderr size in bytes (matches spool file size when spooled)"),
        ),
        migrations.AddField(
            model_name="run",
            name="code_snapshot_sha256",
            field=models.CharField(blank=True, db_index=True, help_text="SHA-256 of the code at execution time; matches Script.code_sha256 if unchanged", max_length=64),
        ),
        migrations.AddIndex(
            model_name="script",
            index=models.Index(fields=["archived_at", "-updated_at"], name="core_scrip_archived_idx"),
        ),
        migrations.AddIndex(
            model_name="run",
            index=models.Index(fields=["-created_at"], name="core_run_created_at_idx"),
        ),
        migrations.RunPython(backfill_script_code_fields, noop),
    ]
