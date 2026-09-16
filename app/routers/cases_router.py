import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date as date_cls

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from app.auth import get_current_user
from app.cricos_lookup import CricosLookupError, lookup_duration_weeks

from app.drive_service import DriveServiceError, ocr_file
from app.eligibility import MIN_TOTAL_WEEKS, CourseInput, Stage1Input, Stage3Input, calculate_duration, check_stage1, check_stage3
from app.models import CASE_DOC_TYPES, DocType, User, UserRole
from app.s3_service import S3ServiceError, download_bytes, upload_bytes
from app.schemas import CaseCreate, CaseCreateFull, CaseDetailResponse, CaseSummaryResponse, CourseCreate, CourseResponse, DocumentResponse, ExtractPreviewResponse
from app.sheet_store import StoreError, as_int, as_optional_int, case_by_id, case_rows, courses_for_case, create_case, create_case_full, delete, documents_for_case, documents_for_course, find_user_by_id, insert, insert_document_record, now, replace_case, rows_multi, update

from app.document_extract import (
    extract_afp_fields,
    extract_afp_fields_from_text,
    extract_coe_fields,
    extract_coe_fields_from_text,
    extract_completion_letter_fields,
    extract_completion_letter_fields_from_text,
    extract_current_visa_fields,
    extract_current_visa_fields_from_text,
    extract_new_coe_fields,
    extract_new_coe_fields_from_text,
    extract_ovhc_fields,
    extract_ovhc_fields_from_text,
    extract_pte_fields,
    extract_pte_fields_from_text,
)

router = APIRouter(prefix="/cases", tags=["cases"])
ALLOWED_DOC_CONTENT_TYPES = {"application/pdf", "image/jpeg", "image/png"}
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
DOCUMENT_FILE_LABELS = {
    DocType.coe: "CoE",
    DocType.completion_letter: "Completion Letter",
    DocType.transcript: "Transcript",
    DocType.academic_certificate: "Academic Certificate",
    DocType.current_visa: "Current Visa",
    DocType.afp_certificate: "AFP Certificate",
    DocType.afp_receipt: "AFP Receipt",
    DocType.pte: "PTE",
    DocType.ovhc: "OVHC",
    DocType.new_coe: "New CoE",
}
CONTENT_TYPE_EXTENSIONS = {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png"}


def sheet_error(exc: StoreError):
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Google Sheets is unavailable: {exc}")


def _load_breakdown(raw: str | None) -> dict | None:
    """Breakdowns are stored as a JSON blob in their own Cases column so a
    result and the exact figures behind it survive across page loads without
    needing to recompute anything -- unlike the status/reason fields, which
    are their own plain columns, a breakdown is a nested structure so JSON is
    the simplest way to round-trip it through a single Sheets cell."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def scoped_case(case_id: int, user: User) -> dict:
    try:
        item = case_by_id(case_id)
    except StoreError as exc:
        raise sheet_error(exc) from exc
    if not item or (user.role != UserRole.admin and as_int(item["owner_user_id"]) != user.id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found")
    return item


def course_for_case(case_id: int, course_id: int, user: User) -> dict:
    scoped_case(case_id, user)
    try:
        course = next((item for item in courses_for_case(case_id) if as_int(item["id"]) == course_id), None)
    except StoreError as exc:
        raise sheet_error(exc) from exc
    if not course:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course not found")
    return course


def course_response(row: dict) -> dict:
    course_id = as_int(row["id"])
    return {"id": course_id, "name": row["name"], "course_type": row["course_type"], "start_date": row["start_date"] or None, "end_date": row["end_date"] or None, "cricos_weeks": as_optional_int(row["cricos_weeks"]), "cricos_code": row.get("cricos_code") or None, "sort_order": as_int(row["sort_order"], 0), "documents": [document_response(item) for item in documents_for_course(course_id)]}


def document_response(row: dict) -> dict:
    return {"id": as_int(row["id"]), "doc_type": row["doc_type"], "file_name": row["file_name"], "drive_view_link": row["drive_view_link"], "uploaded_at": row["uploaded_at"]}


def detail_response(case: dict) -> dict:
    case_id = as_int(case["id"])
    owner = find_user_by_id(as_int(case["owner_user_id"]))
    return {
        "id": case_id,
        "student_name": case["student_name"],
        "stream": case["stream"],
        "status": case["status"],
        "created_at": case["created_at"],
        "owner_email": owner.email if owner else None,
        "courses": [course_response(row) for row in courses_for_case(case_id)],
        "documents": [document_response(row) for row in documents_for_case(case_id)],
        "eligibility_status": case.get("eligibility_status") or "pending",
        "eligibility_reason": case.get("eligibility_reason") or None,
        "total_duration_weeks": as_optional_int(case.get("total_duration_weeks")),
        "duration_breakdown": _load_breakdown(case.get("duration_breakdown_json")),
        "document_validity_status": case.get("document_validity_status") or "pending",
        "document_validity_reason": case.get("document_validity_reason") or None,
        "document_validity_breakdown": _load_breakdown(case.get("document_validity_breakdown_json")),
        "lodgement_date_status": case.get("lodgement_date_status") or "pending",
        "lodgement_date_reason": case.get("lodgement_date_reason") or None,
        "lodgement_date": case.get("lodgement_date") or None,
        "lodgement_basis": case.get("lodgement_basis") or None,
        "lodgement_breakdown": _load_breakdown(case.get("lodgement_breakdown_json")),
    }


def detail_response_from_full_creation(result: dict, owner_email: str | None) -> dict:
    """Like detail_response_from_fresh_courses, but for a case created via
    create_case_full() -- courses and case-level documents already come back
    with real ids from that one batched call, so this just reshapes them
    into the usual response, no extra Sheets reads needed.
    """
    case = result["caseRow"]
    case_id = as_int(case["id"])
    courses_out = [
        {
            "id": as_int(row["id"]),
            "name": row["name"],
            "course_type": row["course_type"],
            "start_date": row["start_date"] or None,
            "end_date": row["end_date"] or None,
            "cricos_weeks": as_optional_int(row["cricos_weeks"]),
            "cricos_code": row.get("cricos_code") or None,
            "sort_order": as_int(row["sort_order"], 0),
            "documents": [document_response(doc) for doc in row.get("documents", [])],
        }
        for row in result["courses"]
    ]
    return {
        "id": case_id,
        "student_name": case["student_name"],
        "stream": case["stream"],
        "status": case["status"],
        "created_at": case["created_at"],
        "owner_email": owner_email,
        "courses": courses_out,
        "documents": [document_response(doc) for doc in result.get("caseDocuments", [])],
    }


def detail_response_from_fresh_courses(case: dict, courses: list[dict]) -> dict:
    """Like detail_response, but builds the course list from courses the
    caller already has in hand (just-created or just-replaced, so they have
    no documents yet) instead of re-reading Courses/Documents from the sheet.
    """
    case_id = as_int(case["id"])
    owner = find_user_by_id(as_int(case["owner_user_id"]))
    courses_out = [
        {
            "id": as_int(row["id"]),
            "name": row["name"],
            "course_type": row["course_type"],
            "start_date": row["start_date"] or None,
            "end_date": row["end_date"] or None,
            "cricos_weeks": as_optional_int(row["cricos_weeks"]),
            "sort_order": as_int(row["sort_order"], 0),
            "documents": [],
        }
        for row in courses
    ]
    return {"id": case_id, "student_name": case["student_name"], "stream": case["stream"], "status": case["status"], "created_at": case["created_at"], "owner_email": owner.email if owner else None, "courses": courses_out}


def _completion_letter_fields(content: bytes, drive_file_id: str) -> dict:
    """Regex extraction against the PDF's own embedded text; if that comes up
    empty, it means the document is scanned/image-based with no text layer at
    all (not a pattern-matching gap -- there's nothing there to search), so
    this falls back to Drive's own OCR conversion and runs the exact same
    patterns against the text OCR recovers instead.
    """
    fields = extract_completion_letter_fields(content)
    if fields:
        return fields
    try:
        text = ocr_file(drive_file_id)
    except DriveServiceError:
        return {}
    return extract_completion_letter_fields_from_text(text)


def _coe_fields(content: bytes, drive_file_id: str) -> dict:
    """Same idea as _completion_letter_fields, for a CoE's CRICOS code."""
    fields = extract_coe_fields(content)
    if fields.get("cricos_code"):
        return fields
    try:
        text = ocr_file(drive_file_id)
    except DriveServiceError:
        return fields
    return extract_coe_fields_from_text(text)


def extract_and_store_course_fields(course: dict, doc_type: str, content: bytes, drive_file_id: str) -> None:
    """Best-effort: after a CoE or Completion Letter upload, extracts
    whatever that document type is responsible for (regex first, OCR
    fallback for a scanned document) and writes it onto the Course row.
    Never raises -- the document itself is already safely uploaded
    regardless of whether extraction finds anything; a miss just leaves the
    field for an admin to fill in by hand. A CoE also gets its CRICOS code
    looked up against the government registry right away, so the
    duration/CRICOS weeks are already sitting there by the time the
    combined calculation is run.
    """
    course_id = as_int(course["id"])
    if doc_type == DocType.completion_letter.value:
        fields = _completion_letter_fields(content, drive_file_id)
        if fields:
            try:
                update("Courses", course_id, fields)
            except StoreError:
                pass
    elif doc_type == DocType.coe.value:
        fields = _coe_fields(content, drive_file_id)
        cricos_code = fields.get("cricos_code")
        if not cricos_code:
            return
        updates = {"cricos_code": cricos_code}
        try:
            weeks = lookup_duration_weeks(cricos_code)
        except CricosLookupError:
            weeks = None
        if weeks is not None:
            updates["cricos_weeks"] = weeks
        try:
            update("Courses", course_id, updates)
        except StoreError:
            pass


def reextract_missing_course_fields(course: dict) -> dict:
    """Best-effort self-healing: if this course is still missing data that's
    supposed to come from an already-uploaded document (start/end date from
    a Completion Letter, or CRICOS code/weeks from a CoE), re-downloads that
    document's own content from Drive and re-runs extraction against it right
    now (regex first, OCR fallback for a scanned document -- see
    _completion_letter_fields/_coe_fields). This is what makes "Check
    Eligibility" pick up an extraction fix (or any other reason a field never
    got filled in) on an already-uploaded document, without the student ever
    needing to re-upload it. Never raises -- a re-extraction miss just leaves
    the course exactly as it was, same as the original upload-time
    extraction. The Completion Letter and CoE lookups below are independent
    of each other (different documents, different fields) so they run
    concurrently -- same downloads/OCR calls as before, just not forced to
    wait on each other one at a time.
    """
    course_id = as_int(course["id"])
    docs = documents_for_course(course_id)

    def completion_letter_updates() -> dict:
        if course.get("start_date") and course.get("end_date"):
            return {}
        doc = next((d for d in docs if d["doc_type"] == DocType.completion_letter.value), None)
        if not doc:
            return {}
        try:
            content = download_bytes(doc["s3_key"])
            fields = _completion_letter_fields(content, doc["drive_file_id"])
        except S3ServiceError:
            fields = {}
        return {key: fields[key] for key in ("start_date", "end_date") if fields.get(key) and not course.get(key)}

    def coe_updates() -> dict:
        if course.get("cricos_code") and course.get("cricos_weeks"):
            return {}
        doc = next((d for d in docs if d["doc_type"] == DocType.coe.value), None)
        if not doc:
            return {}
        try:
            content = download_bytes(doc["s3_key"])
            fields = _coe_fields(content, doc["drive_file_id"])
        except S3ServiceError:
            fields = {}
        cricos_code = fields.get("cricos_code")
        if not cricos_code:
            return {}
        result = {}
        if not course.get("cricos_code"):
            result["cricos_code"] = cricos_code
        if not course.get("cricos_weeks"):
            try:
                weeks = lookup_duration_weeks(cricos_code)
            except CricosLookupError:
                weeks = None
            if weeks is not None:
                result["cricos_weeks"] = weeks
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        updates: dict = {}
        for result in pool.map(lambda fn: fn(), (completion_letter_updates, coe_updates)):
            updates.update(result)

    if not updates:
        return course
    try:
        return update("Courses", course_id, updates)
    except StoreError:
        return course


def _case_date(case: dict, column: str) -> date_cls | None:
    value = case.get(column)
    return date_cls.fromisoformat(value) if value else None


# Maps each case-level (document validity) doc type to its (bytes extractor,
# text extractor, {result key: Case column}) -- the text extractor is the
# OCR fallback's target, same split as the qualification documents. AFP
# Certificate and AFP Receipt both write to the same afp_issue_date column
# since either one satisfies the requirement -- whichever is uploaded (or
# re-uploaded/replaced) is the one that counts.
CASE_FIELD_EXTRACTORS = {
    DocType.current_visa.value: (extract_current_visa_fields, extract_current_visa_fields_from_text, {"visa_subclass": "visa_subclass", "length_of_stay_date": "visa_length_of_stay_date"}),
    DocType.pte.value: (extract_pte_fields, extract_pte_fields_from_text, {"valid_until_date": "pte_valid_until_date"}),
    DocType.ovhc.value: (extract_ovhc_fields, extract_ovhc_fields_from_text, {"relevant_date": "ovhc_relevant_date"}),
    DocType.afp_certificate.value: (extract_afp_fields, extract_afp_fields_from_text, {"issue_date": "afp_issue_date"}),
    DocType.afp_receipt.value: (extract_afp_fields, extract_afp_fields_from_text, {"issue_date": "afp_issue_date"}),
    DocType.new_coe.value: (extract_new_coe_fields, extract_new_coe_fields_from_text, {"start_date": "new_coe_start_date"}),
}


def _case_doc_fields(doc_type: str, content: bytes, drive_file_id: str) -> dict:
    """Regex extraction against the document's own embedded text; if that
    comes up empty (a scanned/image-based document with no text layer at
    all), falls back to Drive's OCR conversion and runs the exact same
    patterns against the recovered text -- same approach as the qualification
    documents' _completion_letter_fields/_coe_fields.
    """
    entry = CASE_FIELD_EXTRACTORS.get(doc_type)
    if not entry:
        return {}
    extractor_bytes, extractor_text, _ = entry
    fields = extractor_bytes(content)
    if fields:
        return fields
    try:
        text = ocr_file(drive_file_id)
    except DriveServiceError:
        return {}
    return extractor_text(text)


def extract_and_store_case_fields(case_id: int, doc_type: str, content: bytes, drive_file_id: str) -> None:
    """Best-effort: after a Current Visa/PTE/OVHC/AFP upload, extracts
    whatever that document type is responsible for and writes it onto the
    Case row (these are one-per-case, not one-per-course, so they live
    directly on Cases rather than a per-document blob). Never raises -- same
    fire-and-forget pattern as the qualification documents.
    """
    entry = CASE_FIELD_EXTRACTORS.get(doc_type)
    if not entry:
        return
    _, _, field_map = entry
    fields = _case_doc_fields(doc_type, content, drive_file_id)
    if not fields:
        return
    updates = {column: fields[key] for key, column in field_map.items() if fields.get(key)}
    if not updates:
        return
    try:
        update("Cases", case_id, updates)
    except StoreError:
        pass


def reextract_missing_case_fields(case: dict) -> dict:
    """Best-effort self-healing: if this case is still missing data that's
    supposed to come from an already-uploaded document (visa subclass/length
    of stay, PTE valid-until, OVHC relevant date, AFP issue date),
    re-downloads that document's own content from Drive and re-runs
    extraction against it right now (regex first, OCR fallback for a scanned
    document). This is what makes "Check Document Validity" pick up an
    extraction fix (or any other reason a field never got filled in) on an
    already-uploaded document, without the student ever needing to re-upload
    it. Never raises -- a re-extraction miss just leaves the case exactly as
    it was, same as the original upload-time extraction. The five document
    checks below are independent of each other (different documents,
    different fields) so they run concurrently -- same downloads/OCR calls
    as before, just not forced to wait on each other one at a time.
    """
    case_id = as_int(case["id"])
    docs = documents_for_case(case_id)

    def doc_for(doc_type: str) -> dict | None:
        return next((d for d in docs if d["doc_type"] == doc_type), None)

    def visa_updates() -> dict:
        if case.get("visa_subclass") and case.get("visa_length_of_stay_date"):
            return {}
        doc = doc_for(DocType.current_visa.value)
        if not doc:
            return {}
        try:
            content = download_bytes(doc["s3_key"])
            fields = _case_doc_fields(DocType.current_visa.value, content, doc["drive_file_id"])
        except S3ServiceError:
            fields = {}
        result = {}
        if fields.get("visa_subclass") and not case.get("visa_subclass"):
            result["visa_subclass"] = fields["visa_subclass"]
        if fields.get("length_of_stay_date") and not case.get("visa_length_of_stay_date"):
            result["visa_length_of_stay_date"] = fields["length_of_stay_date"]
        return result

    def pte_updates() -> dict:
        if case.get("pte_valid_until_date"):
            return {}
        doc = doc_for(DocType.pte.value)
        if not doc:
            return {}
        try:
            content = download_bytes(doc["s3_key"])
            fields = _case_doc_fields(DocType.pte.value, content, doc["drive_file_id"])
        except S3ServiceError:
            fields = {}
        return {"pte_valid_until_date": fields["valid_until_date"]} if fields.get("valid_until_date") else {}

    def ovhc_updates() -> dict:
        if case.get("ovhc_relevant_date"):
            return {}
        doc = doc_for(DocType.ovhc.value)
        if not doc:
            return {}
        try:
            content = download_bytes(doc["s3_key"])
            fields = _case_doc_fields(DocType.ovhc.value, content, doc["drive_file_id"])
        except S3ServiceError:
            fields = {}
        return {"ovhc_relevant_date": fields["relevant_date"]} if fields.get("relevant_date") else {}

    def afp_updates() -> dict:
        if case.get("afp_issue_date"):
            return {}
        doc = doc_for(DocType.afp_certificate.value) or doc_for(DocType.afp_receipt.value)
        if not doc:
            return {}
        try:
            content = download_bytes(doc["s3_key"])
            fields = _case_doc_fields(doc["doc_type"], content, doc["drive_file_id"])
        except S3ServiceError:
            fields = {}
        return {"afp_issue_date": fields["issue_date"]} if fields.get("issue_date") else {}

    def new_coe_updates() -> dict:
        if case.get("new_coe_start_date"):
            return {}
        doc = doc_for(DocType.new_coe.value)
        if not doc:
            return {}
        try:
            content = download_bytes(doc["s3_key"])
            fields = _case_doc_fields(DocType.new_coe.value, content, doc["drive_file_id"])
        except S3ServiceError:
            fields = {}
        return {"new_coe_start_date": fields["start_date"]} if fields.get("start_date") else {}

    with ThreadPoolExecutor(max_workers=5) as pool:
        updates: dict = {}
        for result in pool.map(lambda fn: fn(), (visa_updates, pte_updates, ovhc_updates, afp_updates, new_coe_updates)):
            updates.update(result)

    if not updates:
        return case
    try:
        return update("Cases", case_id, updates)
    except StoreError:
        return case


def case_payload(payload: CaseCreate, owner_id: int) -> tuple[dict, list[dict]]:
    row = {"owner_user_id": str(owner_id), "student_name": payload.student_name, "stream": payload.stream.value, "status": "draft", "drive_folder_id": "", "created_at": now()}
    courses = [{"name": course.name, "course_type": course.course_type.value, "start_date": course.start_date.isoformat() if course.start_date else "", "end_date": course.end_date.isoformat() if course.end_date else "", "cricos_weeks": course.cricos_weeks if course.cricos_weeks is not None else "", "sort_order": course.sort_order or index} for index, course in enumerate(payload.courses)]
    return row, courses


def _iso_or_blank(value) -> str:
    return value.isoformat() if value else ""


def _document_manifest(documents: list) -> list[dict]:
    return [{"doc_type": doc.doc_type.value, "file_name": doc.file_name, "mime_type": doc.mime_type} for doc in documents]


def case_payload_full(payload: CaseCreateFull, owner_id: int) -> tuple[dict, list[dict], list[dict]]:
    """Like case_payload, but for create_case_full: every course already
    carries its dates/CRICOS code/weeks (found earlier by the frontend's
    extract-preview calls, before Save), plus each course's and the case's
    own document manifest (metadata only -- no file bytes; those upload
    separately afterward straight to S3, per document).
    """
    row = {
        "owner_user_id": str(owner_id),
        "student_name": payload.student_name,
        "stream": payload.stream.value,
        "status": "draft",
        "drive_folder_id": "",
        "created_at": now(),
        "visa_subclass": payload.visa_subclass or "",
        "visa_length_of_stay_date": _iso_or_blank(payload.visa_length_of_stay_date),
        "pte_valid_until_date": _iso_or_blank(payload.pte_valid_until_date),
        "ovhc_relevant_date": _iso_or_blank(payload.ovhc_relevant_date),
        "afp_issue_date": _iso_or_blank(payload.afp_issue_date),
        "new_coe_start_date": _iso_or_blank(payload.new_coe_start_date),
    }
    courses = [
        {
            "name": course.name,
            "course_type": course.course_type.value,
            "start_date": _iso_or_blank(course.start_date),
            "end_date": _iso_or_blank(course.end_date),
            "cricos_weeks": course.cricos_weeks if course.cricos_weeks is not None else "",
            "cricos_code": course.cricos_code or "",
            "sort_order": course.sort_order or index,
            "documents": _document_manifest(course.documents),
        }
        for index, course in enumerate(payload.courses)
    ]
    case_documents = _document_manifest(payload.case_documents)
    return row, courses, case_documents


def _extract_preview_fields(doc_type: DocType, content: bytes) -> dict:
    """Regex-only extraction, no OCR fallback -- there's no Drive file to OCR
    against yet at this stage, since nothing has been persisted anywhere
    (see extract_preview below). Same patterns a real upload always used;
    only which field(s) come back, and under which response key, changes per
    doc_type.
    """
    if doc_type == DocType.completion_letter:
        return extract_completion_letter_fields(content)
    if doc_type == DocType.coe:
        fields = extract_coe_fields(content)
        cricos_code = fields.get("cricos_code")
        if not cricos_code:
            return {}
        result = {"cricos_code": cricos_code}
        try:
            weeks = lookup_duration_weeks(cricos_code)
        except CricosLookupError:
            weeks = None
        if weeks is not None:
            result["cricos_weeks"] = weeks
        return result
    if doc_type == DocType.new_coe:
        fields = extract_new_coe_fields(content)
        return {"new_coe_start_date": fields["start_date"]} if fields.get("start_date") else {}
    if doc_type == DocType.current_visa:
        fields = extract_current_visa_fields(content)
        result = {}
        if fields.get("visa_subclass"):
            result["visa_subclass"] = fields["visa_subclass"]
        if fields.get("length_of_stay_date"):
            result["visa_length_of_stay_date"] = fields["length_of_stay_date"]
        return result
    if doc_type == DocType.pte:
        fields = extract_pte_fields(content)
        return {"pte_valid_until_date": fields["valid_until_date"]} if fields.get("valid_until_date") else {}
    if doc_type == DocType.ovhc:
        fields = extract_ovhc_fields(content)
        return {"ovhc_relevant_date": fields["relevant_date"]} if fields.get("relevant_date") else {}
    if doc_type in (DocType.afp_certificate, DocType.afp_receipt):
        fields = extract_afp_fields(content)
        return {"afp_issue_date": fields["issue_date"]} if fields.get("issue_date") else {}
    return {}  # transcript, academic_certificate: nothing auto-extracted, matches today


@router.post("/extract-preview", response_model=ExtractPreviewResponse)
async def extract_preview(doc_type: DocType = Form(...), file: UploadFile = File(...), current_user: User = Depends(get_current_user)):
    """Runs extraction against a document's bytes without saving anything --
    no S3 write, no Sheets write, no case/course needing to exist yet. Lets
    the frontend show a student what was found in a document the instant
    they attach it (well before Save), holding the result locally until
    create_case_full below actually persists it. Costs at most one external
    CRICOS-registry call; zero Apps Script calls.
    """
    if file.content_type not in ALLOWED_DOC_CONTENT_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only PDF, JPG, or PNG files are allowed")
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="File exceeds 15MB limit")
    return _extract_preview_fields(doc_type, content)


@router.post("/create-full", response_model=CaseDetailResponse, status_code=status.HTTP_201_CREATED)
def create_case_full_route(payload: CaseCreateFull, current_user: User = Depends(get_current_user)):
    """Creates the case, every course, and every document's metadata in one
    Apps Script call (create_case_full in sheet_store.py) -- replaces
    create_case_route below for the normal signup flow, now that courses and
    case-level fields already arrive pre-extracted (via extract_preview
    above), so nothing needs re-extracting or self-healing here. The actual
    file bytes still need to reach S3; the frontend uploads each one
    separately afterward via upload_document_content, using the document ids
    this returns.
    """
    if current_user.role != UserRole.student:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only students can create a case")
    try:
        row, courses, case_documents = case_payload_full(payload, current_user.id)
        result = create_case_full(row, courses, case_documents, now())
        return detail_response_from_full_creation(result, current_user.email)
    except StoreError as exc:
        if "DUPLICATE_CASE" in str(exc):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="You already have a case") from exc
        raise sheet_error(exc) from exc


@router.post("", response_model=CaseDetailResponse, status_code=status.HTTP_201_CREATED)
def create_case_route(payload: CaseCreate, current_user: User = Depends(get_current_user)):
    if current_user.role != UserRole.student:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only students can create a case")
    try:
        # create_case duplicate-checks (one case per owner) and inserts the
        # case plus all its courses in a single Apps Script round-trip.
        row, courses = case_payload(payload, current_user.id)
        case, created_courses = create_case(row, courses)
        return detail_response_from_fresh_courses(case, created_courses)
    except StoreError as exc:
        # Apps Script reports this as the JS error's String() form ("Error:
        # DUPLICATE_CASE"), not the bare code, so an exact match here never
        # fires -- every duplicate-case attempt was falling through to the
        # generic 502 "Google Sheets is unavailable" instead of a real 409.
        if "DUPLICATE_CASE" in str(exc):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="You already have a case") from exc
        raise sheet_error(exc) from exc


@router.get("", response_model=list[CaseSummaryResponse])
def list_cases(current_user: User = Depends(get_current_user)):
    try:
        all_cases = case_rows()
        if current_user.role != UserRole.admin:
            all_cases = [item for item in all_cases if as_int(item["owner_user_id"]) == current_user.id]
        return [{"id": as_int(item["id"]), "student_name": item["student_name"], "stream": item["stream"], "status": item["status"], "created_at": item["created_at"], "course_count": len(courses_for_case(as_int(item["id"]))), "owner_email": (find_user_by_id(as_int(item["owner_user_id"])).email if find_user_by_id(as_int(item["owner_user_id"])) else None), "eligibility_status": item.get("eligibility_status") or "pending"} for item in reversed(all_cases)]
    except StoreError as exc:
        raise sheet_error(exc) from exc


@router.get("/{case_id}", response_model=CaseDetailResponse)
def get_case(case_id: int, current_user: User = Depends(get_current_user)):
    try:
        return detail_response(scoped_case(case_id, current_user))
    except StoreError as exc:
        raise sheet_error(exc) from exc


def _iso(value: date_cls | None) -> str | None:
    return value.isoformat() if value else None


def _stage1_block_message(eligibility_status: str) -> tuple[str, str]:
    # A confirmed "not_eligible" upstream is a hard stop, not just a
    # not-yet-known state -- say so plainly instead of the vaguer "pending"
    # wording, which implies this might resolve on its own once more data
    # shows up (true for a genuinely pending qualification check, but
    # misleading here).
    if eligibility_status == "not_eligible":
        return "not_eligible", "The qualification check is not eligible, so this cannot be checked."
    return "pending", "Qualification check must show eligible before document validity can be checked."


def _stage3_block_message(eligibility_status: str, document_validity_status: str) -> tuple[str, str]:
    # Same idea as _stage1_block_message, but a lodgement date can be
    # blocked by either (or both) of the two earlier checks at once, so the
    # message names every blocker rather than just one.
    not_eligible_blockers = []
    pending_blockers = []
    if eligibility_status == "not_eligible":
        not_eligible_blockers.append("the qualification check")
    elif eligibility_status != "eligible":
        pending_blockers.append("the qualification check")
    if document_validity_status == "not_eligible":
        not_eligible_blockers.append("the document validity check")
    elif document_validity_status != "eligible":
        pending_blockers.append("the document validity check")
    if not_eligible_blockers:
        verb = "is" if len(not_eligible_blockers) == 1 else "are"
        return "not_eligible", f"{' and '.join(not_eligible_blockers).capitalize()} {verb} not eligible, so a lodgement date cannot be calculated."
    return "pending", f"{' and '.join(pending_blockers).capitalize()} must show eligible before a lodgement date can be calculated."


@router.post("/{case_id}/run-checks", response_model=CaseDetailResponse)
def run_checks(case_id: int, current_user: User = Depends(get_current_user)):
    """Runs all three cascading checks -- qualification/CRICOS duration,
    document validity, then the lodgement date -- in a single request,
    short-circuiting exactly as before (document validity only runs once
    qualification is eligible; lodgement date only once both are). This
    replaces three separate endpoints (calculate/validate-documents/
    calculate-lodgement-date) that the frontend used to call one after
    another: each one independently re-read the Cases row over a fresh Apps
    Script round trip (2.5-4.5s no matter how little work it does -- see
    sheet_store.py) just to learn a status the previous call had *just*
    written a moment earlier. Here, a later stage reads the value the
    earlier stage computed in this same request instead, so the only Sheets
    round trips left are the ones that read or write genuinely new data --
    one Cases read, one Courses read, one Documents read, and one final
    combined Cases write, regardless of which stage the case stops at.

    Those three reads are also independent of each other -- none needs
    another's result -- so they're fetched together via rows_multi in one
    Apps Script round trip rather than one call per table. Each call this
    backend makes to Apps Script is its own separate execution there, so
    this also means one execution against Apps Script's own rate/concurrency
    limits instead of three, not just a wall-clock saving. It warms the
    process-wide table cache (see sheet_store.py's TABLE_CACHE_TTL_SECONDS),
    so scoped_case/courses_for_case/documents_for_case below -- and every
    later per-course Documents lookup inside reextract_missing_course_fields/
    reextract_missing_case_fields -- read from memory instead of firing their
    own requests.
    """
    try:
        rows_multi(["Cases", "Courses", "Documents"])
        case = scoped_case(case_id, current_user)
        course_rows = courses_for_case(case_id)
        case_docs = documents_for_case(case_id)

        # ---- Stage 1: qualification / CRICOS duration ----
        with ThreadPoolExecutor(max_workers=max(len(course_rows), 1)) as pool:
            fresh_course_rows = list(pool.map(reextract_missing_course_fields, course_rows))
        courses = [
            CourseInput(
                id=as_int(row["id"]),
                name=row["name"],
                course_type=row["course_type"],
                start_date=date_cls.fromisoformat(row["start_date"]) if row["start_date"] else None,
                end_date=date_cls.fromisoformat(row["end_date"]) if row["end_date"] else None,
                cricos_weeks=as_optional_int(row["cricos_weeks"]),
            )
            for row in fresh_course_rows
        ]
        stage1 = calculate_duration(case["stream"], courses)
        updates = {
            "eligibility_status": stage1.status,
            "eligibility_reason": stage1.reason or "",
            "total_duration_weeks": stage1.total_weeks,
            "duration_breakdown_json": json.dumps({
                "groups": [
                    {
                        "label": group.label,
                        "actual_weeks": group.actual_weeks,
                        "required_weeks": group.required_weeks,
                        "credited_weeks": group.credited_weeks,
                    }
                    for group in stage1.groups
                ],
                "total_weeks": stage1.total_weeks,
                "min_required_weeks": MIN_TOTAL_WEEKS,
            }),
        }

        if stage1.status != "eligible":
            doc_validity_status, doc_validity_reason = _stage1_block_message(stage1.status)
            lodgement_status, lodgement_reason = _stage3_block_message(stage1.status, doc_validity_status)
            updates.update({
                "document_validity_status": doc_validity_status,
                "document_validity_reason": doc_validity_reason,
                "document_validity_breakdown_json": "",
                "lodgement_date_status": lodgement_status,
                "lodgement_date_reason": lodgement_reason,
                "lodgement_date": "",
                "lodgement_basis": "",
                "lodgement_breakdown_json": "",
            })
            updated = update("Cases", case_id, updates)
            return detail_response(updated)

        # ---- Stage 2: document validity (current visa/PTE/OVHC/AFP) ----
        case = reextract_missing_case_fields(case)
        # case_docs was already fetched in parallel with Cases/Courses above
        # -- reused as-is here (it can't have changed: reextract_missing_case_
        # fields only ever writes Case-row fields, never the Documents table).
        case_doc_types = {item["doc_type"] for item in case_docs}
        stage1_input = Stage1Input(
            has_current_visa=DocType.current_visa.value in case_doc_types,
            has_pte=DocType.pte.value in case_doc_types,
            has_ovhc=DocType.ovhc.value in case_doc_types,
            has_afp=(DocType.afp_certificate.value in case_doc_types) or (DocType.afp_receipt.value in case_doc_types),
            has_afp_receipt=DocType.afp_receipt.value in case_doc_types,
            visa_subclass=case.get("visa_subclass") or None,
            visa_length_of_stay_date=_case_date(case, "visa_length_of_stay_date"),
            pte_valid_until_date=_case_date(case, "pte_valid_until_date"),
            ovhc_relevant_date=_case_date(case, "ovhc_relevant_date"),
            afp_issue_date=_case_date(case, "afp_issue_date"),
        )
        stage2 = check_stage1(stage1_input, date_cls.today())
        updates.update({
            "document_validity_status": stage2.status,
            "document_validity_reason": stage2.reason or "",
            "document_validity_breakdown_json": json.dumps({
                "checks": [
                    {
                        "label": "Current Visa",
                        "extracted": {k: v for k, v in {"subclass": stage1_input.visa_subclass, "length_of_stay_date": _iso(stage1_input.visa_length_of_stay_date)}.items() if v},
                        "rule": "Must be subclass 500, and its length-of-stay date must not have already passed",
                    },
                    {
                        "label": "PTE",
                        "extracted": {k: v for k, v in {"valid_until_date": _iso(stage1_input.pte_valid_until_date)}.items() if v},
                        "rule": "Valid-until date must not have already passed",
                    },
                    {
                        "label": "OVHC",
                        "extracted": {k: v for k, v in {"relevant_date": _iso(stage1_input.ovhc_relevant_date)}.items() if v},
                        "rule": "Policy start (or issue) date must already be before today",
                    },
                    {
                        "label": "AFP (Certificate or Receipt)",
                        "extracted": {k: v for k, v in {"issue_date": _iso(stage1_input.afp_issue_date)}.items() if v},
                        "rule": "Receipt: presence alone is sufficient, no date check. Certificate: issue date must already be today or earlier",
                    },
                ],
            }),
        })

        if stage2.status != "eligible":
            lodgement_status, lodgement_reason = _stage3_block_message(stage1.status, stage2.status)
            updates.update({
                "lodgement_date_status": lodgement_status,
                "lodgement_date_reason": lodgement_reason,
                "lodgement_date": "",
                "lodgement_basis": "",
                "lodgement_breakdown_json": "",
            })
            updated = update("Cases", case_id, updates)
            return detail_response(updated)

        # ---- Stage 3: lodgement date ----
        # Reuses fresh_course_rows from stage 1 rather than re-reading
        # Courses -- it already reflects any self-healing write stage 1 just
        # made, which a fresh read would too, but at the cost of a Sheets
        # round trip for data we already have in hand.
        completion_dates = [date_cls.fromisoformat(row["end_date"]) for row in fresh_course_rows if row.get("end_date")]
        latest_completion_date = max(completion_dates) if completion_dates else None
        stage3_input = Stage3Input(
            latest_completion_date=latest_completion_date,
            visa_length_of_stay_date=_case_date(case, "visa_length_of_stay_date"),
            new_coe_start_date=_case_date(case, "new_coe_start_date"),
        )
        stage3 = check_stage3(stage3_input, date_cls.today())
        updates.update({
            "lodgement_date_status": stage3.status,
            "lodgement_date_reason": stage3.reason or "",
            "lodgement_date": stage3.lodgement_date.isoformat() if stage3.lodgement_date else "",
            "lodgement_basis": stage3.lodgement_basis or "",
            "lodgement_breakdown_json": json.dumps({
                "latest_completion_date": _iso(latest_completion_date),
                "window_end": _iso(stage3.window_end),
                "factors": [{"label": f.label, "date": _iso(f.date), "included": f.included} for f in stage3.factors],
            }),
        })
        updated = update("Cases", case_id, updates)
        return detail_response(updated)
    except StoreError as exc:
        raise sheet_error(exc) from exc


@router.post("/{case_id}/review", response_model=CaseDetailResponse)
def mark_case_reviewed(case_id: int, current_user: User = Depends(get_current_user)):
    if current_user.role != UserRole.admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admins can mark a case as reviewed")
    case = scoped_case(case_id, current_user)
    try:
        updated = update("Cases", case_id, {**case, "status": "reviewed"})
        return detail_response(updated)
    except StoreError as exc:
        raise sheet_error(exc) from exc


@router.put("/{case_id}/replace", response_model=CaseDetailResponse)
def replace_case_qualifications(case_id: int, payload: CaseCreate, current_user: User = Depends(get_current_user)):
    if current_user.role != UserRole.student:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only students can change their case")
    current = scoped_case(case_id, current_user)
    try:
        _, courses = case_payload(payload, current_user.id)
        case_row = {"owner_user_id": str(current_user.id), "student_name": payload.student_name, "stream": payload.stream.value, "status": current["status"], "drive_folder_id": current["drive_folder_id"], "created_at": current["created_at"]}
        case, replaced_courses = replace_case(case_id, case_row, courses)
        return detail_response_from_fresh_courses(case, replaced_courses)
    except StoreError as exc:
        raise sheet_error(exc) from exc


@router.post("/{case_id}/courses", response_model=CourseResponse, status_code=status.HTTP_201_CREATED)
def add_course(case_id: int, payload: CourseCreate, current_user: User = Depends(get_current_user)):
    scoped_case(case_id, current_user)
    try:
        next_order = len(courses_for_case(case_id))
        row = insert("Courses", {"case_id": str(case_id), "name": payload.name, "course_type": payload.course_type.value, "start_date": payload.start_date.isoformat() if payload.start_date else "", "end_date": payload.end_date.isoformat() if payload.end_date else "", "cricos_weeks": payload.cricos_weeks if payload.cricos_weeks is not None else "", "sort_order": payload.sort_order or next_order})
        return course_response(row)
    except StoreError as exc:
        raise sheet_error(exc) from exc


@router.put("/{case_id}/documents/{document_id}/content", status_code=status.HTTP_204_NO_CONTENT)
async def upload_document_content(case_id: int, document_id: int, file: UploadFile = File(...), current_user: User = Depends(get_current_user)):
    """Uploads a document's actual bytes to S3, using the s3_key/mime_type
    already recorded on its Documents row -- created moments earlier by
    create_case_full_route, which knows nothing about file bytes at all, only
    metadata. Pure S3 call, no Apps Script involved -- unlike the old
    per-document upload endpoints below, firing many of these at once costs
    nothing against Apps Script's global write-lock queue.
    """
    scoped_case(case_id, current_user)
    try:
        all_docs = documents_for_case(case_id) + [
            doc for course in courses_for_case(case_id) for doc in documents_for_course(as_int(course["id"]))
        ]
    except StoreError as exc:
        raise sheet_error(exc) from exc
    document = next((item for item in all_docs if as_int(item["id"]) == document_id), None)
    if not document:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="File exceeds 15MB limit")
    try:
        upload_bytes(document["s3_key"], content, document["mime_type"])
    except S3ServiceError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.post("/{case_id}/courses/{course_id}/documents", response_model=DocumentResponse, status_code=status.HTTP_201_CREATED)
async def upload_document(case_id: int, course_id: int, doc_type: DocType = Form(...), file: UploadFile = File(...), current_user: User = Depends(get_current_user)):
    if doc_type in CASE_DOC_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{doc_type.value} is a case document, not a qualification document")
    case = scoped_case(case_id, current_user)
    course = course_for_case(case_id, course_id, current_user)
    if file.content_type not in ALLOWED_DOC_CONTENT_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only PDF, JPG, or PNG files are allowed")
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="File exceeds 15MB limit")
    file_name = f"{course['name']} - {DOCUMENT_FILE_LABELS[doc_type]}{CONTENT_TYPE_EXTENSIONS[file.content_type]}"
    s3_key = f"485_docs/{case['student_name']}-{case_id}/{file_name}"
    try:

        upload_bytes(s3_key, content, file.content_type)
        document = insert_document_record(
            doc_type=doc_type.value,
            file_name=file_name,
            mime_type=file.content_type,
            s3_key=s3_key,
            uploaded_at=now(),
            course_id=course_id,
        )
        extract_and_store_course_fields(course, doc_type.value, content, drive_file_id="")
        return document_response(document)
    except (StoreError, S3ServiceError) as exc:
        # Same "Error: CODE" vs. bare-code mismatch as DUPLICATE_CASE/
        # DUPLICATE_EMAIL elsewhere -- an exact match here never fires.
        if "DUPLICATE_DOCUMENT" in str(exc):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This document is already saved. Only pending documents can be uploaded.") from exc
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.post("/{case_id}/documents", response_model=DocumentResponse, status_code=status.HTTP_201_CREATED)
async def upload_case_level_document(case_id: int, doc_type: DocType = Form(...), file: UploadFile = File(...), current_user: User = Depends(get_current_user)):
    if doc_type not in CASE_DOC_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{doc_type.value} is a qualification document, not a case document")
    case = scoped_case(case_id, current_user)
    if file.content_type not in ALLOWED_DOC_CONTENT_TYPES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only PDF, JPG, or PNG files are allowed")
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="File exceeds 15MB limit")
    file_name = f"{DOCUMENT_FILE_LABELS[doc_type]}{CONTENT_TYPE_EXTENSIONS[file.content_type]}"
    s3_key = f"485_docs/{case['student_name']}-{case_id}/{file_name}"
    try:
        upload_bytes(s3_key, content, file.content_type)
        document = insert_document_record(
            doc_type=doc_type.value,
            file_name=file_name,
            mime_type=file.content_type,
            s3_key=s3_key,
            uploaded_at=now(),
            case_id=case_id,
        )
        extract_and_store_case_fields(case_id, doc_type.value, content, drive_file_id="")
        return document_response(document)
    except (StoreError, S3ServiceError) as exc:
        # Same "Error: CODE" vs. bare-code mismatch as DUPLICATE_CASE/
        # DUPLICATE_EMAIL elsewhere -- an exact match here never fires.
        if "DUPLICATE_DOCUMENT" in str(exc):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This document is already saved. Only pending documents can be uploaded.") from exc
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.delete("/{case_id}/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_case_level_document(case_id: int, document_id: int, current_user: User = Depends(get_current_user)):
    scoped_case(case_id, current_user)
    try:
        document = next((item for item in documents_for_case(case_id) if as_int(item["id"]) == document_id), None)
        if not document:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
        delete("Documents", document_id)
    except StoreError as exc:
        raise sheet_error(exc) from exc


@router.delete("/{case_id}/courses/{course_id}/documents/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_document(case_id: int, course_id: int, document_id: int, current_user: User = Depends(get_current_user)):
    course_for_case(case_id, course_id, current_user)
    try:
        document = next((item for item in documents_for_course(course_id) if as_int(item["id"]) == document_id), None)
        if not document:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
        delete("Documents", document_id)
    except StoreError as exc:
        raise sheet_error(exc) from exc
