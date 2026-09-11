# 485 Visa Lodgement Date Calculator — Business Logic Reference

This document specifies every business rule in the system, independent of the current
tech stack (FastAPI/Python backend, Google Sheets as the data store, React frontend).
It's written so the logic can be re-implemented from scratch in any other stack.

The system answers two questions for a student pursuing an Australian subclass 485
(Temporary Graduate) visa:

1. **Am I eligible** based on my completed study (duration vs. CRICOS-registered length)?
2. **If eligible, and if my supporting documents are valid, what date should I lodge my
   485 application?**

These are produced by **three independent checks**, each with its own status/reason,
run in a fixed sequence with a cascading gate between them.

---

## 1. Core Concepts

### 1.1 The three-way status

Every check (and the overall case) reports one of three statuses:

| Status | Meaning |
|---|---|
| `eligible` | A real, confirmed pass. |
| `not_eligible` | A real, confirmed **disqualification** — a hard, final verdict. |
| `pending` | A genuine **data gap** (a document not uploaded yet, a date/code not extracted yet) — **never** shown to the student as "not eligible". It just means "not enough information yet to give a real verdict." |

This distinction matters throughout: `pending` implies the situation could still change
(add a document, fix an extraction) and the check should be retried later; `not_eligible`
is final and won't change unless the underlying input data itself changes (e.g. a
qualification is replaced, a document is re-uploaded and now reads differently).

### 1.2 Three independent checks, one cascading gate

| # | Check | Depends on |
|---|---|---|
| 1 | **Qualification Duration / CRICOS Eligibility** | Nothing (runs first) |
| 2 | **Document Validity** (Current Visa / PTE / OVHC / AFP) | Check 1 must be `eligible` |
| 3 | **Lodgement Date Calculation** | Checks 1 **and** 2 must both be `eligible` |

Each check has its own status + reason + a "breakdown" (the exact figures used, for
manual verification) — they are **not** merged into one combined verdict. But a
downstream check is genuinely meaningless if an upstream one hasn't passed (a lodgement
date is meaningless for someone who doesn't qualify, or whose visa has expired), so:

- The gate is enforced **by the check itself** (server-side), not just hidden in the UI —
  re-checked from the upstream check's **already-stored** result every time, never
  silently re-derived.
- **Cheap to evaluate**: the gate check happens before any expensive work (document
  re-extraction, OCR, CRICOS registry lookups), so calling a downstream check when it's
  blocked costs almost nothing.
- **Blocked-message wording distinguishes cause**: if the upstream blocker is a
  confirmed `not_eligible` (a hard failure), the downstream check reports itself as
  `not_eligible` too, with a reason like *"The document validity check is not eligible,
  so a lodgement date cannot be calculated."* If the upstream blocker is merely
  `pending` (not yet known), the downstream check reports itself as `pending` with a
  reason like *"Qualification check must show eligible before document validity can be
  checked."* — i.e. it might still resolve once more data arrives. If multiple blockers
  exist, name all of them (e.g. *"The qualification check and the document validity
  check are not eligible..."*), with correct singular/plural grammar (`is`/`are`).

---

## 2. Data Model

### 2.1 Case (one per student's 485 application)

| Field | Type | Notes |
|---|---|---|
| `student_name`, `stream` | string | `stream` ∈ `vocational` \| `higher` |
| `eligibility_status` / `eligibility_reason` | string | Check 1 result |
| `total_duration_weeks` | int | Check 1's credited total |
| `duration_breakdown` | JSON | Check 1's full figures (see §7) — persisted |
| `visa_subclass`, `visa_length_of_stay_date` | string, date | Extracted from Current Visa |
| `pte_valid_until_date` | date | Extracted from PTE |
| `ovhc_relevant_date` | date | Extracted from OVHC |
| `afp_issue_date` | date | Extracted from AFP Certificate/Receipt |
| `document_validity_status` / `document_validity_reason` | string | Check 2 result |
| `document_validity_breakdown` | JSON | Check 2's full figures — persisted |
| `new_coe_start_date` | date | Extracted from an optional "New CoE" upload |
| `lodgement_date_status` / `lodgement_date_reason` | string | Check 3 result |
| `lodgement_date` | date | The final answer |
| `lodgement_basis` | string | Human-readable explanation of which factor was picked |
| `lodgement_breakdown` | JSON | Check 3's full figures — persisted |

### 2.2 Course / Qualification (many per case)

| Field | Type | Notes |
|---|---|---|
| `name`, `course_type` | string | `course_type` ∈ `certificate` \| `diploma` \| `bachelors` \| `masters` |
| `start_date`, `end_date` | date | Extracted from the Completion Letter |
| `cricos_code` | string | Extracted from the CoE |
| `cricos_weeks` | int? | Looked up from the CRICOS registry using `cricos_code` |

### 2.3 Documents (per-course or per-case)

**Per-course** (qualification) document types: `coe` (optional), `completion_letter`
(required), `transcript` (required), `academic_certificate` (optional).

**Per-case** document types: `current_visa` (required), `pte` (required), `ovhc`
(required), `afp_certificate` (optional), `afp_receipt` (optional — but see §4.1, one of
the two AFP documents is required), `new_coe` (optional, only relevant to Check 3).

### 2.4 Why breakdowns are persisted, not just returned

Each check's "breakdown" (the exact dates/numbers behind its verdict, for manual
verification — see §7) must be **stored** alongside the status/reason, not just returned
transiently from the endpoint that computed it. Reasons:

- A student may not click anything ever again after the checks first run — the UI has no
  manual "recheck" buttons; every check is fully automatic (see §8).
- Without persistence, revisiting the page later would show a status with no supporting
  figures at all.
- Persisting also means a **previously-passing** check's breakdown survives even while a
  later check is blocked/re-run — nothing needs to overwrite what already succeeded.

Implementation-agnostic: store each breakdown as a serialized JSON blob (or a proper
nested table if your store supports it) next to the check's status/reason column(s).

---

## 3. Check 1 — Qualification Duration / CRICOS Eligibility

### 3.1 Constants

```
MIN_TOTAL_WEEKS = 92
```

### 3.2 Rule: credited weeks = CRICOS-registered duration, not actual completion speed

The 92-week minimum is a property of the qualification(s) **chosen** (their
CRICOS-registered duration), **not** of how fast the student actually completed them.

- `actual_weeks = round((end_date - start_date).days / 7)` — kept for reference/display
  only.
- **Credited weeks = the CRICOS-registered duration** (`cricos_weeks`, looked up from the
  registry — see §5) for that qualification, **regardless of whether actual_weeks is
  more, less, or equal**. A student who fast-tracks is credited the full registered
  duration, not penalised for finishing early; a student who took longer is not credited
  extra either.
- **Fallback**: if the CRICOS registry has no duration on record for this course's code
  at all (a genuine lookup miss, not "not yet looked up"), there's nothing to credit —
  fall back to `actual_weeks` instead of blocking the whole calculation as `pending`
  forever.

### 3.3 Per-stream grouping algorithm

**If any qualification is missing its `start_date` or `end_date`** → status `pending`,
reason `"Missing start/end date for: <names>"`. (A genuine data gap: extraction hasn't
found these dates yet.)

**Vocational stream**:
- Every **certificate** is evaluated and credited **individually** — never grouped or
  capped by another certificate. If a student has Certificate 1 and Certificate 2, each
  is credited its own CRICOS-registered weeks, and both are summed.
- Every **diploma** is likewise evaluated individually, on top of the certificates.
- (There is no "latest one only" rule for vocational — every certificate/diploma counts.)

**Higher stream**:
- Only the **latest** degree (by `end_date`) among `bachelors`/`masters` courses counts
  — take the single highest-`end_date` one and credit it. Earlier degrees on the case
  don't add to the total (a Masters supersedes an earlier Bachelors, for instance).

**Evaluating one group** (a single certificate, a single diploma, or the one counted
degree — always evaluated one course "group" at a time):
```
group.actual_weeks   = actual_weeks(course.start_date, course.end_date)
group.required_weeks = course.cricos_weeks   # None if the registry had nothing
group.credited_weeks = course.cricos_weeks if course.cricos_weeks is not None
                       else group.actual_weeks   # fallback
```

**If there are no qualifying groups at all** (e.g. a vocational case with only a
transcript, no certificate/diploma type set) → status `pending`, reason `"No
qualifications to evaluate"`.

**Total and verdict**:
```
total = sum(group.credited_weeks for group in groups)
if total < 92:
    status = "not_eligible"
    reason = f"Total CRICOS-registered duration of the selected qualification(s) is {total} weeks; 92 weeks required."
else:
    status = "eligible"
```

Every group is evaluated (not short-circuited on the first failure) so the total, and
any failure reason, always reflects every qualification, not just the first one looked
at.

---

## 4. Check 2 — Document Validity

### 4.1 Required documents

- **Current Visa** — required.
- **PTE** — required.
- **OVHC** — required.
- **AFP Certificate OR AFP Receipt** — either one satisfies the requirement; having
  both is not needed, and having neither is a genuine gap (`pending`, "Missing
  document(s): ... AFP Certificate or AFP Receipt").

If any of the above is missing entirely → status `pending`, reason names exactly which
document(s) are missing (e.g. `"Missing document(s): Current Visa, PTE"`).

### 4.2 Per-document rules (once all required documents are present)

Evaluated **in this order**, each one a hard gate before the next (a missing/unextracted
field for a document not yet reached in this order → `pending`, not `not_eligible`):

1. **Current Visa**
   - Must be **subclass 500** (as a plain string comparison, e.g. `"500"`).
     Otherwise → `not_eligible`, `"Current visa is not subclass 500"`.
   - Its **length-of-stay / must-not-arrive-after date must not have already
     passed** — an *expiry-type* date, valid means today or later:
     `visa_length_of_stay_date < today` → `not_eligible`, `"Current visa has expired
     (length of stay: <date>)"`.
2. **PTE**
   - Its **"Valid Until" date must not have already passed** — same expiry-type
     direction as the visa: `pte_valid_until_date < today` → `not_eligible`, `"PTE
     score has expired (valid until: <date>)"`.
3. **OVHC**
   - Its **relevant date must already have begun**, as of today or earlier — an
     *issue-type* date (a policy starting today already counts as started):
     `ovhc_relevant_date > today` → `not_eligible`, `"OVHC policy hasn't started yet
     (date: <date>)"`.
   - "Relevant date" = the policy's labeled start date if the provider's letter has
     one, otherwise the letter's own issue date (see §6.7).
4. **AFP (Certificate or Receipt)**
   - **If it's the Receipt**: presence alone is sufficient — **no date check at all**.
     Rationale: a Receipt (proof of having applied) is issued *before* the actual
     Certificate exists, so it has no meaningful issue date to validate against "today."
   - **If it's the Certificate** (and no Receipt on file, or the Receipt path doesn't
     apply): its **issue date must already have passed**, as of today or earlier — same
     issue-type direction as OVHC: `afp_issue_date > today` → `not_eligible`, `"AFP
     document hasn't been issued yet (date: <date>)"`.

If every one of these passes → status `eligible`.

### 4.3 Cascading gate (from §1.2)

Only runs its real logic once Check 1's **stored** `eligibility_status` is `eligible`.
Otherwise:
- Check 1 `not_eligible` → this check reports `not_eligible`, reason `"The
  qualification check is not eligible, so this cannot be checked."` (breakdown cleared)
- Check 1 `pending` (or anything else not `eligible`) → this check reports `pending`,
  reason `"Qualification check must show eligible before document validity can be
  checked."` (breakdown cleared)

---

## 5. CRICOS Registry Lookup

A qualification's registered course duration (in weeks) is looked up from the
government's public CRICOS course registry by its course code (extracted from the CoE
document — see §6.2), via `GET CourseDetails.aspx?CourseCode=<code>` (no need to drive
the site's search form). Parse the duration out of the response HTML (match on a stable
element id, not the human-readable label text, so a copy change on the page doesn't
silently break the lookup). If the code isn't found, the page simply doesn't have that
field at all — treat that as "no CRICOS duration known" (feeds the §3.2 fallback), not
as an error. A genuine network/request failure is a distinct error case (log/retry —
don't treat it the same as "not found").

---

## 6. Document Field Extraction

Documents are PDFs (or images). Fields are extracted with **regex over the PDF's own
embedded text** first; if that yields nothing (a scanned/photographed document with no
text layer at all), fall back to **OCR** and re-run the exact same regex patterns
against the OCR'd text. No AI/LLM extraction — every pattern below was built from real
sample documents; a genuinely new label wording simply won't match until a new pattern
is added for it.

### 6.1 Date parsing — recognized shapes (tried in this priority order)

| Shape | Example | Parse rule |
|---|---|---|
| `D/M/YYYY` or `DD/MM/YYYY` | `18/11/2024` | day-first |
| `D Month YYYY` (ordinal-tolerant, full or abbreviated month name) | `14th August 2026`, `13 Jun 2022` | — |
| `D-Mon-YY` or `D-Mon-YYYY` | `21-Jul-25` | 2-digit year → `+2000` |
| `DD-MM-YYYY` (numeric, dash) | `12-08-2025` | day-first |
| `Month YYYY` (**no day at all**) | `July 2023` | **defaults the day to the 15th** (the middle of the month) — tried **last**, since it's the least specific shape and must not swallow a full `D Month YYYY` match |

An invalid calendar date (e.g. `30/02/2024`) parses to nothing (not an error) — the
field is simply left unfilled, same as any other extraction miss.

### 6.2 Completion Letter → `start_date`, `end_date`

Tries, in order, until one succeeds:
1. **Label/value block pairing**: some PDF text extractions produce a whole block of
   field labels first (each ending in `:`, on its own line) followed by all their
   values in the same order (e.g. `Name: / DOB: / Course Commencement Date: / ... /
   John Smith / 21/11/2001 / 09/09/2024 / ...`). Detect a run of 2+ consecutive
   label-shaped lines, then pair the Nth label with the Nth value **positionally** —
   proximity-based search would otherwise risk grabbing an unrelated nearby date (e.g.
   Date of Birth) when the label and its value aren't actually adjacent in the raw
   text.
2. **Merged range-cell**: a header like `"Commencement Date- Completion Date"`
   followed by a single value cell `"26/02/2024 - 22/08/2025"` — two dates from one
   signal, split on being the 1st/2nd date found after the header.
3. **Labeled, proximity-based search** — find a label, then the nearest date within a
   window of characters after it (handles `"Label: Date"`, `"Label\nDate"` wrapped
   table cells, and prose alike). Start-date labels tried in order: `"Actual
   Commencement Date"`, `"Course Commencement Date"`, `"Course Start Date"`,
   `"Commencement Date"`, bare `"Commencement:"`, bare `"Start Date:"` (last two tried
   last — least specific). End-date labels, same pattern: `"Actual/Course Completion
   Date"`, `"Completion Date"`, bare `"Completion:"`, `"Finish Date:"`.
4. **Prose phrasing** (wider search window to allow for an institution name in
   between) — start: `"commenced (this|the) (course|qualification)"`, `"started the
   course"`, `"commenced study"`, bare `"commenced on"` (last, least specific). End:
   `"complet(ed|ing) all (the) requirements of (this|the) (program|course|
   qualification) ... on"`, `"successfully complet... on"`, bare `"completed on"`
   (last, least specific).
5. **Session-end fallback** (end date only, last resort) — a specific phrasing seen on
   one sample: `"...at the end of Session 1 2026 (10July)."` — the year comes from
   `"Session N YYYY"`, the day/month from the parenthetical right after it. Must **not**
   match an unrelated nearby `"conferred on <date>"` sentence (a different, later date —
   degree conferral, not course completion).

### 6.3 CoE → `cricos_code`

Anchored specifically on a `"Course:"` label at the **start of a line**, looking for a
bracketed code `[A-Z0-9]{5,8}` right after the course name — deliberately **not** a
`"Provider:"` line a few lines away, which has the same bracket shape but is the
*provider's* CRICOS code, not the course's.

### 6.4 New CoE → `start_date`

Reuses the exact same start-date search as the Completion Letter (§6.2, step 1/3/4) —
same label vocabulary appears on both document types. Used only as an input to Check 3
(see §8.3), never for the CRICOS code (that only matters for a qualification's own
duration check).

### 6.5 Current Visa → `visa_subclass`, `length_of_stay_date`

- Subclass: `subclass\s*\(?(\d{3})\)?` (case-insensitive) — e.g. matches `"Subclass
  (500) Student visa"` or `"Subclass 500"`.
- Length-of-stay date: proximity search after whichever of these labels is found first
  — `"Stay until:"`, `"Must not arrive after:"`, `"Length of stay:"` (all three
  observed to carry the same date on a real sample, so any one is treated as equally
  authoritative).

### 6.6 PTE → `valid_until_date`

Proximity search after `"Valid Until:"` specifically — **not** the nearby `"Test
Date:"`, which is a different date that isn't used at all.

### 6.7 OVHC → `relevant_date`

- Try `"Policy start date"` label first (some providers, e.g. nib, label it
  explicitly).
- If no label found, fall back to the **letter's own issue date** — the first date
  found within roughly the first 300 characters of the document (some providers, e.g.
  Medibank/Bupa, don't label a policy-start date at all; their own top-of-letter issue
  date is what the validity check uses instead).
- A labeled date always wins over a stray unlabeled date, even if the stray date
  happens to sit earlier in the raw text.

### 6.8 AFP Certificate/Receipt → `issue_date`

These typically show one standalone date near the top of the letter with **no label at
all** (just where a business letter's date normally sits). Take the **first date found
within roughly the first 300 characters** — deliberately not a whole-document search,
which could otherwise pick up an unrelated later date further down the page.

### 6.9 OCR fallback (only when regex-over-embedded-text finds nothing)

For a scanned/image-only document with no text layer: run the document through OCR to
recover its text, then re-run the **exact same** regex extraction functions against the
OCR output. Same extraction functions, two different text sources.

---

## 7. Breakdown ("Show calculation details") Contents

Each check's persisted breakdown is the exact machine-verifiable evidence behind its
verdict — **dates and numbers only, no prose explanation** (a UI/philosophy rule: this
is proof of the calculation, not a re-explanation of it).

**Check 1 (Qualification)**:
```json
{
  "groups": [
    {"label": "Degree (Bachelors 1)", "actual_weeks": 131, "required_weeks": null, "credited_weeks": 131}
  ],
  "total_weeks": 131,
  "min_required_weeks": 92
}
```

**Check 2 (Document Validity)**:
```json
{
  "checks": [
    {"label": "Current Visa", "extracted": {"subclass": "500", "length_of_stay_date": "2027-12-28"}},
    {"label": "PTE", "extracted": {"valid_until_date": "2028-07-10"}},
    {"label": "OVHC", "extracted": {"relevant_date": "2026-08-19"}},
    {"label": "AFP (Certificate or Receipt)", "extracted": {"issue_date": "2026-08-20"}}
  ]
}
```

**Check 3 (Lodgement Date)**:
```json
{
  "latest_completion_date": "2026-03-15",
  "window_end": "2026-09-15",
  "factors": [
    {"label": "current visa (60-day rule, capped at visa expiry)", "date": "2026-05-14", "included": true},
    {"label": "completion date + 6 months", "date": "2026-09-15", "included": true},
    {"label": "New CoE / 60-day rule", "date": "2026-06-01", "included": false}
  ]
}
```

---

## 8. Check 3 — Lodgement Date Calculation

### 8.1 Constants

```
LODGEMENT_WINDOW_MONTHS       = 6
LODGEMENT_BUFFER_DAYS         = 2
NEW_COE_MIN_GAP_DAYS          = 60
CURRENT_VISA_LODGEMENT_GAP_DAYS = 60
```

### 8.2 The 6-month application window (hard gate)

```
latest_completion_date = max(end_date across every qualification on the case)
window_end = latest_completion_date + 6 months   (see §8.5 for month-add rule)
if today > window_end:
    status = "not_eligible"
    reason = f"Outside the 6-month application window (window ended {window_end})"
    # window_end is still returned for display even on this hard fail
```
If `latest_completion_date` isn't known yet at all → status `pending`, reason
`"Qualification completion date not yet known"`.

### 8.3 The three factors (once inside the window)

Compute up to three candidate dates ("factors"); each is either **included** or
**excluded** from the final pick:

**A. Current-visa route** (always considered, if the visa's length-of-stay date is
known):
```
visa_route_date = min(latest_completion_date + 60 days, visa_length_of_stay_date)
```
Rationale: you'd normally lodge 60 days after completion, but that can never be later
than the visa's own length-of-stay/expiry date allows — so it's capped at whichever is
earlier. If the visa date isn't known at all, this factor is `included = false`.

**B. Completion date + 6 months** (= `window_end` from §8.2) — always included.

**C. New CoE / 60-day rule** (only if a New CoE was uploaded and its `start_date` was
extracted):
```
if new_coe_start_date > latest_completion_date:
    factor_c_date = max(new_coe_start_date, latest_completion_date + 60 days)
    included = true
else:
    # invalid New CoE -- its start date isn't even after the last completion date
    factor_c_date = new_coe_start_date
    included = false   # excluded from the pick, NOT a hard failure
```

### 8.4 Picking the final date

```
included_factors = [f for f in factors if f.included]
chosen = the included factor whose date is CLOSEST to today (min absolute day difference)
lodgement_date = chosen.date - 2 days   (LODGEMENT_BUFFER_DAYS)
lodgement_basis = f"Based on {chosen.label} ({chosen.date}), minus a 2-day buffer"
status = "eligible"
```

### 8.5 Adding N months to a date (calendar-correct)

```
month_index = date.month - 1 + months
year  = date.year + month_index // 12
month = month_index % 12 + 1
day   = min(date.day, days_in_month(year, month))   # clamp e.g. Jan 31 + 1mo -> Feb 28/29
```

### 8.6 Cascading gate (from §1.2)

Only runs its real logic once **both** Check 1's and Check 2's stored statuses are
`eligible`. Otherwise, collect blockers from each:
- Each upstream check that is `not_eligible` → added to a "hard" blocker list.
- Each upstream check that is anything else but `eligible` (i.e. `pending`) → added to
  a "soft" blocker list.

If any hard blocker exists → this check reports `not_eligible`; the reason names every
hard blocker (grammar-correct singular/plural: `"is"` for one, `"are"` for more than
one) — e.g. `"The document validity check is not eligible, so a lodgement date cannot
be calculated."` or `"The qualification check and the document validity check are not
eligible, so a lodgement date cannot be calculated."`

Otherwise (only soft blockers) → this check reports `pending`; reason names every soft
blocker — e.g. `"The qualification check must show eligible before a lodgement date can
be calculated."` (breakdown, lodgement_date, and lodgement_basis are all cleared in
either blocked case.)

---

## 9. Self-Healing Re-Extraction

Every check, before computing anything, **re-attempts extraction** for any field that's
still missing but has a document already on file to extract it from — re-downloading
that document's content and re-running the exact extraction logic from §6 (regex first,
OCR fallback). This is what lets:
- A bug fix to the extraction patterns retroactively apply to already-uploaded documents
  without asking the student to re-upload anything.
- A document that failed to extract at upload time (e.g. it needed OCR but that path
  wasn't wired up yet) get picked up automatically the next time a check runs.

Already-filled fields are left untouched (never overwritten by a re-extraction, even if
it disagrees) — this only ever fills in what was previously blank. Independent
fields/documents are re-extracted **concurrently** (not needlessly serialized) since
they don't depend on each other.

---

## 10. Auto-Run Model (no manual trigger buttons)

There are no "Check X" buttons for the student to click. Instead, on landing on the
results screen:

- **Each check re-runs automatically** whenever **either** of these is true:
  - it has never produced a persisted breakdown yet, **or**
  - its own status is still `pending` (a possibly-resolvable data gap — worth
    retrying).
- Once a check has a real, final verdict (`eligible` or `not_eligible`) **and** its
  breakdown is persisted, it is **not** re-run automatically on a later visit — it's
  just displayed from storage.
- The three checks are always attempted **in sequence** (1 → 2 → 3), and calling a
  later one is always safe even when an earlier one isn't eligible yet — the check
  itself handles the gate cheaply (§1.2) and records the correctly-worded blocked
  status. This means the auto-run can simply cascade forward "did stage N just run (or
  need to run) → also (re-)attempt stage N+1" without the caller needing to
  pre-validate eligibility itself.
- Uploading the optional **New CoE** document immediately re-triggers Check 3 (since
  there's no manual button to do it otherwise) — but only if Checks 1 and 2 are already
  `eligible` (no point recomputing a lodgement date that's currently blocked anyway).
- While any check is actively running, the UI shows a plain top-level indicator (e.g.
  "Your results are on their way — this can take up to a minute.") **without hiding the
  already-rendered result cards** — a check still "loading" shows its own in-progress
  state inline; nothing needs to disappear/blank out while waiting.

---

## 11. Quick-Reference: All Constants

| Constant | Value | Used in |
|---|---|---|
| `MIN_TOTAL_WEEKS` | 92 | Check 1 pass/fail threshold |
| `LODGEMENT_WINDOW_MONTHS` | 6 | Check 3 application window |
| `LODGEMENT_BUFFER_DAYS` | 2 | Check 3 final date buffer |
| `NEW_COE_MIN_GAP_DAYS` | 60 | Check 3, factor C |
| `CURRENT_VISA_LODGEMENT_GAP_DAYS` | 60 | Check 3, factor A |

## 12. Quick-Reference: Required vs. Optional Documents

| Document | Scope | Required? |
|---|---|---|
| Completion Letter | per qualification | **Required** |
| Transcript | per qualification | **Required** |
| CoE | per qualification | Optional (but needed for `cricos_code`/`cricos_weeks`) |
| Academic Certificate | per qualification | Optional |
| Current Visa | per case | **Required** |
| PTE | per case | **Required** |
| OVHC | per case | **Required** |
| AFP Certificate | per case | Required **as an either/or pair** with AFP Receipt |
| AFP Receipt | per case | Required **as an either/or pair** with AFP Certificate |
| New CoE | per case | Optional (only feeds Check 3, factor C) |

---

## 13. Appendix: Full Source Code

The prose above describes every rule; this appendix is the literal, executable Python
that implements it — copy these three files verbatim into a new project (they have no
dependency on the rest of this codebase's web framework, auth, or storage layer; the
only external packages needed are `pypdf` for `eligibility.py`/`cricos_lookup.py`'s
caller stack and `httpx` for the registry lookup).

### 13.1 `eligibility.py` — all three checks (pure functions, no I/O)

```python
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
```

### 13.2 `document_extract.py` — regex-based field extraction

```python
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

from pypdf import PdfReader

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
```

### 13.3 `cricos_lookup.py` — government CRICOS registry lookup

```python
"""Looks up a course's officially registered duration from the government
CRICOS course registry. Verified live against the real site: a course code
can be looked up with a plain GET to CourseDetails.aspx?CourseCode=<code> --
no need to drive the search form's ASP.NET postback at all. An unknown code
just redirects back to the empty search page instead of a details page, so
"not found" is detected by the duration field simply not being there.
"""

import re
import httpx

CRICOS_DETAILS_URL = "https://cricos.education.gov.au/Course/CourseDetails.aspx"

# Keyed off the page's actual element id rather than its human-readable
# label text, so a copy change on the page ("Duration (Weeks):" -> something
# else) doesn't silently break this.
DURATION_PATTERN = re.compile(r'id="ctl00_cphDefaultPage_courseDetail_lblDuration">(\d+)<')


class CricosLookupError(RuntimeError):
    pass


def lookup_duration_weeks(cricos_code: str) -> int | None:
    """Returns the registered duration in weeks, or None if the code isn't
    found on the registry. Raises CricosLookupError only for an actual
    network/request failure, not for a not-found code.
    """
    try:
        response = httpx.get(
            CRICOS_DETAILS_URL,
            params={"CourseCode": cricos_code},
            timeout=20.0,
            follow_redirects=True,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise CricosLookupError(f"CRICOS registry request failed: {exc}") from exc

    match = DURATION_PATTERN.search(response.text)
    return int(match.group(1)) if match else None
```
