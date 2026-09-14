import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date as date_cls

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from app.auth import get_current_user
from app.cricos_lookup import CricosLookupError, lookup_duration_weeks
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
from app.drive_service import DriveServiceError, ocr_file
from app.eligibility import MIN_TOTAL_WEEKS, CourseInput, Stage1Input, Stage3Input, calculate_duration, check_stage1, check_stage3
from app.models import CASE_DOC_TYPES, DocType, User, UserRole
from app.s3_service import S3ServiceError, download_bytes, upload_bytes
from app.schemas import CaseCreate, CaseDetailResponse, CaseSummaryResponse, CourseCreate, CourseResponse, DocumentResponse
from app.sheet_store import StoreError, as_int, as_optional_int, case_by_id, case_rows, courses_for_case, create_case, delete, documents_for_case, documents_for_course, find_user_by_id, insert, insert_document_record, now, replace_case, update

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


@router.post("/{case_id}/calculate", response_model=CaseDetailResponse)
def calculate_case(case_id: int, current_user: User = Depends(get_current_user)):
    """Runs the qualification duration/CRICOS check and stores the result.
    Re-running always recalculates fresh rather than only computing once --
    and before doing so, re-extracts any course field that's still missing
    from its already-uploaded document (see reextract_missing_course_fields),
    so an extraction fix (or any other cause of a gap) gets picked up on the
    next check without the student re-uploading anything. This only costs
    extra Drive round-trips for a course that's still actually missing data;
    a course that already has everything is untouched.
    """
    case = scoped_case(case_id, current_user)
    try:
        course_rows = courses_for_case(case_id)
        with ThreadPoolExecutor(max_workers=max(len(course_rows), 1)) as pool:
            rows = list(pool.map(reextract_missing_course_fields, course_rows))
        courses = [
            CourseInput(
                id=as_int(row["id"]),
                name=row["name"],
                course_type=row["course_type"],
                start_date=date_cls.fromisoformat(row["start_date"]) if row["start_date"] else None,
                end_date=date_cls.fromisoformat(row["end_date"]) if row["end_date"] else None,
                cricos_weeks=as_optional_int(row["cricos_weeks"]),
            )
            for row in rows
        ]
        result = calculate_duration(case["stream"], courses)
        breakdown = {
            "groups": [
                {
                    "label": group.label,
                    "actual_weeks": group.actual_weeks,
                    "required_weeks": group.required_weeks,
                    "credited_weeks": group.credited_weeks,
                }
                for group in result.groups
            ],
            "total_weeks": result.total_weeks,
            "min_required_weeks": MIN_TOTAL_WEEKS,
        }
        updates = {
            "eligibility_status": result.status,
            "eligibility_reason": result.reason or "",
            "total_duration_weeks": result.total_weeks,
            # Persisted (not just returned) so the exact figures behind this
            # result -- the ones a student/admin verifies the math against --
            # are still there on the next page load, without recomputing.
            "duration_breakdown_json": json.dumps(breakdown),
        }
        updated = update("Cases", case_id, updates)
        return detail_response(updated)
    except StoreError as exc:
        raise sheet_error(exc) from exc


def _iso(value: date_cls | None) -> str | None:
    return value.isoformat() if value else None


@router.post("/{case_id}/validate-documents", response_model=CaseDetailResponse)
def validate_documents(case_id: int, current_user: User = Depends(get_current_user)):
    """Runs the document validity check (current visa/PTE/OVHC/AFP) and
    stores the result. Confirmed rule: this only runs once the qualification
    check is already "eligible" (read from the already-stored result, never
    recomputed) -- checking document validity for someone who doesn't even
    qualify on their study duration is meaningless. Re-running always
    recalculates fresh, and before doing so, re-extracts any field still
    missing from its already-uploaded document (see
    reextract_missing_case_fields), same self-healing pattern as the
    qualification check.
    """
    case = scoped_case(case_id, current_user)
    try:
        eligibility_status = case.get("eligibility_status") or "pending"
        if eligibility_status != "eligible":
            # A confirmed "not_eligible" upstream is a hard stop, not just a
            # not-yet-known state -- say so plainly instead of the vaguer
            # "pending" wording, which implies this might resolve on its own
            # once more data shows up (true for a genuinely pending
            # qualification check, but misleading here).
            if eligibility_status == "not_eligible":
                blocked_status, blocked_reason = "not_eligible", "The qualification check is not eligible, so this cannot be checked."
            else:
                blocked_status, blocked_reason = "pending", "Qualification check must show eligible before document validity can be checked."
            updated = update("Cases", case_id, {
                "document_validity_status": blocked_status,
                "document_validity_reason": blocked_reason,
                "document_validity_breakdown_json": "",
            })
            return detail_response(updated)

        case = reextract_missing_case_fields(case)
        case_docs = documents_for_case(case_id)
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
        result = check_stage1(stage1_input, date_cls.today())
        breakdown = {
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
        }
        updates = {
            "document_validity_status": result.status,
            "document_validity_reason": result.reason or "",
            # Persisted for the same reason as duration_breakdown_json above.
            "document_validity_breakdown_json": json.dumps(breakdown),
        }
        updated = update("Cases", case_id, updates)
        return detail_response(updated)
    except StoreError as exc:
        raise sheet_error(exc) from exc


@router.post("/{case_id}/calculate-lodgement-date", response_model=CaseDetailResponse)
def calculate_lodgement_date(case_id: int, current_user: User = Depends(get_current_user)):
    """Runs the lodgement date calculation -- a third check with its own
    status/reason/breakdown, not combined with the other two into a single
    verdict. Confirmed rule: it refuses to run unless BOTH the qualification
    check and the document validity check are already "eligible" (read from
    their already-stored results, never recomputed) -- a lodgement date is
    meaningless for someone who doesn't qualify, or whose visa/PTE/OVHC/AFP
    aren't currently valid. Re-running always recalculates fresh, and before
    doing so, re-extracts a still-missing New CoE start date from an
    already-uploaded document (self-healing, same pattern as the other two
    checks).
    """
    case = scoped_case(case_id, current_user)
    try:
        eligibility_status = case.get("eligibility_status") or "pending"
        document_validity_status = case.get("document_validity_status") or "pending"
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
        if not_eligible_blockers or pending_blockers:
            # A confirmed "not_eligible" upstream is a hard stop -- say so
            # plainly rather than the vaguer "pending" wording, which implies
            # this might resolve on its own once more data shows up (true for
            # a genuinely pending check, but misleading here). If any
            # blocker is a hard failure, that takes priority in the message
            # even if another blocker is merely still pending.
            if not_eligible_blockers:
                blocked_status = "not_eligible"
                verb = "is" if len(not_eligible_blockers) == 1 else "are"
                blocked_reason = f"{' and '.join(not_eligible_blockers).capitalize()} {verb} not eligible, so a lodgement date cannot be calculated."
            else:
                blocked_status = "pending"
                blocked_reason = f"{' and '.join(pending_blockers).capitalize()} must show eligible before a lodgement date can be calculated."
            updated = update("Cases", case_id, {
                "lodgement_date_status": blocked_status,
                "lodgement_date_reason": blocked_reason,
                "lodgement_date": "",
                "lodgement_basis": "",
                "lodgement_breakdown_json": "",
            })
            return detail_response(updated)

        case = reextract_missing_case_fields(case)
        rows = courses_for_case(case_id)
        completion_dates = [date_cls.fromisoformat(row["end_date"]) for row in rows if row.get("end_date")]
        latest_completion_date = max(completion_dates) if completion_dates else None

        stage3_input = Stage3Input(
            latest_completion_date=latest_completion_date,
            visa_length_of_stay_date=_case_date(case, "visa_length_of_stay_date"),
            new_coe_start_date=_case_date(case, "new_coe_start_date"),
        )
        result = check_stage3(stage3_input, date_cls.today())
        breakdown = {
            "latest_completion_date": _iso(latest_completion_date),
            "window_end": _iso(result.window_end),
            "factors": [{"label": f.label, "date": _iso(f.date), "included": f.included} for f in result.factors],
        }
        updates = {
            "lodgement_date_status": result.status,
            "lodgement_date_reason": result.reason or "",
            "lodgement_date": result.lodgement_date.isoformat() if result.lodgement_date else "",
            "lodgement_basis": result.lodgement_basis or "",
            # Persisted for the same reason as duration_breakdown_json above.
            "lodgement_breakdown_json": json.dumps(breakdown),
        }
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
        # S3 is the fast, synchronous primary store -- Drive sync happens
        # later, out-of-band, via drive_sync_worker.py (see
        # s3-drive-sync-plan.md). insert_document_record still
        # duplicate-checks under one lock, it just never touches Drive.
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
