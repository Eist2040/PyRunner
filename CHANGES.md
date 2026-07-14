# PyRunner — Large-Script Support + Performance Improvements

This patch fixes the root cause that prevented PyRunner from handling
scripts with **100,000+ lines of code**, and ships a substantial set of
performance and robustness improvements across the executor, models,
views, templates, and storage layer.

---

## HOTFIX v3 — Scripts list page 500 crash

The v2 patch crashed the **Scripts list page** (`/cpanel/scripts/`)
with a 500 error because of two Django template bugs in
`templates/cpanel/scripts/list.html`:

1. **Arithmetic in a template tag** — Django templates don't support
   `*`, `/`, `+`, `-` inside `{% if %}` expressions. The line
   `{% if script.code_size > 1024*1024 %}` parsed `1024*1024` as a
   single token, which failed to resolve to a number, and the
   subsequent `int > None` comparison raised `TypeError` → 500.
   Fixed by using the literal `1048576` (single integer — Django
   parses these fine).

2. **Underscore-prefixed annotation names** — Django templates
   intentionally refuse to resolve variables starting with `_` (a
   security feature to prevent access to `_meta` and other internals).
   The annotations `_run_count` and `_success_count` therefore
   silently resolved to empty strings in the template, breaking the
   Runs and Success Rate columns.
   Fixed by renaming to `runs_total` and `runs_success` (and updating
   the view to match).

If you applied v2 and saw 500s on the Scripts page, re-deploy with
this v3 zip — only `core/views/scripts.py` and
`templates/cpanel/scripts/list.html` changed.

---

## HOTFIX v2 — `DataStoreService` → `DatastoreService` (boot crash)

The first version of this patch had a class-name typo in
`core/services/__init__.py`:

```python
# WRONG (was in v1)
from .datastore_service import DataStoreService   # ← capital S
# CORRECT (v2)
from .datastore_service import DatastoreService   # ← lowercase s
```

The actual class in `core/services/datastore_service.py` is
`DatastoreService` (lowercase **s**), so the wrong-cased import raised
`ImportError` on every boot, which made the container restart-loop on
Coolify. The fix is to use the same casing as the original repo.

---


## Root cause of the 100K-line blocker

Django ships with `DATA_UPLOAD_MAX_MEMORY_SIZE = 2_621_440` (2.5 MB) by
default. A 100,000-line Python script easily weighs 5–10 MB, so Django
rejected the POST with `RequestDataTooBig` **before** the view ever ran.
The same ceiling applied to `FILE_UPLOAD_MAX_MEMORY_SIZE`. On top of
that, the executor capped captured output at 1 MB and used
`proc.communicate()`, which buffers the entire stdout/stderr in RAM.

## The fix in one sentence

Raise the upload ceilings to 100 MB (env-tunable), cap captured output
at 50 MB inline and **spool** anything bigger to disk, stream the
subprocess pipes in 256 KB chunks, and add a chunked-upload endpoint +
UI for scripts larger than 5 MB.

---

## Files modified / added

| Path | Status | Why |
|------|--------|-----|
| `pyrunner/settings.py` | MODIFIED | Raises `DATA_UPLOAD_MAX_MEMORY_SIZE` / `FILE_UPLOAD_MAX_MEMORY_SIZE` to 100 MB. Adds `MAX_SCRIPT_SIZE_BYTES` (50 MB default), `MAX_OUTPUT_BYTES` (50 MB), `MAX_OUTPUT_SPOOL_BYTES` (2 GB), `OUTPUT_SPOOL_THRESHOLD` (4 MB), `MAX_WEBHOOK_BODY_BYTES` (10 MB), `OUTPUT_SPOOL_DIR`, `DEFAULT_PAGE_SIZE`, `MAX_PAGE_SIZE`. Adds SQLite `mmap_size` + bigger `cache_size` PRAGMAs for fast large-row reads. |
| `core/executor.py` | REWRITTEN | Streams stdout/stderr in 256 KB chunks via threaded `os.read()`; spools to disk past threshold; uses a **single compiled regex** for secret masking (O(n) instead of O(n×m)); writes script body in 256 KB chunks; isolates the subprocess in its own process group via `os.setsid`; SIGTERM→SIGKILL kill tree; runs Python with `-I` (isolated mode). |
| `core/models/script.py` | MODIFIED | Adds `code_sha256`, `code_size`, `code_line_count` fields; computes them in `save()`; adds `Script.max_size_bytes()` classmethod and `Script.compute_hash()` static; adds index on `(archived_at, -updated_at)`. |
| `core/models/run.py` | MODIFIED | Adds `stdout_spooled`, `stderr_spooled`, `stdout_size`, `stderr_size`, `code_snapshot_sha256` fields; `code_snapshot` is now optional (only stored when code changed since last run — massive DB savings for repeated runs of big scripts); adds index on `-created_at`. Adds `stdout_total_size` / `stderr_total_size` properties. |
| `core/migrations/0024_large_script_support.py` | NEW | Adds the new fields and indexes; backfills `code_sha256` / `code_size` / `code_line_count` for existing scripts in batched SQL (PostgreSQL uses `pgcrypto` when available). |
| `core/forms.py` | MODIFIED | `ScriptForm.clean_code()` now enforces `MAX_SCRIPT_SIZE_BYTES` and gives a friendly validation error instead of a 500. |
| `core/views/scripts.py` | REWRITTEN | `script_list_view` now annotates `_run_count` + `_success_count` in **one query** (kills the N+1 of `script.run_count`/`script.success_rate`); defers the heavy `code` and `description` fields; adds server-side pagination (25/page) + name search. `script_run_view` and `_create_run()` skip the snapshot body when `code_sha256` matches the previous run. New endpoints `script_chunked_upload_init / chunk / complete` for scripts >5 MB. |
| `core/views/runs.py` | MODIFIED | New `run_output_stream_view` serves byte-range slices of stdout/stderr (works for both inline and spooled outputs) so the browser can virtualize huge outputs in chunks. `run_clear_view` now also deletes spool files for the deleted runs. |
| `core/views/webhooks.py` | MODIFIED | Reads webhook bodies in 64 KB chunks via `request.stream()`; enforces `MAX_WEBHOOK_BODY_BYTES`; sets `body_truncated` flag if exceeded. |
| `core/urls/cpanel.py` | MODIFIED | Wires the new endpoints: `scripts/upload/init/`, `scripts/upload/<id>/chunk/`, `scripts/upload/<id>/complete/`, `runs/<id>/output/<stream>/`. |
| `core/tasks.py` | MODIFIED | Scheduled-run path uses `_create_run()` for the smart snapshot dedup. |
| `core/services/__init__.py` | MODIFIED | Exports `OutputStorageService`. |
| `core/services/output_storage_service.py` | NEW | Disk-spool API for oversized run output: `spool_stream`, `write_text`, `read_stream`, `delete_for_run`, `cleanup_orphans`. |
| `core/services/retention_service.py` | MODIFIED | Cleans spool files when deleting runs; sweeps orphaned spool files at the end of every cleanup cycle. |
| `templates/cpanel/scripts/edit.html` | MODIFIED | Monaco editor enabled with `largeFileOptimizations: true`, `wordBasedSuggestions: false`, `semanticHighlighting: { enabled: false }`; live line/byte counter; transparent chunked-upload path on submit when script >5 MB; bigger default height. |
| `templates/cpanel/scripts/create.html` | MODIFIED | Same Monaco improvements as `edit.html`. |
| `templates/cpanel/scripts/_form_main.html` | MODIFIED | Adds live size/line indicator and "chunked upload" tip line; taller default editor height. |
| `templates/cpanel/scripts/list.html` | MODIFIED | Uses annotated `_run_count` / `_success_count` (no N+1); adds Size column; adds pagination UI; adds name search box. |
| `templates/cpanel/runs/detail.html` | MODIFIED | Detects spooled outputs and shows size + "Load full output" button that streams via the new endpoint in 256 KB chunks; shows "code unchanged" notice when snapshot was deduped; shows output sizes panel when spooled. |

---

## How to apply

1. Copy the contents of this `pyrunner-updated/` tree over your existing
   repo, preserving paths. The files are intentionally **only the ones
   that changed** — your other files are untouched.
2. Apply the migration:
   ```bash
   python manage.py migrate core
   ```
3. (Optional) Tune limits via env vars in your `.env`:
   ```bash
   DATA_UPLOAD_MAX_MEMORY_SIZE=104857600      # 100 MB (default)
   FILE_UPLOAD_MAX_MEMORY_SIZE=104857600      # 100 MB (default)
   MAX_SCRIPT_SIZE_BYTES=52428800             # 50 MB (default)
   MAX_OUTPUT_BYTES=52428800                  # 50 MB inline (default)
   MAX_OUTPUT_SPOOL_BYTES=2147483648          # 2 GB on disk (default)
   OUTPUT_SPOOL_THRESHOLD=4194304             # spool past 4 MB (default)
   MAX_WEBHOOK_BODY_BYTES=10485760            # 10 MB webhook body (default)
   ```
4. (Reverse proxy) If you run behind nginx / Cloudflare / a CDN, make
   sure `client_max_body_size` (or the equivalent) is at least
   `100 MB` so the upload ceiling isn't re-imposed upstream.
5. Restart `qcluster` workers — the new executor is loaded at task
   execution time, so a clean worker restart is the simplest way to
   pick up the changes.

---

## Performance wins at a glance

| Area | Before | After |
|------|--------|-------|
| Max script size (POST) | 2.5 MB (Django default) — **rejects 100K-line scripts** | 100 MB inline, **unlimited via chunked upload** |
| Max captured output | 1 MB (hard truncation) | 50 MB inline, **2 GB spooled to disk** + streaming endpoint |
| Secret masking cost | O(n × m) — full string scan per secret | O(n) — one compiled regex, single pass |
| Script list N+1 | 2 extra COUNT queries per row | **1 query** via `Count(..., filter=...)` annotation |
| Script list payload | Loads full `code` (multi-MB) per row | Defers `code` + `description` |
| Run snapshot storage | Copies entire code on **every** run | Stores hash; copies body **only when changed** |
| Subprocess isolation | Same process group as worker | Own process group → reliable SIGTERM/SIGKILL of child trees |
| Webhook body | `request.body` (full buffer) | Streamed in 64 KB chunks with size cap |
| Huge output rendering | Whole stdout in `<pre>` (browser freezes) | Virtualized 256 KB chunked loader |
| Monaco on 100K-line files | Freezes / crashes tab | `largeFileOptimizations` + disabled expensive features |

---

## Compatibility notes

* The migration is **safe and reversible** — the `RunPython` step only
  backfills `Script.code_sha256 / code_size / code_line_count`; the
  reverse migration is a no-op (data is preserved).
* Existing runs keep working: the new `stdout_spooled` / `stderr_spooled`
  default to `False`, and the existing `stdout` / `stderr` fields still
  hold the full inline content for old runs.
* Existing scripts get `code_sha256` computed on migration, so the
  smart-snapshot dedup works immediately on the next run.
* The new chunked-upload endpoint is **opt-in** — small scripts still
  POST through the regular form, so existing UX is unchanged.

---

## What was NOT touched

* Authentication / authorization (axes, magic-link, invites) — unchanged.
* Schedule service, backup service, S3 service — unchanged.
* Environments, packages, secrets, tags, datastores — unchanged.
* Dashboard, logs, settings, users, services views — unchanged.
* All migrations up to `0023_run_pid` — unchanged.

If a downstream file imports something from a modified module and you
notice a regression, the safest place to look is
`core/views/scripts.py` (the most heavily rewritten file). Everything
else is additive or single-method changes.
