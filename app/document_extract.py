"""Regex-based field extraction from Completion Letter and CoE PDFs.

No OCR/AI involved: PDF text is pulled out directly (works when the PDF has
real embedded text, which is the normal case for institution-issued letters
-- a scanned/photographed document with no embedded text just yields nothing
to search, same as any other extraction miss). Every label/phrasing pattern
here was built directly from real sample documents, not guessed -- this is a
closed set that covers what's been seen so far, and a genuinely new label
wording will simply not match until a pattern is added for it.
"""

import io
import re
from datetime import date

import pytesseract
from pypdf import PdfReader

from app.config import settings

if settings.tesseract_cmd:
    pytesseract.pytesseract.tesseract_cmd = settings.tesseract_cmd

MONTH_NAMES = "jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec"
MONTH_FULL = (
    "january|february|march|april|may|june|july|august|september|october|november|december"
)
MONTH_WORD = "(?:" + MONTH_FULL + "|" + MONTH_NAMES + ")"
MONTH_INDEX = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# One pattern per date "shape" seen across the sample documents:
#   18/11/2024, 3/2/2025            -> D/M/YYYY or DD/MM/YYYY
#   13 Jun 2022, 14th August 2026   -> D Month YYYY, with an optional ordinal suffix
#   21-Jul-25, 04-Mar-2024          -> D-Mon-YY or D-Mon-YYYY
#   12-08-2025                      -> DD-MM-YYYY (numeric, dash-separated)
#   July 2023                       -> Month YYYY, no day at all -- tried last
#                                       since it's the least specific shape;
#                                       parse_date_text assumes the 15th
DATE_PATTERN = re.compile(
    r"(\d{1,2}/\d{1,2}/\d{4})"
    r"|(\d{1,2}(?:st|nd|rd|th)?\s+" + MONTH_WORD + r"\.?,?\s+\d{4})"
    r"|(\d{1,2}-" + MONTH_WORD + r"-\d{2,4})"
    r"|(\d{1,2}-\d{1,2}-\d{4})"
    r"|(" + MONTH_WORD + r"\s+\d{4})",
    re.IGNORECASE,
)


def _month_number(word: str) -> int | None:
    return MONTH_INDEX.get(word[:3].lower())


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_date_text(text: str) -> date | None:
    """Parses one already-matched date string (from DATE_PATTERN) into a date."""
    text = text.strip()

    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", text)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return _safe_date(y, mo, d)

    m = re.match(r"^(\d{1,2})(?:st|nd|rd|th)?\s+(" + MONTH_WORD + r")\.?,?\s+(\d{4})$", text, re.IGNORECASE)
    if m:
        mo = _month_number(m.group(2))
        if mo:
            return _safe_date(int(m.group(3)), mo, int(m.group(1)))

    m = re.match(r"^(\d{1,2})-(" + MONTH_WORD + r")-(\d{2,4})$", text, re.IGNORECASE)
    if m:
        mo = _month_number(m.group(2))
        if mo:
            year = int(m.group(3))
            if year < 100:
                year += 2000
            return _safe_date(year, mo, int(m.group(1)))

    # DD-MM-YYYY, numeric and dash-separated -- same day-first convention as
    # the D/M/YYYY slash format above.
    m = re.match(r"^(\d{1,2})-(\d{1,2})-(\d{4})$", text)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return _safe_date(y, mo, d)

    # Month YYYY with no day at all -- confirmed convention: assume the 15th
    # (the middle of the month), rather than the 1st or last day.
    m = re.match(r"^(" + MONTH_WORD + r")\s+(\d{4})$", text, re.IGNORECASE)
    if m:
        mo = _month_number(m.group(1))
        if mo:
            return _safe_date(int(m.group(2)), mo, 15)

    return None


def extract_pdf_text(content: bytes) -> str:
    """Returns the PDF's embedded text, or "" if there isn't any (e.g. a
    scanned image with no text layer) -- callers treat that the same as any
    other extraction miss, not as an error.
    """
    try:
        reader = PdfReader(io.BytesIO(content))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return ""


def ocr_pdf_bytes(content: bytes) -> str:
    """Fallback for a scanned/photographed document with no embedded text
    layer at all (extract_pdf_text comes back empty for these -- nothing to
    search, not a pattern-matching gap). Runs entirely on this server: pulls
    each page's own embedded scan image straight out of the PDF (pypdf, pure
    Python -- no page-rendering engine needed, since a scanned page's
    "content" already *is* one embedded image) and reads it with a local
    Tesseract OCR install. No Drive/Apps Script/network round trip, so
    unlike the old Drive-OCR path this doesn't need the document to already
    be uploaded anywhere first -- it works straight off the bytes in hand,
    even before anything's been saved (see cases_router.py's extract-preview
    endpoint).
    """
    try:
        reader = PdfReader(io.BytesIO(content))
        parts = []
        for page in reader.pages:
            for image_file in page.images:
                parts.append(pytesseract.image_to_string(image_file.image))
        return "\n".join(parts)
    except Exception:
        return ""


def _find_date_after(text: str, label_pattern: str, window: int) -> date | None:
    """Finds the first occurrence of label_pattern, then the nearest date
    within `window` characters after it. Handles "Label: Date", "Label\\nDate"
    (wrapped table cells), and prose ("...commenced ... on Date") uniformly,
    since in every sample seen the date sits close after its label.
    """
    for label_match in re.finditer(label_pattern, text, re.IGNORECASE):
        segment = text[label_match.end(): label_match.end() + window]
        date_match = DATE_PATTERN.search(segment)
        if date_match:
            parsed = parse_date_text(date_match.group(0))
            if parsed:
                return parsed
    return None


def _extract_range_cell(text: str) -> tuple[date | None, date | None]:
    """Handles a merged table cell like "Commencement Date- Completion Date"
    with a single value "26/02/2024 - 22/08/2025" -- two dates in one place
    separated by a dash, rather than two separately-labeled dates.
    """
    header = re.search(r"commencement\s*date[\s-]*completion\s*date", text, re.IGNORECASE)
    if not header:
        return None, None
    segment = text[header.end(): header.end() + 60]
    dates = list(DATE_PATTERN.finditer(segment))
    if len(dates) >= 2:
        start = parse_date_text(dates[0].group(0))
        end = parse_date_text(dates[1].group(0))
        return start, end
    return None, None


def _extract_session_end_date(text: str) -> date | None:
    """Last-resort fallback for a specific phrasing seen on one sample: "...at
    the end of Session 1 2026 (10July)." -- the year comes from "Session N
    YYYY" and the day/month from the parenthetical right after it. Explicitly
    does NOT match a nearby "conferred on <date>" sentence -- that's a
    different, later date (degree conferral), not the course end date.
    """
    m = re.search(r"session\s+\d+\s+(\d{4})\s*\((\d{1,2})\s*(" + MONTH_WORD + r")\)", text, re.IGNORECASE)
    if not m:
        return None
    year = int(m.group(1))
    mo = _month_number(m.group(3))
    if not mo:
        return None
    return _safe_date(year, mo, int(m.group(2)))


LABEL_LINE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9 /()'-]*:\s*$")


def _extract_label_value_block(text: str) -> dict[str, str]:
    """Some PDFs' underlying text extraction lists a whole block of field
    labels first (each on its own line, ending in ':') and then all of their
    values right after, in the same order -- e.g. "Name: / DOB: / Course
    Commencement Date: / ... / John Smith / 21/11/2001 / 09/09/2024 / ...".
    The label and its value aren't anywhere near each other in the raw text,
    so proximity search (_find_date_after) can't find the right one -- and
    worse, may grab a *different* field's value that happens to fall within
    its search window. This pairs the Nth label with the Nth value
    positionally instead, which is what the document's actual layout means.
    Returns {normalized_label: value_line}, e.g. {"course commencement
    date": "09/09/2024"}. Only kicks in if there's a real run of 2+
    consecutive label-shaped lines followed by that many value lines.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    best_start, best_count = None, 0
    i = 0
    while i < len(lines):
        if LABEL_LINE_PATTERN.match(lines[i]):
            j = i
            while j < len(lines) and LABEL_LINE_PATTERN.match(lines[j]):
                j += 1
            if j - i > best_count:
                best_start, best_count = i, j - i
            i = j
        else:
            i += 1

    if best_count < 2 or best_start is None:
        return {}

    labels = lines[best_start : best_start + best_count]
    values = lines[best_start + best_count : best_start + 2 * best_count]
    if len(values) < best_count:
        return {}

    return {label.rstrip(":").strip().lower(): value for label, value in zip(labels, values)}


START_BLOCK_KEYS = [
    "course commencement date",
    "actual commencement date",
    "commencement date",
    "course start date",
    "start date",
    "commencement",
]
END_BLOCK_KEYS = [
    "course completion date",
    "actual completion date",
    "completion date",
    "completion",
    "finish date",
]


def _date_from_block(block: dict[str, str], keys: list[str]) -> date | None:
    for key in keys:
        value = block.get(key)
        if not value:
            continue
        match = DATE_PATTERN.search(value)
        if match:
            parsed = parse_date_text(match.group(0))
            if parsed:
                return parsed
    return None


START_LABEL_PATTERNS = [
    r"actual\s+commencement\s*\n?\s*date\s*:?",
    r"course\s+commencement\s*\n?\s*date\s*:?",
    r"course\s+start\s*\n?\s*date\s*:?",
    r"commencement\s*\n?\s*date\s*:?",
    # Bare "Commencement:" with no "Date" after it -- seen on a real sample.
    # Tried last since it's the least specific: only falls back to this once
    # every "...commencement date..." variant above has already missed.
    r"commencement\s*:?",
    # Bare "Start Date:" (no "Course"/"Commencement" wording at all) -- seen
    # on a real sample. Tried last, same reasoning as "Commencement:" above.
    r"start\s*\n?\s*date\s*:?",
]
START_PROSE_PATTERNS = [
    r"commenced\s+(?:this|the)\s+(?:course|qualification)\b",
    r"started\s+the\s+course\b",
    r"commenced\s+study\b",
    # Bare "commenced on <date>" -- e.g. "The student commenced on the 13 May
    # 2024 as a full-time student". Tried last since it's the least specific.
    r"commenced\s+on\b",
]

END_LABEL_PATTERNS = [
    r"actual\s+completion\s*\n?\s*date\s*:?",
    r"course\s+completion\s*\n?\s*date\s*(?:\([^)]*\))?\s*\*?:?",
    r"completion\s*\n?\s*date\s*\*?:?",
    # Bare "Completion:" with no "Date" after it -- same reasoning as the
    # commencement fallback above, kept symmetric in case it shows up too.
    r"completion\s*:?",
    # Bare "Finish Date:" (no "Course"/"Completion" wording at all) -- seen
    # on a real sample. Tried last, same reasoning as "Start Date:" above.
    r"finish\s*\n?\s*date\s*:?",
]
END_PROSE_PATTERNS = [
    r"complet\w*\s+all\s+(?:the\s+)?requirements?\s+of\s+(?:this|the)\s+(?:program|course|qualification)[^.]{0,80}?\bon\b",
    r"successfully\s+complet\w*\b[^.]{0,40}?\bon\b",
    # Bare "completed on <date>" -- e.g. "...and completed on the 09 Nov
    # 2025". Tried last since it's the least specific.
    r"\bcompleted\s+on\b",
]


def _find_start_date(text: str, block: dict[str, str], range_start: date | None) -> date | None:
    """Finds "whatever this document's commencement/start date is", tried in
    priority order: label/value block pairing, the range-cell shorthand, then
    labeled and prose patterns.
    """
    start = _date_from_block(block, START_BLOCK_KEYS) if block else None
    start = start or range_start
    for pattern in START_LABEL_PATTERNS:
        if start:
            break
        start = _find_date_after(text, pattern, window=40)
    for pattern in START_PROSE_PATTERNS:
        if start:
            break
        # Wider than the label patterns' window -- "commenced study at
        # <institution name> in <Month YYYY>" needs room for the institution
        # name in between, unlike the other, more direct prose phrasings.
        start = _find_date_after(text, pattern, window=60)
    return start


def extract_completion_letter_fields_from_text(text: str) -> dict:
    """Same extraction as extract_completion_letter_fields, but starting from
    text that's already been pulled out of the document -- shared with the
    OCR fallback, which produces text a different way (Drive's OCR
    conversion) but needs the exact same pattern-matching applied to it.
    """
    if not text:
        return {}

    # Try the positional label/value-block pairing first -- when a document's
    # underlying text extraction produces this shape (all labels, then all
    # values, in matching order), it's a *more* reliable read than the
    # proximity search below, not less: proximity search risks grabbing an
    # unrelated nearby date (e.g. Date of Birth) if this document's fields
    # aren't actually adjacent to their labels in the extracted text.
    block = _extract_label_value_block(text)

    # The merged-range-cell format gives both dates from one signal.
    range_start, range_end = _extract_range_cell(text)

    start = _find_start_date(text, block, range_start)

    end = (_date_from_block(block, END_BLOCK_KEYS) if block else None) or range_end
    for pattern in END_LABEL_PATTERNS:
        if end:
            break
        end = _find_date_after(text, pattern, window=40)
    for pattern in END_PROSE_PATTERNS:
        if end:
            break
        end = _find_date_after(text, pattern, window=150)
    if not end:
        end = _extract_session_end_date(text)

    result = {}
    if start:
        result["start_date"] = start.isoformat()
    if end:
        result["end_date"] = end.isoformat()
    return result


def extract_completion_letter_fields(content: bytes) -> dict:
    """Returns {"start_date": iso-string|None, "end_date": iso-string|None}
    -- only ever from a Completion Letter, never a CoE, per how these two
    document types divide the work (dates here, CRICOS code from the CoE).
    """
    return extract_completion_letter_fields_from_text(extract_pdf_text(content))


def extract_new_coe_fields_from_text(text: str) -> dict:
    """Same extraction as extract_new_coe_fields, but starting from text
    that's already been pulled out of the document -- shared with the OCR
    fallback.
    """
    if not text:
        return {}
    block = _extract_label_value_block(text)
    range_start, _ = _extract_range_cell(text)
    start = _find_start_date(text, block, range_start)
    if not start:
        return {}
    return {"start_date": start.isoformat()}


def extract_new_coe_fields(content: bytes) -> dict:
    """Returns {"start_date": iso-string|None} from a "New CoE" -- the CoE
    for a course the student is planning to start next, used only for the
    lodgement-date calculation (not the CRICOS code, which only matters for
    a qualification's own duration check). Same document shape/labels as a
    regular CoE, so this reuses the same start-date search as the Completion
    Letter -- Completion Letters and CoEs have shown the same label
    vocabulary ("Commencement Date" and its variants) in every sample seen.
    """
    return extract_new_coe_fields_from_text(extract_pdf_text(content))


def extract_coe_fields_from_text(text: str) -> dict:
    """Same extraction as extract_coe_fields, but starting from text that's
    already been pulled out of the document -- shared with the OCR fallback.
    """
    if not text:
        return {}
    m = re.search(r"(?:^|\n)\s*Course:\s*[^\[\n]{1,150}?\[([A-Z0-9]{5,8})\]", text)
    if not m:
        return {}
    return {"cricos_code": m.group(1)}


def extract_coe_fields(content: bytes) -> dict:
    """Returns {"cricos_code": str|None} from a CoE -- anchored specifically
    on the "Course:" label (start of line) so it can't accidentally pick up
    the CRICOS *Provider* code, which appears in the same [XXXXXXX] bracket
    shape a few lines away on a "Provider:" line instead.
    """
    return extract_coe_fields_from_text(extract_pdf_text(content))


# ---------- Document validity documents: Current Visa, PTE, OVHC, AFP ----------
#
# Same "find date near label" toolkit as above, applied to the four document
# validity documents. Each returns whatever it can find -- a miss just leaves
# that field for an admin to fill in, same as the qualification documents.

VISA_DATE_LABEL_PATTERNS = [
    r"stay\s+until\s*:?",
    r"must\s+not\s+arrive\s+after\s*:?",
    r"length\s+of\s+stay\s*:?",
]


def extract_current_visa_fields_from_text(text: str) -> dict:
    """Same extraction as extract_current_visa_fields, but starting from text
    that's already been pulled out of the document -- shared with the OCR
    fallback.
    """
    if not text:
        return {}

    result = {}
    subclass_match = re.search(r"subclass\s*\(?(\d{3})\)?", text, re.IGNORECASE)
    if subclass_match:
        result["visa_subclass"] = subclass_match.group(1)

    length_of_stay = None
    for pattern in VISA_DATE_LABEL_PATTERNS:
        if length_of_stay:
            break
        length_of_stay = _find_date_after(text, pattern, window=40)
    if length_of_stay:
        result["length_of_stay_date"] = length_of_stay.isoformat()

    return result


def extract_current_visa_fields(content: bytes) -> dict:
    """Returns {"visa_subclass": "500", "length_of_stay_date": iso-string} --
    whichever of "Stay until" / "Must not arrive after" / "Length of stay" is
    found first (a real sample showed all three with the same date, so any
    one of them is treated as equally authoritative).
    """
    return extract_current_visa_fields_from_text(extract_pdf_text(content))


def extract_pte_fields_from_text(text: str) -> dict:
    """Same extraction as extract_pte_fields, but starting from text that's
    already been pulled out of the document -- shared with the OCR fallback.
    """
    if not text:
        return {}
    valid_until = _find_date_after(text, r"valid\s+until\s*:?", window=40)
    if not valid_until:
        return {}
    return {"valid_until_date": valid_until.isoformat()}


def extract_pte_fields(content: bytes) -> dict:
    """Returns {"valid_until_date": iso-string} from a PTE score report --
    the "Valid Until" date specifically (not the Test Date, which isn't
    used)."""
    return extract_pte_fields_from_text(extract_pdf_text(content))


def extract_afp_fields_from_text(text: str) -> dict:
    """Same extraction as extract_afp_fields, but starting from text that's
    already been pulled out of the document -- shared with the OCR fallback.
    """
    if not text:
        return {}
    match = DATE_PATTERN.search(text[:300])
    if not match:
        return {}
    parsed = parse_date_text(match.group(0))
    if not parsed:
        return {}
    return {"issue_date": parsed.isoformat()}


def extract_afp_fields(content: bytes) -> dict:
    """Returns {"issue_date": iso-string} for an AFP Certificate or Receipt.
    These typically show one standalone date near the top of the letter with
    no label at all (just sitting where a business letter's date normally
    goes) -- so instead of a label pattern, this takes the first date found
    within the first part of the document rather than searching the whole
    text (which could otherwise pick up an unrelated later date).
    """
    return extract_afp_fields_from_text(extract_pdf_text(content))


def extract_ovhc_fields_from_text(text: str) -> dict:
    """Same extraction as extract_ovhc_fields, but starting from text that's
    already been pulled out of the document -- shared with the OCR fallback.
    """
    if not text:
        return {}
    relevant = _find_date_after(text, r"policy\s+start\s*\n?\s*date\s*:?", window=40)
    if not relevant:
        # Some providers' OVHC letters don't label a policy start date at
        # all -- they just show the letter's own issue date near the top
        # (same no-label shape as AFP). Confirmed: that issue date is what
        # the validity check should use for these providers.
        match = DATE_PATTERN.search(text[:300])
        if match:
            relevant = parse_date_text(match.group(0))
    if not relevant:
        return {}
    return {"relevant_date": relevant.isoformat()}


def extract_ovhc_fields(content: bytes) -> dict:
    """Returns {"relevant_date": iso-string} from an OVHC document -- the
    labeled "Policy start date" when the provider includes one (e.g. nib),
    otherwise the letter's own issue date (e.g. Medibank, Bupa), which is
    what the validity check uses in that case."""
    return extract_ovhc_fields_from_text(extract_pdf_text(content))
