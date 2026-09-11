"""Google Sheets persistence through the project's Apps Script Web App."""

import contextvars
import time
from datetime import datetime, timezone

from app.drive_service import call_apps_script
from app.models import User, UserRole


class StoreError(RuntimeError):
    pass


def _call(payload: dict) -> dict:
    try:
        return call_apps_script(payload)
    except Exception as exc:
        raise StoreError(str(exc)) from exc


def initialize() -> None:
    _call({"action": "initializeDataStore"})


# --- Request-scoped read cache -----------------------------------------
#
# Every `rows(table)` call is a full-table read over HTTP to the Apps
# Script. Several endpoints (case/document listing in particular) call it
# once per row in a loop -- e.g. listing N cases used to mean N+1 identical
# reads of the same Courses/Users table. `reset_request_cache()` is called
# once per HTTP request (see main.py's middleware); within that request,
# repeated reads of the same table are served from memory instead of the
# network. Any write invalidates that table's cached copy so later reads in
# the same request see fresh data.

_request_cache: contextvars.ContextVar[dict | None] = contextvars.ContextVar("_request_cache", default=None)


def reset_request_cache() -> None:
    _request_cache.set({})


# --- Cross-request table cache ------------------------------------------
#
# A single Apps Script round-trip costs 2.5-4.5 seconds no matter how little
# work it does (measured directly -- this is Google's inherent per-call
# overhead, not something in our control). Every authenticated request pays
# that cost at least once just for get_current_user's Users lookup, and
# clicking around the app fires several requests in quick succession that
# would otherwise re-fetch the same barely-changed tables over and over.
# This caches each table in the process for a short TTL. Any write made
# through this backend (signup, case create/update/delete, document upload,
# etc.) invalidates that table immediately, so you always see your own
# writes right away -- the cache can only ever be stale with respect to
# something that changed *outside* this backend, i.e. a row someone edited
# by hand directly in the Sheet (see ADMIN_SETUP.md), which can take up to
# TABLE_CACHE_TTL_SECONDS to be picked up.

TABLE_CACHE_TTL_SECONDS = 20

_table_cache: dict[str, tuple[float, list[dict]]] = {}


def _get_cached_rows(table: str) -> list[dict]:
    cached = _table_cache.get(table)
    now_ts = time.monotonic()
    if cached is not None and (now_ts - cached[0]) < TABLE_CACHE_TTL_SECONDS:
        return cached[1]
    data = _call({"action": "dataGet", "table": table}).get("rows", [])
    _table_cache[table] = (now_ts, data)
    return data


def _invalidate(table: str) -> None:
    cache = _request_cache.get()
    if cache is not None:
        cache.pop(table, None)
    _table_cache.pop(table, None)


def rows(table: str) -> list[dict]:
    cache = _request_cache.get()
    if cache is not None and table in cache:
        return cache[table]
    data = _get_cached_rows(table)
    if cache is not None:
        cache[table] = data
    return data


def insert(table: str, row: dict) -> dict:
    result = _call({"action": "dataInsert", "table": table, "row": row})["row"]
    _invalidate(table)
    return result


def update(table: str, row_id: int | str, row: dict) -> dict:
    result = _call({"action": "dataUpdate", "table": table, "id": str(row_id), "row": row})["row"]
    _invalidate(table)
    return result


def delete(table: str, row_id: int | str) -> bool:
    result = bool(_call({"action": "dataDelete", "table": table, "id": str(row_id)}).get("deleted"))
    _invalidate(table)
    return result


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def as_int(value: str | int | None, default: int | None = None) -> int | None:
    if value in (None, ""):
        return default
    return int(value)


def as_optional_int(value: str | int | None) -> int | None:
    return as_int(value)


def find_user_by_email(email: str) -> User | None:
    row = next((item for item in rows("Users") if item["email"].lower() == email.lower()), None)
    return user_from_row(row) if row else None


def find_user_by_id(user_id: int) -> User | None:
    row = next((item for item in rows("Users") if as_int(item["id"]) == user_id), None)
    return user_from_row(row) if row else None


def user_from_row(row: dict) -> User:
    return User(
        id=as_int(row["id"]),
        email=row["email"],
        hashed_password=row["hashed_password"],
        full_name=row["full_name"],
        role=UserRole(row["role"]),
    )


def create_user(email: str, hashed_password: str, full_name: str, role: UserRole) -> User:
    """Duplicate-checks and inserts in a single Apps Script round-trip
    (server-side action `createUser`) instead of a separate lookup-then-insert
    pair of calls. Raises StoreError("DUPLICATE_EMAIL") if the email exists.
    """
    data = _call(
        {
            "action": "createUser",
            "row": {
                "email": email,
                "hashed_password": hashed_password,
                "full_name": full_name,
                "role": role.value,
                "created_at": now(),
            },
        }
    )
    _invalidate("Users")
    return user_from_row(data["row"])


def case_rows() -> list[dict]:
    return rows("Cases")


def course_rows() -> list[dict]:
    return rows("Courses")


def document_rows() -> list[dict]:
    return rows("Documents")


def case_by_id(case_id: int) -> dict | None:
    return next((item for item in case_rows() if as_int(item["id"]) == case_id), None)


def courses_for_case(case_id: int) -> list[dict]:
    return sorted([item for item in course_rows() if as_int(item["case_id"]) == case_id], key=lambda item: as_int(item["sort_order"], 0))


def documents_for_course(course_id: int) -> list[dict]:
    return [item for item in document_rows() if as_int(item["course_id"]) == course_id]


def documents_for_case(case_id: int) -> list[dict]:
    return [item for item in document_rows() if as_int(item.get("case_id")) == case_id]


def create_case(row: dict, courses: list[dict]) -> tuple[dict, list[dict]]:
    """Duplicate-checks (one case per owner) and inserts the case plus all of
    its courses in a single Apps Script round-trip (server-side action
    `createCaseWithCourses`) instead of 1 read + 1 insert per course.
    Raises StoreError("DUPLICATE_CASE") if the owner already has a case.
    """
    data = _call({"action": "createCaseWithCourses", "caseRow": row, "courses": courses})
    _invalidate("Cases")
    _invalidate("Courses")
    return data["caseRow"], data["courses"]


def replace_case(case_id: int, case_row: dict, courses: list[dict]) -> tuple[dict, list[dict]]:
    data = _call({"action": "replaceCaseQualifications", "caseId": str(case_id), "caseRow": case_row, "courses": courses})
    _invalidate("Cases")
    _invalidate("Courses")
    _invalidate("Documents")
    return data["caseRow"], data["courses"]


def upload_course_document(
    case_id: int,
    course_id: int,
    doc_type: str,
    file_name: str,
    mime_type: str,
    data_base64: str,
    folder_id: str,
    case_name: str,
    uploaded_at: str,
) -> tuple[dict, str]:
    """Duplicate-checks, ensures the Drive folder exists, uploads the file,
    inserts the Documents row, and (if a folder was just created) updates the
    case's drive_folder_id -- all in one Apps Script round-trip (server-side
    action `uploadCourseDocument`) instead of up to ~5 separate calls.
    Raises StoreError("DUPLICATE_DOCUMENT") if that doc_type is already saved.
    """
    data = _call(
        {
            "action": "uploadCourseDocument",
            "caseId": str(case_id),
            "courseId": str(course_id),
            "docType": doc_type,
            "fileName": file_name,
            "mimeType": mime_type,
            "dataBase64": data_base64,
            "folderId": folder_id or "",
            "caseName": case_name,
            "uploadedAt": uploaded_at,
        }
    )
    _invalidate("Documents")
    if not folder_id:
        _invalidate("Cases")
    return data["document"], data["folderId"]


def upload_case_document(
    case_id: int,
    doc_type: str,
    file_name: str,
    mime_type: str,
    data_base64: str,
    folder_id: str,
    case_name: str,
    uploaded_at: str,
) -> tuple[dict, str]:
    """Same as upload_course_document, but for the case-level documents
    (current visa, AFP, PTE, OVHC) that live on the case itself rather than
    on any one qualification (server-side action `uploadCaseDocument`).
    """
    data = _call(
        {
            "action": "uploadCaseDocument",
            "caseId": str(case_id),
            "docType": doc_type,
            "fileName": file_name,
            "mimeType": mime_type,
            "dataBase64": data_base64,
            "folderId": folder_id or "",
            "caseName": case_name,
            "uploadedAt": uploaded_at,
        }
    )
    _invalidate("Documents")
    if not folder_id:
        _invalidate("Cases")
    return data["document"], data["folderId"]
