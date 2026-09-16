from datetime import date, datetime

from pydantic import BaseModel, EmailStr, Field

from app.models import CourseType, DocType, Stream, UserRole

# ---------- Auth ----------

class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: str = Field(min_length=1, max_length=255)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class MeResponse(BaseModel):
    id: int
    email: str
    full_name: str
    role: UserRole

    class Config:
        from_attributes = True


# ---------- Documents ----------

class DocumentResponse(BaseModel):
    id: int
    doc_type: DocType
    file_name: str
    drive_view_link: str
    uploaded_at: datetime
    # Lets the frontend upload a document's bytes (PUT .../content) by
    # passing these straight back, instead of the endpoint needing its own
    # Sheets reads to look them up -- see create_case_full_route/
    # upload_document_content in cases_router.py. Not sensitive: knowing a
    # key within a private bucket grants no access without the app's own
    # credentials, and the naming convention already embeds the case id.
    s3_key: str | None = None
    mime_type: str | None = None

    class Config:
        from_attributes = True


# ---------- Courses ----------

class CourseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    course_type: CourseType
    start_date: date | None = None
    end_date: date | None = None
    cricos_weeks: int | None = Field(default=None, ge=0)
    sort_order: int = 0


class CourseResponse(BaseModel):
    id: int
    name: str
    course_type: CourseType
    start_date: date | None
    end_date: date | None
    cricos_weeks: int | None
    cricos_code: str | None = None
    sort_order: int
    documents: list[DocumentResponse] = []

    class Config:
        from_attributes = True


# ---------- Duration/CRICOS calculation breakdown ----------
# Set by POST /cases/{id}/calculate and persisted (as JSON) alongside the
# result, so it also comes back from a plain GET on a later page visit
# without recomputing anything -- letting a student or admin manually
# re-check the result against the same figures the calculation used: which
# document dates were extracted, and how each qualification group's credited
# weeks were derived.

class DurationGroupBreakdown(BaseModel):
    label: str
    actual_weeks: int
    required_weeks: int | None  # None if the CRICOS registry had no duration for this course -- credited_weeks then falls back to actual_weeks instead
    credited_weeks: int


class DurationBreakdown(BaseModel):
    groups: list[DurationGroupBreakdown]
    total_weeks: int
    min_required_weeks: int


# ---------- Document validity breakdown ----------
# Set by POST /cases/{id}/validate-documents and persisted (as JSON) the same
# way as duration_breakdown above -- a completely separate check from the
# duration/CRICOS one, with its own status/reason and its own breakdown. Not
# combined into one verdict.

class DocumentCheckBreakdown(BaseModel):
    label: str
    extracted: dict[str, str]  # whatever fields this document type extracts, e.g. {"subclass": "500", "length_of_stay_date": "2027-12-28"}
    rule: str  # plain-language statement of the pass/fail rule for this document


class DocumentValidityBreakdown(BaseModel):
    checks: list[DocumentCheckBreakdown]


# ---------- Lodgement date breakdown ----------
# Set by POST /cases/{id}/calculate-lodgement-date and persisted (as JSON)
# the same way as the other two breakdowns -- a third independent check. It
# refuses to run unless both the qualification check and the document
# validity check are already "eligible" (see the router).
# Shown to students for now (for manual verification during this build),
# same as the other two -- the original spec's admin-only restriction on
# the lodgement date is intentionally not yet applied.

class LodgementFactorBreakdown(BaseModel):
    label: str
    date: str | None
    included: bool  # False for a factor that was excluded (e.g. an invalid New CoE)


class LodgementBreakdown(BaseModel):
    latest_completion_date: str | None
    window_end: str | None  # completion date + 6 months
    factors: list[LodgementFactorBreakdown]


# ---------- Extraction preview ----------
# Returned by POST /cases/extract-preview -- runs the same regex/CRICOS-
# lookup extraction a document upload always has, but against bytes that
# haven't been saved anywhere (no S3 write, no Sheets write). Used so the
# frontend can show a student what was found in a document the moment they
# attach it, well before Save, and hold the result locally until then.

class ExtractPreviewResponse(BaseModel):
    start_date: date | None = None
    end_date: date | None = None
    cricos_code: str | None = None
    cricos_weeks: int | None = None
    visa_subclass: str | None = None
    visa_length_of_stay_date: date | None = None
    pte_valid_until_date: date | None = None
    ovhc_relevant_date: date | None = None
    afp_issue_date: date | None = None
    new_coe_start_date: date | None = None


# ---------- Cases ----------

class CaseCreate(BaseModel):
    student_name: str = Field(min_length=1, max_length=255)
    stream: Stream
    courses: list[CourseCreate] = []


# ---------- Batched case creation (documents already extracted client-side
# via ExtractPreviewResponse, before Save) ----------
# One request creates the case, every course (with its dates/CRICOS already
# known), and every document's metadata (no file bytes here -- those upload
# separately afterward, straight to S3, using the s3_key each created
# DocumentResponse comes back with). See create_case_full() in
# cases_router.py.

class DocumentManifestEntry(BaseModel):
    doc_type: DocType
    file_name: str = Field(min_length=1, max_length=500)
    mime_type: str


class CourseCreateFull(CourseCreate):
    cricos_code: str | None = None
    documents: list[DocumentManifestEntry] = []


class CaseCreateFull(BaseModel):
    student_name: str = Field(min_length=1, max_length=255)
    stream: Stream
    courses: list[CourseCreateFull] = []
    case_documents: list[DocumentManifestEntry] = []
    visa_subclass: str | None = None
    visa_length_of_stay_date: date | None = None
    pte_valid_until_date: date | None = None
    ovhc_relevant_date: date | None = None
    afp_issue_date: date | None = None
    new_coe_start_date: date | None = None


class CaseSummaryResponse(BaseModel):
    id: int
    student_name: str
    stream: Stream
    status: str
    created_at: datetime
    course_count: int
    owner_email: str | None = None
    eligibility_status: str = "pending"

    class Config:
        from_attributes = True


class CaseDetailResponse(BaseModel):
    id: int
    student_name: str
    stream: Stream
    status: str
    created_at: datetime
    owner_email: str | None = None
    courses: list[CourseResponse] = []
    documents: list[DocumentResponse] = []
    eligibility_status: str = "pending"
    eligibility_reason: str | None = None
    total_duration_weeks: int | None = None
    duration_breakdown: DurationBreakdown | None = None
    document_validity_status: str = "pending"
    document_validity_reason: str | None = None
    document_validity_breakdown: DocumentValidityBreakdown | None = None
    lodgement_date_status: str = "pending"
    lodgement_date_reason: str | None = None
    lodgement_date: date | None = None
    lodgement_basis: str | None = None
    lodgement_breakdown: LodgementBreakdown | None = None

    class Config:
        from_attributes = True
