"""Three independent checks -- qualification duration vs. CRICOS, document
validity (current visa/PTE/OVHC/AFP), and the lodgement date calculation.
Pure math/comparisons over data the caller already has in hand -- no
Sheets/HTTP calls in here, so it's testable in isolation. Deliberately kept
as separate results (their own status/reason each), not combined into a
single verdict -- the lodgement date calculation is the one exception with
a partial dependency: it only runs once the qualification check is already
"eligible" (checked by the caller against the already-stored result, not
re-derived here), but does not depend on the document validity check.

All three use the same three-way status: "eligible" | "not_eligible" |
"pending". "pending" means a genuine data gap (a document not uploaded yet,
or a field not extracted yet) -- not a verdict, so it's never shown to a
student as "not eligible". "not_eligible" is only ever a real, confirmed
disqualification.

Confirmed rule: the 92-week minimum is a property of the qualification(s)
*chosen* (their CRICOS-registered duration), not of how fast the student
actually got through them. A student who finishes early (fast-tracks) is
still credited the full CRICOS-registered weeks for that qualification --
`actual_weeks` is kept around for reference, but it no longer reduces or
fails a group on its own. The only way to land under 92 credited weeks is
for the qualifications themselves not to add up to enough CRICOS-registered
study.
"""

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta

MIN_TOTAL_WEEKS = 92
LODGEMENT_WINDOW_MONTHS = 6
LODGEMENT_BUFFER_DAYS = 2
NEW_COE_MIN_GAP_DAYS = 60
CURRENT_VISA_LODGEMENT_GAP_DAYS = 60


@dataclass
class CourseInput:
    id: int
    name: str
    course_type: str  # certificate | diploma | bachelors | masters
    start_date: date | None
    end_date: date | None
    cricos_weeks: int | None


@dataclass
class GroupResult:
    label: str
    actual_weeks: int
    required_weeks: int | None  # None if the CRICOS registry had no duration for this course -- credited_weeks then falls back to actual_weeks instead
    credited_weeks: int


@dataclass
class DurationResult:
    status: str  # "eligible" | "not_eligible" | "pending"
    total_weeks: int
    reason: str | None
    groups: list[GroupResult] = field(default_factory=list)


def actual_weeks(start: date, end: date) -> int:
    return round((end - start).days / 7)


def _evaluate_group(courses: list[CourseInput], label: str) -> GroupResult:
    """Sums the group's actual weeks and credits the *latest* course's
    CRICOS-registered duration -- this is also correct for a single-course
    group (the "latest" of one course is just that course). Credited weeks
    equal the registered duration once it's known, whether the student
    finished early, on time, or later (fast-tracking is credited, not
    penalised). Used for a group that's genuinely evaluated as one unit (a
    single diploma, or the latest degree) -- a group of several separate
    certificates is evaluated one certificate at a time instead (see
    calculate_duration), each getting credited its own CRICOS-registered
    weeks the same way.

    Confirmed fallback: if the CRICOS registry genuinely has no duration for
    this course (lookup found nothing, as opposed to just not having been
    looked up yet), there's no registered figure to credit at all -- so this
    falls back to the actual completion-letter duration instead of blocking
    the whole calculation as "pending" indefinitely.
    """
    total_actual = sum(actual_weeks(c.start_date, c.end_date) for c in courses)
    latest = max(courses, key=lambda c: c.end_date)
    required = latest.cricos_weeks

    if required is None:
        return GroupResult(label, total_actual, None, total_actual)
    return GroupResult(label, total_actual, required, required)


def calculate_duration(stream: str, courses: list[CourseInput]) -> DurationResult:
    missing = [c for c in courses if not c.start_date or not c.end_date]
    if missing:
        names = ", ".join(c.name for c in missing)
        return DurationResult("pending", 0, f"Missing start/end date for: {names}")

    groups: list[GroupResult] = []
    if stream == "vocational":
        # Each certificate is credited its own CRICOS-registered weeks --
        # same treatment as each diploma below, not combined into one
        # latest-only group. An earlier certificate's registered duration
        # counts in full; it's never discarded just because a later
        # certificate also exists.  
        certificates = [c for c in courses if c.course_type == "certificate"]
        diplomas = [c for c in courses if c.course_type == "diploma"]
        for certificate in certificates:
            groups.append(_evaluate_group([certificate], f"Certificate ({certificate.name})"))
        for diploma in diplomas:
            groups.append(_evaluate_group([diploma], f"Diploma ({diploma.name})"))
    else:  # higher
        degrees = [c for c in courses if c.course_type in ("bachelors", "masters")]
        if degrees:
            latest_degree = max(degrees, key=lambda c: c.end_date)
            groups.append(_evaluate_group([latest_degree], f"Degree ({latest_degree.name})"))

    if not groups:
        return DurationResult("pending", 0, "No qualifications to evaluate", groups)

    total = sum(g.credited_weeks for g in groups)
    if total < MIN_TOTAL_WEEKS:
        return DurationResult(
            "not_eligible", total,
            f"Total CRICOS-registered duration of the selected qualification(s) is {total} weeks; {MIN_TOTAL_WEEKS} weeks required.",
            groups,
        )

    return DurationResult("eligible", total, None, groups)


@dataclass
class Stage1Input:
    has_current_visa: bool
    has_pte: bool
    has_ovhc: bool
    has_afp: bool  # True if EITHER the Certificate or the Receipt is present
    has_afp_receipt: bool  # True specifically for the Receipt (not the Certificate) -- its own presence is enough, no date check (see check_stage1)
    visa_subclass: str | None
    visa_length_of_stay_date: date | None
    pte_valid_until_date: date | None
    ovhc_relevant_date: date | None  # labeled "Policy start date" for some providers (e.g. nib), the letter's own issue date for others (e.g. Medibank, Bupa) -- either way, the date the validity check uses
    afp_issue_date: date | None


@dataclass
class Stage1Result:
    status: str  # "eligible" | "not_eligible" | "pending"
    reason: str | None


# Confirmed per-document rules:
#   Current Visa -- must be subclass 500, and its length-of-stay / must-not-
#     arrive-after date must NOT have already passed (an expiry-type date:
#     valid means today or later).
#   PTE -- its "Valid Until" date must NOT have already passed (same
#     expiry-type direction as the visa).
#   OVHC -- its relevant date (policy start, or the letter's own issue date
#     when no policy-start label exists) must already have begun, as of today
#     or earlier (an issue-type date: a policy starting today already counts
#     as started, not "not yet started").
#   AFP (Certificate or Receipt, either satisfies) -- if it's the Receipt,
#     its mere presence is enough -- confirmed rule: a Receipt (proof of
#     having applied, issued before the actual Certificate exists) has no
#     issue date to meaningfully check, so the date rule below only applies
#     when it's the Certificate on file. Certificate date must already have
#     been issued, as of today or earlier (same issue-type direction as
#     OVHC -- a document issued today already counts as issued).
def check_stage1(data: Stage1Input, today: date) -> Stage1Result:
    missing = []
    if not data.has_current_visa:
        missing.append("Current Visa")
    if not data.has_pte:
        missing.append("PTE")
    if not data.has_ovhc:
        missing.append("OVHC")
    if not data.has_afp:
        missing.append("AFP Certificate or AFP Receipt")
    if missing:
        return Stage1Result("pending", f"Missing document(s): {', '.join(missing)}")

    if not data.visa_subclass or not data.visa_length_of_stay_date:
        return Stage1Result("pending", "Current Visa details not yet extracted")
    if data.visa_subclass != "500":
        return Stage1Result("not_eligible", "Current visa is not subclass 500")
    if data.visa_length_of_stay_date < today:
        return Stage1Result("not_eligible", f"Current visa has expired (length of stay: {data.visa_length_of_stay_date.isoformat()})")

    if not data.pte_valid_until_date:
        return Stage1Result("pending", "PTE valid-until date not yet extracted")
    if data.pte_valid_until_date < today:
        return Stage1Result("not_eligible", f"PTE score has expired (valid until: {data.pte_valid_until_date.isoformat()})")

    if not data.ovhc_relevant_date:
        return Stage1Result("pending", "OVHC relevant date not yet extracted")
    if data.ovhc_relevant_date > today:
        return Stage1Result("not_eligible", f"OVHC policy hasn't started yet (date: {data.ovhc_relevant_date.isoformat()})")

    if not data.has_afp_receipt:
        if not data.afp_issue_date:
            return Stage1Result("pending", "AFP issue date not yet extracted")
        if data.afp_issue_date > today:
            return Stage1Result("not_eligible", f"AFP document hasn't been issued yet (date: {data.afp_issue_date.isoformat()})")

    return Stage1Result("eligible", None)


@dataclass
class Stage3Input:
    latest_completion_date: date | None  # max end_date across every qualification on the case
    visa_length_of_stay_date: date | None
    new_coe_start_date: date | None  # None if never uploaded or invalid (see check_stage3)


@dataclass
class Stage3Factor:
    label: str
    date: date | None
    included: bool  # False for a factor that was excluded (e.g. an invalid New CoE, or a visa date that isn't known)


@dataclass
class Stage3Result:
    status: str  # "eligible" | "not_eligible" | "pending"
    reason: str | None
    lodgement_date: date | None = None
    lodgement_basis: str | None = None  # explanation of which factor was chosen
    window_end: date | None = None  # completion date + 6 months, for display even when a hard fail
    factors: list[Stage3Factor] = field(default_factory=list)  # every factor considered, so the caller can show the full picture without recomputing it


def _add_months(d: date, months: int) -> date:
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])  # clamp e.g. Jan 31 + 1mo -> Feb 28/29
    return date(year, month, day)


# Confirmed rule: today must be within 6 months of the latest qualification's
# actual completion date, or the case fails outright ("outside the 6-month
# application window").
#
# If within the window, the lodgement date is whichever of up to three
# factors is closest to today, minus a fixed 2-day buffer:
#   A. the current-visa route: completion date + 60 days vs. the visa's
#      length-of-stay date, whichever is EARLIER -- normally you lodge 60
#      days after completion, but that can't be later than the visa itself
#      allows, so it's capped at the visa's own expiry.
#   B. completion date + 6 months
#   C. (optional) the New CoE's start date vs. (completion date + 60 days),
#      whichever is LATER -- only included if a New CoE was provided and its
#      start date is actually after the last completion date; an invalid one
#      (start date not after completion) is simply excluded, not a hard fail.
def check_stage3(data: Stage3Input, today: date) -> Stage3Result:
    if not data.latest_completion_date:
        return Stage3Result("pending", "Qualification completion date not yet known")

    window_end = _add_months(data.latest_completion_date, LODGEMENT_WINDOW_MONTHS)
    if today > window_end:
        return Stage3Result(
            "not_eligible", f"Outside the 6-month application window (window ended {window_end.isoformat()})",
            window_end=window_end,
        )

    factors: list[Stage3Factor] = []
    if data.visa_length_of_stay_date:
        visa_route_date = min(
            data.latest_completion_date + timedelta(days=CURRENT_VISA_LODGEMENT_GAP_DAYS),
            data.visa_length_of_stay_date,
        )
        factors.append(Stage3Factor("current visa (60-day rule, capped at visa expiry)", visa_route_date, True))
    else:
        factors.append(Stage3Factor("current visa (60-day rule, capped at visa expiry)", None, False))

    factors.append(Stage3Factor("completion date + 6 months", window_end, True))

    if data.new_coe_start_date:
        if data.new_coe_start_date > data.latest_completion_date:
            factor_c_date = max(data.new_coe_start_date, data.latest_completion_date + timedelta(days=NEW_COE_MIN_GAP_DAYS))
            factors.append(Stage3Factor("New CoE / 60-day rule", factor_c_date, True))
        else:
            # Invalid New CoE (start date not after the last completion date)
            # -- excluded from the pick, not a hard fail.
            factors.append(Stage3Factor("New CoE / 60-day rule", data.new_coe_start_date, False))

    included = [f for f in factors if f.included]
    chosen = min(included, key=lambda f: abs((f.date - today).days))
    lodgement_date = chosen.date - timedelta(days=LODGEMENT_BUFFER_DAYS)
    basis = f"Based on {chosen.label} ({chosen.date.isoformat()}), minus a {LODGEMENT_BUFFER_DAYS}-day buffer"
    return Stage3Result("eligible", None, lodgement_date, basis, window_end, factors)
