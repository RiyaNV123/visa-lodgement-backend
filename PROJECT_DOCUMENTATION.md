# Visa Lodgement Date Calculator — Project Documentation

**Status:** Phase 1 (accounts, document collection) is complete and signed off. Three checks are built and live: the qualification duration / CRICOS eligibility check, the document validity check (Current Visa/PTE/OVHC/AFP), and the lodgement date calculation — each with its own status, reason, and calculation-details breakdown, kept as three distinct results rather than one combined verdict. They run in a confirmed sequence, though: document validity only ever runs once the qualification check is eligible, and the lodgement date only ever runs once *both* of those are eligible — a document-validity or lodgement-date result is otherwise meaningless. All three checks run entirely automatically the moment a student's documents are submitted — there are **no manual buttons at all** on the results screen, just the three results appearing on their own (the only exception is the optional New CoE upload, which itself automatically re-triggers the lodgement date check once uploaded). Each check's calculation-details table is dates and numbers only — no written explanations.

---

## 1. What This Product Is

The tool determines a student's eligibility for an Australian subclass 485 (Temporary Graduate) visa and manages the supporting documents for that application.

### Two roles, enforced by the backend (not just hidden in the UI)

- **Students** self-sign up, log in, and submit everything on one screen: their 485 stream, each completed qualification with its documents, and the additional documents (Current Visa/PTE/OVHC/AFP/optional New CoE). After a Preview step and Save, all three checks run automatically with no buttons to click, and the student sees three side-by-side results — Qualification, Document Validity, Lodgement Date — each with a status and a dates/numbers-only calculation-details breakdown.
- **Admins** do not self-sign up. Accounts are provisioned by hand, directly in the Google Sheet. Admins see every student who has signed up (not just ones with a submitted case), can open and review any case's documents, mark a case reviewed, and see the same three results as the student plus the total credited study weeks behind an eligible qualification result.

---

## 2. Architecture & Technology Choices

| Layer | Choice | Why |
|---|---|---|
| Frontend | React 18 + Vite + Tailwind CSS v4 + React Router | Matches the client's provided visual style guide exactly (navy/off-white theme, Manrope/Inter typography, rounded cards). |
| Backend | Python + FastAPI | Lightweight, fast to iterate, clean typed request/response models. |
| Data storage | Google Sheets (4 tabs: Users, Cases, Courses, Documents) | No database server to run or pay for; the client can open and inspect raw data directly in a spreadsheet they already own. |
| File storage | Google Drive | One folder per case; documents organised automatically by student. |
| Integration layer | A single Google Apps Script Web App | Both Sheets and Drive access go through one script, callable over plain HTTPS with a shared secret. |
| Auth | JWT + bcrypt password hashing | Standard, stateless, no session storage needed. |
| Document data extraction | Regex-based text extraction directly from each PDF's embedded text layer (no OCR, no AI) | An AI-based (Gemini) extraction approach was prototyped and then explicitly dropped at the client's request. Every extraction pattern was built and hardened against real, client-supplied sample documents rather than assumed from a template. |
| Course duration lookup | Direct lookup against the Australian government's CRICOS course registry | Confirms the officially registered duration (in weeks) for a course from its CRICOS code, rather than trusting a self-reported number. |

**Design principle carried through the whole project:** every integration avoids the Google Cloud Console entirely. This was an explicit, repeated client preference: one Apps Script Web App is the only integration surface, authorised once, with no billing account and no service-account credentials to manage.

---

## 3. Phase 1 — Delivered

Signed off by the client. Covers account creation, document collection, and staff review tooling.

### 3.1 Student flow

- Sign up (always creates a student account) or log in.
- **Everything is collected on one screen**: a stream dropdown (Post Vocational 485 — Certificate/Diploma — or Post Higher 485 — Bachelors/Masters — which restricts which course types can be added), each completed qualification with its 4 documents (CoE, Completion Letter, Transcript, Academic Certificate — Completion Letter and Transcript required), and the additional documents (Current Visa, PTE, OVHC, AFP Certificate or Receipt, and an optional New CoE for the lodgement-date calculation). Switching the stream dropdown after adding qualifications clears them, since their course types belong to the previous stream.
- **Preview** shows everything entered — every qualification's documents and the additional documents — for a final check before saving.
- **Save** creates the case and uploads every document in the background — the student is never blocked waiting for uploads to finish, and a single failed upload doesn't hold up the rest. The student lands directly on the results screen; there's no separate qualifications/additional-documents screen to click through afterward.
- The results screen shows three side-by-side results (Qualification / Document Validity / Lodgement Date) — see Sections 4–6 — which run automatically the moment the student arrives.
- "Change stream / start again" lets a student redo their case if needed.

### 3.2 Admin flow

- Log in only — no admin self-signup exists anywhere in the product.
- "All Cases" dashboard lists every student who has signed up, whether or not they've submitted a case yet, each with a stream badge (or "No case yet"), a review-status badge, course count, and signup date.
- Opening a case shows every qualification's documents and the additional documents (the same underlying documents a student submitted, though the student's own screen no longer shows this document-by-document view — see 3.1), plus the owner's email and the three results, with a Preview link on every uploaded document that opens the real file in Google Drive.
- "Done — mark as reviewed" records that the admin has checked a case and returns straight to the case list — supporting a clean review-one-after-another workflow across many students.

### 3.3 Admin account provisioning

Admin accounts are never created through the app. An admin is added by hand, directly in the Sheet's Users tab, using a small local tool that turns a chosen password into the correct hash to paste in — or by running a short interactive setup script. This keeps account creation completely outside any public-facing surface.

### 3.4 Document handling rules

- Every document is validated for file type (PDF/JPG/PNG) and a 15MB size limit.
- Files are renamed on upload to a consistent, readable pattern (e.g. "Certificate 1 – Transcript.pdf") so a case's Drive folder stays organised regardless of what the student originally named the file.
- Once a document type is uploaded for a course, it cannot be silently overwritten — the system blocks a duplicate upload, protecting against an accidental second file quietly replacing a verified one.

---

## 4. Qualification Duration / CRICOS Eligibility Check — Delivered

### 4.1 How data is extracted

Instead of the student typing in course start/end dates or an AI reading the document, the system reads each document's own embedded PDF text directly and pulls out the relevant fields using a purpose-built set of text patterns:

- **Completion Letter** → course start date and course completion date.
- **CoE** → the CRICOS course code.

Every pattern was built and tested against real sample documents supplied by the client (not a generic template), covering multiple label wordings and document layouts. A miss on a genuinely new document phrasing simply leaves that field blank for an admin to fill in by hand — it never blocks the document from being saved.

### 4.2 CRICOS registry lookup

Once a CoE's CRICOS code is extracted, the system looks it up directly against the Australian government's CRICOS course registry to get that course's officially registered duration in weeks. If the registry genuinely has no duration for that course (not just "not looked up yet"), the calculation falls back to that course's own actual completion-letter duration instead of blocking indefinitely.

### 4.3 The eligibility rule

- A student needs a total of **92 weeks** of CRICOS-registered study across their qualifying course(s) to be eligible.
- **Fast-tracking is credited, not penalised:** a student who finishes a course earlier than its officially registered duration is still credited that course's full registered duration, not the shorter time it actually took them. The 92-week test is about the study program the student chose, not how quickly they personally got through it.
- **Vocational stream:** every completed certificate is credited its own CRICOS-registered duration individually (not just the most recent one), and every completed diploma the same way — each qualification's registered weeks are summed together for the total.
- **Higher stream:** only the most recent Bachelors or Masters counts — an earlier degree on the same case doesn't add to the total.
- Every qualification is evaluated (not just the first one found), so if the total falls short, the reason names every qualification that's missing the data needed to confirm it.

### 4.4 What the student and admin see

- Lives in the **first column** of the three-column results screen (Qualification Check | Document Validity Check | Lodgement Date Calculation) that the student lands on immediately after Preview → Save.
- Runs **automatically** the very first time a case reaches this screen — no click needed, and **there is no button at all** for this check. If it comes back eligible, Document Validity then runs automatically too, and if that's also eligible, Lodgement Date runs automatically last. A student who does nothing after saving still sees all three results (or however far the chain gets) fill in on their own, with a plain "Checking…" line while each one is in flight.
- **Eligible** — the student's completed study meets the 92-week requirement.
- **Not eligible** — a specific reason is shown (e.g. total registered weeks short of 92).
- **Pending** — a genuine data gap (a document not uploaded yet, or a date/CRICOS code that couldn't be extracted) — this is never shown as a false "not eligible."
- Admins additionally see the total credited weeks behind an "eligible" result.
- A collapsible **"Show calculation details"** section (visible to the student, not just admins, and **open by default**) shows the exact figures behind the result: each course's extracted start/end date and actual weeks, and each qualification group's CRICOS-registered vs. credited weeks, plus the running total against the 92-week minimum. Deliberately **dates and numbers only** — no explanatory sentences — recomputed fresh every time, so it always matches the currently displayed result and lets anyone manually re-check the math.

---

## 5. Document Validity Check — Delivered

A second, completely independent check — deliberately not combined with the qualification check above into one verdict. Two separate buttons, two separate results.

### 5.1 What's checked

- **Current Visa** — must be subclass 500, and its length-of-stay ("Must Not Arrive After") date must not have already passed.
- **PTE** — its "Valid Until" date must not have already passed.
- **OVHC** — its relevant date must already be before today. Different providers present this differently: nib labels a "Policy start date" explicitly; Medibank and Bupa don't label one at all, so the letter's own issue date near the top is used instead — confirmed to carry the same meaning for the validity check either way.
- **AFP** (Certificate or Receipt — either one satisfies the requirement) — its issue date (a standalone date near the top of the letter, no label) must already be before today.

Same extraction approach as the qualification documents: regex against the PDF's own embedded text first, falling back to Drive's built-in OCR conversion for a scanned document with no text layer at all.

### 5.2 What the student and admin see

- Lives in the **second column** of the three-column results screen.
- **Gated on the qualification check**: this one is only ever triggered once the qualification check has already come back eligible (read from the already-stored result, never recomputed) — there's no point validating documents for a case that doesn't even meet the duration requirement. If the qualification check isn't eligible, this one simply never runs and stays "pending" with that reason.
- Runs **automatically**, immediately after the qualification check auto-completes as eligible — no click needed, and, like the qualification check, **there is no button at all** for it; reports which document(s) are still missing as "pending" if not everything's uploaded yet.
- Its own colored status box (eligible / not eligible / pending + a reason) and its own collapsible **"Show document validity details"** table (open by default), listing each document's extracted date(s) only — no rule text or written explanation — recomputed fresh on every check.
- Same self-healing re-extraction as the qualification check: a document uploaded before an extraction fix gets picked up automatically the next time this check runs.
- Its own result doesn't affect, or get affected by, the qualification duration/CRICOS figures themselves — the two are read and displayed side by side, not merged. The only relationship between them is the one-way gate described above.

---

## 6. Lodgement Date Calculation — Delivered

A third independent check. Kept deliberately separate from the other two for now (its own trigger, its own result, its own breakdown) — the client's plan is to integrate the three checks together only once each is confirmed solid on its own.

### 6.1 Its dependencies

This one is gated on **both** of the other two checks: the "Check Lodgement Date" button stays disabled until the qualification check **and** the document validity check have both already come back eligible (each read from its already-stored result, never recomputed) — there's no point calculating a lodgement date for someone who doesn't qualify, or whose visa/PTE/OVHC/AFP documents aren't valid in the first place. The button's tooltip names whichever of the two isn't satisfied yet (or both, e.g. "The qualification check and the document validity check must show eligible before a lodgement date can be calculated."). This is a change from the original design, where this check depended only on the qualification result — a PTE expiring, for instance, was originally allowed to not stop the visa's own length-of-stay date from being used as a valid input. It was widened to also require document validity because a lodgement date is meaningless if the underlying documents (visa, PTE, OVHC, AFP) aren't currently valid.

### 6.2 The calculation

- **6-month application window**: today must be within 6 months of the latest qualification's completion date, or the case fails outright as "outside the 6-month application window."
- **The lodgement date** is whichever of up to three factors sits closest to today, minus a 2-day buffer:
  1. The visa's length-of-stay date.
  2. Completion date + 6 months.
  3. An optional **New CoE** — a course the student is already enrolled in next, if any. Compares the CoE's start date against completion date + 60 days and uses whichever is later. An invalid CoE (its start date isn't after the last completion date) is simply excluded from the pick, not a hard failure.
- New CoE extraction reuses the exact same start-date logic as the Completion Letter (same label vocabulary on both document types), with the same OCR fallback for a scanned document.

### 6.3 What the student and admin see

- Lives in the **third column** of the three-column results screen.
- A **New CoE upload slot** (optional) sits at the top of this column, above its status box, since it's specifically an input to this calculation rather than a document validity concern. Selecting a file uploads it immediately, and — since there's no button on this check to click afterward — automatically re-runs the lodgement date calculation right after, provided both other checks are already eligible.
- Runs **automatically**, immediately after both the qualification check and the document validity check have auto-completed as eligible — no click needed, and **there is no button at all** for this check either. If either of those isn't eligible, this one is simply never triggered and stays "pending."
- Its own colored status box and a collapsible **"Show lodgement date details"** table (open by default) showing the latest completion date, the 6-month window's end date, and every factor considered (including any excluded one, e.g. an invalid New CoE) with its date and whether it was used — dates and numbers only, no explanatory sentence about the method.
- **Shown to students for now**, same as the other two checks, so it can be manually verified during this build — the original spec's admin-only restriction on the lodgement date is intentionally not yet applied; that's a deliberate later step, not an oversight.

---

## 7. Backend Performance Work

After Phase 1 was signed off, testing found the Google Sheets-backed backend to be severely slow — creating a case could take 24 seconds, uploading a single document up to 30 seconds, and the admin dashboard got slower the more students signed up rather than staying a fixed cost. This was treated as a priority fix before any further feature work.

### Root causes identified

- Every table read fetched the entire spreadsheet with no caching, filtering, or pagination.
- Generating a new record's ID required re-reading an entire table just to compute the next number, on every single insert.
- Several backend operations made many sequential round-trips to the Apps Script where one would do — creating a case with one course took roughly eight separate network calls.
- Listing cases or students made one extra round-trip per row, so the admin dashboard got proportionally slower as more students signed up.

### Fixes applied

- The backend now caches every Sheets table in memory for a short window, cleared instantly whenever something is actually written — so a page you are actively using stays fast, and you never see stale data from your own actions.
- The Apps Script integration was rewritten to generate IDs from a safe counter instead of rescanning a table, and to combine several related steps (e.g. creating a case and all its courses, or uploading a document and updating its case record) into one network call instead of many.
- This also closed a data-integrity risk: without the new safe-counter approach, two people submitting at almost the same instant could theoretically have been assigned the same internal record ID.
- Document uploads run in the background rather than blocking the student's screen, so a slow upload no longer looks like a frozen page.

### Result

| Action | Before | After (repeat request within a session) |
|---|---|---|
| Simple authenticated request | 3–4 seconds | ~0.1 seconds |
| Create a case (2 courses) | 24 seconds (1 course) | 6–16 seconds first time, then instant on repeat views |
| Upload a document | 25–30 seconds | 13–20 seconds (now happens in the background) |
| List all cases (admin dashboard) | ~26 seconds with 2 cases | ~0.1 seconds on repeat load |

*The remaining floor on a completely fresh ("cold") request is Google's own per-call overhead for this kind of integration, which is outside the application's control. The practical, and by far most common, experience — a staff member or student clicking between pages during one working session — is now near-instant rather than a multi-second wait on every click.*

---

## 8. Independent Quality Review

A live, hands-on review of the running system (not just a code read-through) was carried out, exercising real sign-ups, document uploads, admin review, real document extraction, and edge cases. Findings and their current status:

| Finding | Status |
|---|---|
| Severe backend latency, worsening with more data | Resolved — see Section 7 |
| No safeguard against duplicate internal record IDs under concurrent use | Resolved — see Section 7 |
| Document uploads blocked the screen and could silently drop a batch under fast concurrent use | Resolved — uploads now run fully in the background with independent tracking per batch |
| Once uploaded, a student had no way to fix a wrongly-uploaded document themselves | Open — backend support exists, needs a UI control |
| No way to add a single extra qualification to an already-submitted case without redoing the whole case | Open — design decision needed |
| A course whose CRICOS registry lookup finds no match used to get stuck as "pending" forever | Resolved — falls back to the actual completion-letter duration automatically, see Section 4.2 |
| Minor cosmetic spacing issue on long document labels | Low priority |

---

## 9. Reference

### 8.1 Documents produced alongside this one

- **Project context & continuation guide** — a living technical handoff document covering the full current state of the codebase, architecture decisions, and their rationale.
- **Independent quality review** — the live-tested findings referenced in Section 8, with full reproduction detail.
- **Admin setup guide** — step-by-step instructions for provisioning a new admin account.

### 8.2 Roles summary

| | Student | Admin |
|---|---|---|
| Account creation | Self-signup | Provisioned by hand only |
| Visibility | Own case only | Every signed-up student and every case |
| Can upload documents | Yes | Yes (on any case) |
| Can trigger the eligibility check | Yes | Yes |
| Can trigger the document validity check | Yes | Yes |
| Can trigger the lodgement date calculation (once qualification is eligible) | Yes | Yes |
| Sees total credited study weeks | No | Yes (on an eligible result) |
| Can mark a case reviewed | No | Yes |
