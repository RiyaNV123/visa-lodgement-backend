"""Standalone background worker: syncs documents from S3 (the fast,
synchronous primary store every upload already landed in) to Google Drive
(the permanent backup), via the same Apps Script Web App the rest of the
app already uses -- see s3-drive-sync-plan.md for the full design.

Deliberately a SEPARATE PROCESS, not an in-process FastAPI background task:
if the API is ever run with multiple workers, an in-process poller would run
once per worker and double-process the same rows. Run this as its own
process (`python -m app.drive_sync_worker`), however many API workers exist.

Each tick:
  1. Batch-read Cases/Courses/Documents once (not per-row) and pick out every
     Documents row that's `pending`/`failed` (under the retry cap), plus any
     row stuck in `syncing` past the timeout (implies a worker died mid-job).
  2. Claim each matching row by flipping it to `syncing` immediately, before
     doing any slow work -- so a slow tick, or a second worker instance,
     can't double-process it.
  3. Download the file's bytes back out of S3, resolve/create the
     `<student_name>-<case_id>` folder under the existing Drive parent
     folder, and upload -- both via the existing app.drive_service
     functions, i.e. the same Apps Script actions the old synchronous
     upload path used to call inline.
  4. On success: `synced`, with drive_file_id/drive_view_link/synced_at.
     On failure: `failed`, with retry_count incremented and last_error set.
"""

import time
from datetime import datetime, timezone

from app.config import settings
from app.drive_service import DriveServiceError, ensure_case_folder, upload_file
from app.s3_service import download_bytes
from app.sheet_store import as_int, rows, update

RETRYABLE_STATUSES = ("pending", "failed")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seconds_since(iso_timestamp: str) -> float:
    try:
        then = datetime.fromisoformat(iso_timestamp)
    except (TypeError, ValueError):
        return float("inf")  # unparseable/missing -- treat as "stuck forever", safe to reclaim
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).total_seconds()


def _case_name_for(doc: dict, cases_by_id: dict, courses_by_id: dict) -> str | None:
    """Documents are keyed by case_id directly for case-level documents, or
    by course_id for qualification documents (whose own row has no case_id
    of its own -- same historical split as the rest of this codebase). This
    resolves either shape to the "<student_name>-<case_id>" folder-naming
    convention used everywhere else.
    """
    case_id = as_int(doc.get("case_id"))
    if not case_id:
        course = courses_by_id.get(as_int(doc.get("course_id")))
        case_id = as_int(course.get("case_id")) if course else None
    case = cases_by_id.get(case_id) if case_id else None
    if not case:
        return None
    return f"{case['student_name']}-{case_id}"


def sync_once() -> None:
    cases_by_id = {as_int(c["id"]): c for c in rows("Cases")}
    courses_by_id = {as_int(c["id"]): c for c in rows("Courses")}
    documents = rows("Documents")

    claimed = 0
    for doc in documents:
        status = doc.get("drive_sync_status") or "pending"
        retry_count = as_int(doc.get("retry_count"), 0) or 0

        if status == "syncing":
            # `synced_at` doubles as "last transition timestamp" -- stamped
            # the moment a row is claimed below, then overwritten with the
            # real completion time on success. So here it means "how long
            # has this row been claimed", which is exactly what the stuck
            # check needs.
            if _seconds_since(doc.get("synced_at") or "") < settings.sync_worker_stuck_timeout_seconds:
                continue  # still within a normal job's runtime -- leave it alone
            # Past the timeout with no result -- the worker that claimed it
            # almost certainly died mid-job. Treat like a failure and let it
            # be retried below (falls through to the pending/failed check).
            status = "failed"

        if status not in RETRYABLE_STATUSES or retry_count >= settings.sync_worker_max_retries:
            continue

        document_id = doc["id"]
        claimed += 1
        print(f"drive_sync_worker: syncing Documents#{document_id} ({doc.get('file_name')})...", flush=True)
        # Claim it before doing any slow work -- stamps synced_at too, so a
        # second worker (or the next tick, if this one dies) can tell how
        # long it's been claimed.
        update("Documents", document_id, {"drive_sync_status": "syncing", "synced_at": _now_iso()})

        try:
            case_name = _case_name_for(doc, cases_by_id, courses_by_id)
            if not case_name:
                raise DriveServiceError(f"Could not resolve a case for Documents row {document_id}")
            content = download_bytes(doc["s3_key"])
            folder_id = ensure_case_folder(case_name)
            file_id, web_view_link = upload_file(folder_id, doc["file_name"], doc.get("mime_type") or "application/octet-stream", content)
            update("Documents", document_id, {
                "drive_sync_status": "synced",
                "drive_file_id": file_id,
                "drive_view_link": web_view_link,
                "synced_at": _now_iso(),
                "last_error": "",
            })
            print(f"drive_sync_worker: Documents#{document_id} synced -> {file_id}", flush=True)
        except Exception as exc:  # S3ServiceError, DriveServiceError, or anything else -- always land in "failed", never crash the tick
            update("Documents", document_id, {
                "drive_sync_status": "failed",
                "retry_count": retry_count + 1,
                "last_error": str(exc),
            })
            print(f"drive_sync_worker: Documents#{document_id} failed: {exc}", flush=True)

    if claimed:
        print(f"drive_sync_worker: tick done, processed {claimed} row(s)", flush=True)


def main() -> None:
    while True:
        try:
            sync_once()
        except Exception as exc:  # a whole-tick failure (e.g. Sheets unreachable) shouldn't kill the worker
            print(f"drive_sync_worker: tick failed: {exc}")
        time.sleep(settings.sync_worker_poll_seconds)


if __name__ == "__main__":
    main()
