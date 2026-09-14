# S3-Primary / Google Drive-Backup Document Pipeline

## Context

Today, documents submitted from the frontend are parsed for insights in the backend, then
uploaded directly to Google Drive via Apps Script. Drive upload is the slow part of the
request, so the frontend waits longer than necessary for a response.

Goal: make S3 (AIC Cloud, S3-compatible) the fast primary store, respond to the frontend as
soon as the doc is safely in S3, and sync each doc to Drive afterward via a decoupled
background worker — with Drive kept in its existing `485_docs/<client-name>/docname.ext`
folder convention, and S3 staying as the permanent primary copy (Drive is backup, not staging).

Constraints from discussion:
- Backend: Python (FastAPI).
- "Database" today is a Google Sheet, driven through Apps Script.
- No existing message broker (no Redis/RabbitMQ) — nothing to install for this if avoidable.
- AIC Cloud is S3-compatible but not a full AWS clone — do not assume AWS-only features
  (S3 Event Notifications, SQS, Lambda triggers, SSE-KMS, Glacier tiering) are available.
  Any "queue" here must be something *we* build and control, not an S3-native trigger.
- Sync status per document must be visible somewhere the user can check.

## Approach

### 1. Upload path (fast, synchronous, user-facing)
1. Frontend sends doc + details to FastAPI.
2. FastAPI parses the doc / extracts insights (unchanged).
3. FastAPI uploads the file to S3 with a key that mirrors the Drive convention:
   `485_docs/<client-name>/docname.ext`
4. FastAPI writes one row/record for this document to the existing Google Sheet, including
   new columns:
   - `s3_key`
   - `drive_sync_status` = `pending`
   - `drive_file_id` = "" (filled in later)
   - `synced_at` = ""
   - `last_error` = ""
   - `retry_count` = 0
5. FastAPI immediately returns the response to the frontend (insights + status) — Drive is
   not on this critical path at all anymore.

This is the "queue a message" step — instead of a broker, the pending-sync "message" *is*
the Sheet row with `drive_sync_status = pending`. It's durable (survives a backend restart)
without adding new infrastructure, since the Sheet is already the source of truth for
document state.

### 2. Background sync worker (decoupled from the web process)
Run this as a **separate standalone process**, not as an in-process FastAPI background task.
Reasons:
- If FastAPI is ever deployed with multiple workers (Gunicorn/Uvicorn `--workers N`), an
  in-process poller would run N times and double-process rows.
- Keeps "parse + respond" and "sync to Drive" fully independent — a crash or slowdown in
  one never affects the other.

Worker loop (simple polling, e.g. every 15-30s — tune based on how fresh the backup needs
to be):
1. Batch-read the Sheet once per tick (not per-row) and filter in memory for rows where
   `drive_sync_status` is `pending` or `failed` (with `retry_count` under a max, e.g. 5).
2. For each matching row, immediately flip its status to `syncing` and write that back
   (this "claims" the row so a slow tick or a future second worker doesn't reprocess it).
3. Download the object from S3 via `s3_key`.
4. Resolve (or create, if missing) the `<client-name>` subfolder under the `485_docs`
   parent folder in Drive, using the Drive API v3 directly from Python
   (`google-api-python-client` + a service account) — **not** via Apps Script. Apps Script's
   HTTP round-trip is very likely a chunk of today's slowness; calling Drive API directly
   from the backend removes that hop entirely.
5. Upload the file to that folder.
6. On success: update the row — `drive_sync_status=synced`, `drive_file_id=<id>`,
   `synced_at=<timestamp>`.
7. On failure: `drive_sync_status=failed`, `retry_count += 1`, `last_error=<message>`. Leave
   it for the next tick (up to the retry cap), then leave it in `failed` for manual attention
   once the cap is hit.
8. Add a safety net: if a row has been stuck in `syncing` for longer than some timeout (e.g.
   5 minutes — implies the worker died mid-job), treat it like `failed` and let it be
   retried.

### 3. Visibility
Since the Sheet is already what gets looked at, `drive_sync_status` / `last_error` /
`retry_count` columns directly on each document's row are enough — no new dashboard needed.
If this grows past a few people checking the Sheet by eye, a simple "show me failed rows"
filter/view in the Sheet covers it.

## Open items to settle before implementation

- Poll interval for the worker (tradeoff: Drive freshness vs Google Sheets API quota usage —
  batch-reading once per tick keeps this cheap regardless).
- Where the worker process actually runs/is deployed (systemd service, separate container,
  cron-invoked script with a lock file, etc.) — depends on how the FastAPI backend is
  currently hosted.
- Service account setup for direct Drive API access (needs the `485_docs` parent folder
  shared with the service account, or domain-wide delegation if it must act as a real user).
- Max retry count and what "manual attention" means in practice (Slack/email alert vs. just
  eyeballing the Sheet).

## Verification

- Upload a doc through the frontend; confirm the response returns quickly and the Sheet row
  appears with `drive_sync_status=pending`.
- Let the worker tick run; confirm the file appears in the correct Drive folder and the row
  flips to `synced` with a `drive_file_id`.
- Force a failure (e.g. temporarily break Drive credentials) and confirm the row goes to
  `failed` with a populated `last_error`, then recovers once fixed and the worker retries it.
- Confirm S3 is untouched/retained after a successful Drive sync (it's permanent primary
  storage, not cleaned up).
