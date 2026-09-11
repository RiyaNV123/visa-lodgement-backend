# Visa Lodgement Date Calculator — Project Context & Continuation Guide
**Purpose of this file:** a complete handoff so any AI model (or human) picking up this project can continue it correctly without re-deriving decisions already made, re-breaking bugs already fixed, or violating conventions the user has explicitly asked for. Read this fully before writing any code.

**Status: Phase 1 is complete** (user-confirmed), and the Critical performance issue found afterward has since been fixed/mitigated (see Section 8). This file was rewritten to describe the final, true state of the code — it replaces all earlier draft/in-progress versions of itself. Also see [BUG_REPORT.md](BUG_REPORT.md) (a live-tested audit, including real before/after performance numbers) and [ADMIN_SETUP.md](ADMIN_SETUP.md) (how admin accounts are provisioned).

---

## 1. What this product is

An internal tool for **ACME Migration** (a migration agency, domain `acmemigration.com`) to determine a student's eligibility and correct lodgement date for an Australian **subclass 485 visa** (Temporary Graduate visa), and to manage the supporting documents for that application.

Two roles, enforced server-side (not just UI hiding):

- **Students** self-signup, log in, pick their 485 stream, add their completed qualifications, and upload documents. Once eligibility logic exists (Phase 2/3), they will see only **eligible / not-eligible + a reason** — they must **never** see the calculated lodgement date.
- **Admins** (a handful of ACME Migration employees — this role was named "staff" earlier in the project and was renamed to "admin" at the user's request; if you see "staff" anywhere in old material, mentally read it as "admin") do **not** self-signup. Their accounts are provisioned by hand, directly in the Google Sheet (see [ADMIN_SETUP.md](ADMIN_SETUP.md)). Admins see **every** student who has signed up (not just ones with a submitted case), can open and review any case's documents, mark a case reviewed, and — once built — will see the **calculated lodgement date** in addition to everything a student sees.

The user runs this agency and is developing this tool **phase-by-phase**, reviewing and explicitly signing off each phase before the next begins. **Do not start work on a later phase without the user's explicit go-ahead**, even if it looks like the natural next step. Phase 1 just received that sign-off; Phase 2 has not.

---

## 2. The full original business-logic specification (verbatim intent, lightly organized)

This is the north-star spec for Phases 2–3. **None of the calculation logic below is built yet** — Phase 1 only built account/document collection. Nothing in this section should be treated as implemented unless Section 4 says so.

### Button 1 — Calculate Visa Application Lodgement Date

**Step 1 — Login.**

**Step 2 — Choose the 485 stream.**
Two options:
- *Post Vocational 485* (Certificate / Diploma) — a student may have done multiple certificates and/or diplomas; when multiple exist, always use the **latest** one for date-based logic.
- *Post Higher 485* (Bachelors / Masters) — always use the **latest** degree.

For each completed course, the student provides documents: **CoE** (may or may not exist), **Completion Letter**, **Transcript**, **Academic Certificate** (not critical).

Then:
- Sum the duration of all courses.
- **92-week rule:** if total completed study across all courses is **less than 92 weeks**, the student is **not eligible** — flag the reason and exit the flow.
- **CRICOS rule (vocational only):** compute the duration of all completed **certificates**, compare against the CRICOS-registered duration for those courses, and take whichever is **smaller**. If there is also a diploma, do the same comparison (diploma duration vs. its CRICOS duration, take the smaller) and **add** that to the certificate total.
- If the 92-week threshold is met → proceed.

**Step 3 — Check current visa.**
Ask for the student's current **Visa Grant Letter**.
- Current visa **must be subclass 500** (Student visa). If not → exit, flag "500 current visa required."
- Check the **"Must Not Arrive After" date / Length of Stay date** — it must be at least before today (the day the form is being filled). If not → flag and exit.
- If OK → ask for **OVHC, AFP Certificate, AFP Receipt, PTE**. If any of these documents is missing, flag "documents absent."

**Step 4 — 6-month application window.**
After the **latest course's completion date**, the student has **6 months** to apply.
- If today is more than 6 months after that completion date → exit, flag the reason.
- If today falls within the 6-month window → eligible, proceed.

**Step 5 — Finalise the lodgement date.**
Three factors are compared:
1. **SV Expiry** = the "Must Not Arrive After" date / Length of Stay date.
2. **Completion Date + 6 months.**
3. **60-Days/CoE rule** — compare the (new) CoE's start date against (previous course completion date + 60 days); take whichever is **later**. The CoE must be **valid**: its start date must fall **after** the last course's completion date — otherwise the CoE is invalid, flag and exit.

If the student completed their course **before** their SV expiry date, they can either get a new CoE or start another course to bridge the gap — in practice most choose a new CoE (a new course is expensive).

**Final calculation:** among the three dates below, pick the one **closest to today**, and add a **2–3 day buffer**:
1. SV expiry.
2. Completion date + 6 months.
3. A new CoE applied for within 60 days of the previous course's completion — the resulting date is **one day before** the new CoE's start date.

**Visibility rule (critical): the final lodgement date must be shown to admins only — never to students.** Students only ever see an eligible/not-eligible verdict with a reason.

### Button 2 — Rechecking Documents Before Lodging

- Check that all required documents are present: **OVHC, AFP Certificate, AFP Receipt, PTE, Qualifications**. If any is missing → flag.
- If all present → check that **every document's relevant date is before today**. If not → flag.

---

## 3. Phase plan and status

| Phase | Duration | Deliverables | Status |
|---|---|---|---|
| 1 | 2 days | Login/Signup, course document uploads (stored in Drive) | ✅ **Complete — user sign-off received.** |
| 2 | 3 days | Duration check, CRICOS comparison, eligibility, final durations stored in Google Sheets | **Not started.** Blocked on an open question — see Section 7. The performance issue that used to block this too has since been fixed (Section 8) — keep the same batching/caching instincts when adding new logic. |
| 3 | 4 days | Visa check eligibility, document upload (AFP/PTE/OVHC), finalising lodgement (3-factor calc) | Not started. |
| 4 | 1 day | Document re-verification (Button 2 above) | Not started. |
| 5 | 1 day | Final testing | Not started. |

**Working rule:** the user reviews and signs off each phase in the running app before the next phase begins. Never silently start Phase 2+ work.

---

## 4. What's actually built (Phase 1 — final state)

### Stack
- **Frontend:** React 18 + Vite + Tailwind CSS v4 (`@tailwindcss/vite`) + `react-router-dom` v6 + `axios`.
- **Backend:** Python + FastAPI. **Persistence is Google Sheets, not a database** — there is no SQL/SQLite/SQLAlchemy anywhere in the current code (it was fully replaced mid-Phase-1; if you see references to `db.py`, `DATABASE_URL`, or `visa_calculator.db` anywhere, that's stale history — ignore it).
- **Auth:** JWT (`python-jose`) + `passlib`/`bcrypt` password hashing.
- **File + data storage:** one company-owned **Google Sheet** (4 tabs: `Users`, `Cases`, `Courses`, `Documents`) plus a company-owned **Google Drive** folder, both accessed through a single **Google Apps Script Web App** (`appsscript/Code.gs`) that the backend calls over HTTP with a shared-secret token. No GCP service account/JSON key anywhere (deliberate — the user rejected that setup path). **This is deployed and working against the user's real Google account.**

### Repo layout
```
backend/
  app/
    main.py                 # FastAPI app, CORS (reads CORS_ORIGINS, comma-separated), mounts all 3 routers
    config.py                # pydantic-settings: JWT_SECRET, APPS_SCRIPT_URL, APPS_SCRIPT_SECRET, CORS_ORIGINS
    models.py                 # shared enums (UserRole: student|admin, Stream, CourseType, DocType) + a small User dataclass
    schemas.py                 # Pydantic request/response models
    auth.py                     # hash/verify password, create/verify JWT, get_current_user, require_admin
    drive_service.py            # httpx client wrapping the Apps Script Web App (ensure_case_folder, upload_file, call_apps_script)
    sheet_store.py               # the "database layer" -- rows/insert/update/delete against the 4 Sheet tabs via Apps Script,
                                  # with a 20s in-process cache per table (see Section 8's performance write-up)
    routers/
      auth_router.py             # POST /auth/signup (student-only, always forces role=student), POST /auth/login, GET /auth/me
      cases_router.py            # POST /cases, GET /cases, GET /cases/{id}, POST /cases/{id}/review,
                                  # PUT /cases/{id}/replace, POST /cases/{id}/courses,
                                  # POST /cases/{id}/courses/{course_id}/documents,
                                  # DELETE /cases/{id}/courses/{course_id}/documents/{document_id}
      admin_router.py             # GET /admin/students -- every student, with their case summary if they have one
  create_admin.py             # interactive CLI to create a role=admin user (see ADMIN_SETUP.md)
  hash_password.py             # prints a bcrypt hash for a password, to paste into the Sheet by hand
  requirements.txt
  .env / .env.example           # JWT_SECRET, APPS_SCRIPT_URL, APPS_SCRIPT_SECRET, CORS_ORIGINS
frontend/
  src/
    main.jsx, App.jsx, index.css      # routing table, Tailwind + Manrope/Inter font tokens
    api/client.js                      # axios instance; attaches JWT; hard-redirects to /login on 401
    context/AuthContext.jsx            # user state, login/signup/logout, fetch /auth/me on mount
    components/AppShell.jsx            # sticky sidebar + sticky header w/ Logout button top-right
    components/DocUploadSlot.jsx       # doc control used on CaseDetail.jsx (see Section 4's UI description)
    components/Spinner.jsx             # <Spinner/> (small inline spinner) + <PageLoader/> (spinner+label, full-width) -- use these for every loading state, don't fall back to plain "Loading…" text
    pages/Login.jsx, Signup.jsx        # auto-redirect away if already authenticated; use replace navigation
    pages/MyCase.jsx                   # route /case -- stream choice + qualifications builder entry point
    pages/CaseNew.jsx                  # the qualifications builder (stream → add/edit qualifications → preview → save)
    pages/MyCaseUploads.jsx            # route /case/uploads -- student's own case, document status + retry uploads
    pages/CaseDetail.jsx               # shared case view: student's own case AND admin's view of any case
    pages/Dashboard.jsx                # admin-only route /cases -- every student (not just ones with a case), with review status
  package.json                        # NOTE: "allowScripts": {"esbuild@0.21.5": true} is required, do not remove
  .env / .env.example                 # VITE_API_BASE_URL
appsscript/
  Code.gs                # doPost(e) router: Drive actions (ensureFolder/uploadFile) + generic Sheet CRUD
                          # (initializeDataStore/dataGet/dataInsert/dataUpdate/dataDelete) + replaceCaseQualifications
  README.md               # deploy steps (script.google.com, no GCP Console) -- needs PARENT_FOLDER_ID, SPREADSHEET_ID, SHARED_SECRET
BUG_REPORT.md             # live-tested audit -- read the Critical performance section before Phase 2
ADMIN_SETUP.md            # exact steps to add an admin account by hand in the Sheet
```

### Data model (4 Google Sheet tabs, columns in this exact order)
- **Users** — `id, email, hashed_password, full_name, role (student|admin), created_at`
- **Cases** — `id, owner_user_id, student_name, stream (vocational|higher), status (draft|reviewed), drive_folder_id, created_at`
- **Courses** — `id, case_id, name, course_type (certificate|diploma|bachelors|masters), start_date, end_date, cricos_weeks, sort_order`
- **Documents** — `id, course_id, doc_type (coe|completion_letter|transcript|academic_certificate), file_name, drive_file_id, drive_view_link, uploaded_at`

**Important open item:** `Courses.start_date`, `end_date`, and `cricos_weeks` exist in the schema but are **never populated by the current UI** — Phase 2 needs this data and it doesn't exist yet. See Section 7.

**Also important:** every one of these Sheet columns expects a real value in the right shape — `created_at`/`uploaded_at` must be a valid ISO datetime string (use `sheet_store.now()`), never an empty string. An empty `created_at` on a Cases row causes a genuine 500 (Pydantic fails to parse `""` as a datetime) the next time that case is read back through `GET /cases`, `GET /cases/{id}`, or `GET /admin/students`. This was hit once during testing (self-inflicted, from a row inserted directly via a script rather than through the app) — the real app always writes a valid timestamp, but if you ever insert/update a row directly (testing, migrations, admin tooling), always pass a real `now()` value, never `""`.

### Roles and access control
- `POST /auth/signup` **always** creates `role=student`; posting `role: "admin"` in the body is silently ignored. There is **no public way to create an admin account.**
- Admin accounts are provisioned by hand — either by directly adding a row to the Sheet's Users tab (using `hash_password.py` to get a valid bcrypt hash to paste in) or by running `create_admin.py` interactively. Full walkthrough in [ADMIN_SETUP.md](ADMIN_SETUP.md). **The `role` column must be exactly the string `admin`** — anything else (a typo, `Admin`, the old `staff`) breaks that row: `UserRole(row["role"])` raises on read, which 500s any request that needs to load that user (their own login, or an admin dashboard row referencing them as a case owner).
- `GET /cases` returns only the caller's own case(s) for students, and **all** cases for admins.
- `GET /cases/{id}` and nested routes 404 (not 403) if a student tries to access another student's case.
- A student may only ever have **one** case (enforced with a 409 on a second `POST /cases`) — see the "Change stream / start again" flow below for how an existing case gets modified instead.
- `GET /admin/students` (admin-only) returns **every** `role=student` user, each with a `case` field that's either `null` (no case yet) or a small case summary (`id, stream, status, course_count, created_at`). This is what makes every signed-up student visible to admins, not just ones who finished a case — a real gap the user caught and asked to be fixed.
- `POST /cases/{id}/review` (admin-only) sets that case's `status` to `"reviewed"`.

### The student flow (current, final)
1. Sign up (always creates a student) or log in.
2. Land on `/case`. If no case exists yet: pick a stream — **Post Vocational 485** (Certificate/Diploma) or **Post Higher 485** (Bachelors/Masters). This choice restricts which course types can be added next (no mixing vocational and higher course types in one case).
3. **Qualifications builder** (`CaseNew.jsx`): click **"+ Add Qualification"** → a form with the course type (limited to the chosen stream) plus 4 document pickers (CoE, Completion Letter, Transcript, Academic Certificate — Completion Letter and Transcript required, the other two optional). **Back** cancels without saving; **Done** saves it into a client-side list and returns to the list, where existing entries can be **Edit files**'d or **Remove**'d, and more can be added via the same **+** button.
4. Once every qualification has its required docs, **Preview documents** shows every qualification and every file's ready/pending state for a final check.
5. **Save** creates the case (`POST /cases`, stream sent explicitly — it is *not* auto-derived from course types) and then uploads every attached file sequentially. If an individual document upload fails, the case is still created and the student still lands on the next screen — that one slot just stays **Pending**, retryable there.
6. Lands on `/case/uploads` → `CaseDetail.jsx`: student name, stream badge, a **Results placeholder** (says eligibility isn't available yet — Phase 2/3 will render real output here, gated so only admins ever see a lodgement date), and course cards in a responsive 2-column grid, each with a 2×2 grid of the 4 document slots.
7. **"Change stream / start again"** (visible to students only) sends them back to the stream-choice screen. Submitting from there calls `PUT /cases/{id}/replace`, which replaces all of that case's `Courses`/`Documents` Sheet rows in one shot. **The old Drive files are deliberately not deleted** — they stay in the case's Drive folder as a recovery trail even though the Sheet no longer references them. There is currently no way to add a single extra qualification to an existing case without going through this full replace — that's a known limitation, not a bug (see BUG_REPORT.md).

### Document-slot behavior (`components/DocUploadSlot.jsx`, used on `CaseDetail.jsx`) — read carefully, this is easy to get wrong
This component has **three states**, and the controls shown are deliberately different in each:
- **Pending** (no document uploaded, no local file chosen): shows one **Upload** button.
- **Ready** (a local file has been chosen but not yet saved): shows **Preview** (opens the local file via `URL.createObjectURL`) and **Remove** (clears the local selection, back to Pending).
- **Uploaded** (a document already exists on the case): shows the real filename and a **Preview** button that opens the actual Drive file (`document.drive_view_link`) in a new tab. **There is deliberately no Replace/Remove button here** — the backend rejects a second upload of the same `doc_type` for the same course with a 409 ("This document is already saved. Only pending documents can be uploaded."), and there's no delete control wired into this UI (the backend DELETE endpoint exists and works, it's just not called from anywhere in the current UI). If a student uploads the wrong file, there is currently no self-service fix — flagged in BUG_REPORT.md as a real gap, not yet resolved.
- New documents on an already-saved case are added via a **pending-files-then-batch-save** pattern: choosing a file for any Pending slot adds it to a running "N new documents ready to save" bar pinned to the bottom of the page; clicking **Save** there uploads all of them in sequence and reloads the case.
- This exact same `CaseDetail.jsx` component, with the exact same document-slot behavior including the Preview buttons, is what renders when an **admin** opens a case from the dashboard (`/cases/:caseId`) — there is no separate admin-only document view. The only things that actually differ for an admin viewing this page: the "Change stream / start again" button is replaced with a **"Done — mark as reviewed"** button (or a green "Reviewed" badge + "Back to All Cases" if already reviewed), and the student's email is shown next to the stream badge.

### The admin flow (current, final)
1. Log in (no signup path — see Roles above).
2. Land on `/cases` (`Dashboard.jsx`): a table of **every signed-up student**, each row showing their case's stream (or a gray "No case yet" badge if they haven't started one), a **Review status** badge (green "Reviewed" / amber "Needs review" — blank "—" if there's no case to review yet), course count, signup date, and a **View** button (only present if they have a case).
3. **View** opens `/cases/:caseId` → the same `CaseDetail.jsx` described above: course cards, document status, Preview links to the real Drive files.
4. After checking the documents, **"Done — mark as reviewed"** calls `POST /cases/{id}/review` and navigates straight back to `/cases` — so the loop is: check a case → Done → back to the list → click the next student → repeat. This was a specific, explicit user request for the review workflow.

### Loading states
Every page-level fetch and every submit/save action shows a visible spinner (`components/Spinner.jsx`'s `<Spinner/>` inline or `<PageLoader/>` full-block), not just static text. This matters more than it sounds: see the Critical performance issue below — some of these loads take 20–30+ seconds against the real Sheets backend, and a plain "Loading…" text label was genuinely mistaken for a broken/frozen page before spinners were added. Don't remove them or revert to bare text.

---

## 5. Design history / why the UI looks the way it does (read this before "improving" it back)

The case-creation UI went through **four iterations** based directly on user feedback. Do not revert to an earlier shape without a new, explicit instruction:

1. **v1:** Stream picker → manual per-course fields (name, type, start date, completion date, CRICOS weeks) typed by hand.
   → User said: don't ask for dates/CRICOS manually, that should come from the documents; go straight from stream choice to uploads.
2. **v2:** Stream picker → "Number of Certificates" + a single "I also completed a Diploma" checkbox → uploads.
   → User said: diplomas can be **multiple** — needs its own count field, defaulting to 0, not a checkbox.
   → Also fixed in this iteration: (a) browser Back button dumped the user onto a stale login page, (b) the count `<input type="number">` couldn't be cleared to type a new value (classic controlled-input clamp-to-1-on-every-keystroke bug), (c) excessive vertical scrolling, (d) the Logout button was pinned to the bottom of a sidebar that stretched to page height.
3. **v3:** No stream picker, no count fields at all — straight to a qualifications list where the student adds one qualification at a time (course type + 4 docs), **Done**, repeat, then one final **Submit**.
4. **v4 (current):** The stream picker was brought back, positioned *before* the v3 qualifications workflow (so the course-type dropdown can be restricted to the chosen stream), and a **Preview documents** step was inserted before the final **Save**. This is the design described in Section 4.

Fixes made along the way that must **not** be regressed:
- `Login.jsx` / `Signup.jsx` redirect away immediately (`<Navigate replace>`) if already authenticated, and use `navigate(path, {replace: true})` after a successful auth — this is what stops the browser Back button from ever showing a stale auth form.
- `/case` (setup) and `/case/uploads` are **separate routes**, not one route with internal-only state — real browser history entries so Back behaves sensibly.
- Logout lives in the **sticky header, top-right**, always visible without scrolling; the sidebar is `sticky`, not a full-height flex child that stretches with page content.
- Any numeric field must track its own raw string state and only clamp on blur or a fully-valid keystroke — never clamp an empty string back to a default on every keystroke.
- Course cards render in a responsive grid (`lg:grid-cols-2`) with a 2×2 grid of document slots inside each card — don't collapse back to a single vertical stack, it was a deliberate fix for excessive scrolling.
- Document controls are **Preview + Remove** (pre-save) or **Preview only** (post-save) — never a "Replace" button, and never silently allow re-uploading an already-saved `doc_type`.

---

## 6. UI style guide (hard constraint, not a suggestion)

Given in full by the user near the start of the project. Match it exactly for anything new:
- Fonts: `font-headline` = Manrope (headings, strong labels), `font-body`/`font-label` = Inter (body, form labels).
- Colors: navy `#002d48` primary, warm off-white `#faf9f6` page background, white surfaces, muted grays for secondary text, orange `#ff8f37` for attention/left-border accents, green/red/blue tint pairs for success/error/info badges.
- `rounded-xl` for major cards, `rounded-full` for pills/badges, shallow shadows (`shadow-[0px_20px_40px_rgba(27,67,97,0.06)]`).
- Uppercase, wide-tracking micro-labels for metadata (`text-[10px] font-bold uppercase tracking-widest`).
- Sticky table headers, compact `px-6 py-4` cells, tables allowed to scroll rather than compress.

---

## 7. Open question that blocks Phase 2 — resolve with the user before starting

Phase 2 needs, per course: **start date, end date/completion date, and CRICOS-registered duration (weeks)** to do the 92-week and CRICOS comparisons from Section 2.

The user removed all manual date/duration entry from the UI early on and said this data should be **"extracted from the document"** rather than typed by hand — which implies some form of **document parsing / OCR** reading these values off the uploaded CoE/Completion Letter PDFs. That's a materially bigger feature than plain date arithmetic and isn't part of the original 5-phase scope as written.

**The user said explicitly they'd revisit this "after phase 1 gets completed."** Phase 1 is now complete — this conversation needs to happen before Phase 2 implementation starts. At minimum, resolve:
- Automated OCR/document-AI extraction, vs. an admin manually entering these fields while reviewing a case (admins already open every case via the new review workflow in Section 4 — that's a natural place to add manual date entry if OCR is out of scope for now).
- If OCR: acceptable error/fallback behavior when extraction fails or is ambiguous, and who corrects it.
- `Courses.start_date`/`end_date`/`cricos_weeks` already exist as nullable Sheet columns, so no schema change is needed once the mechanism is decided.

**Do not start Phase 2 implementation until this is resolved with the user.**

---

## 8. Known gotchas and open issues — read before debugging from scratch

### ✅ Performance — was Critical, now fixed/mitigated (read this before adding Phase 2 logic anyway)
The Google Sheets backend used to be severely slow and scaled *worse* as data grew (creating a case: 24s; a document upload: ~25-30s; the admin dashboard with 2 cases: ~26s). This has been substantially fixed — see the full before/after numbers in [BUG_REPORT.md](BUG_REPORT.md)'s now-`✅ RESOLVED` sections. Summary of the fix:
- `backend/app/sheet_store.py` now caches every Sheets table **in-process for a 20-second TTL**, invalidated immediately on any write made through the backend. This kills the N+1 pattern (listing N cases no longer re-reads the same table N times) and makes repeat requests within a session **nearly instant** (~0.1s vs. 3-4s, measured).
- `appsscript/Code.gs` was rewritten: ID generation now uses a `LockService`-guarded counter instead of rescanning a table (also fixes the ID-collision race condition that used to be listed here), `tableSheet()` no longer re-checks all 4 tabs on every call, and three new batched actions (`createUser`, `createCaseWithCourses`, `uploadCourseDocument`) collapse what used to be several sequential round-trips into one each. **This required a Web App redeployment, which has been done** — if you ever revert or branch from an older copy of `Code.gs`, you must redeploy again for these actions to exist.
- **The remaining floor**: a bare Apps Script round-trip costs 2.5-4.5 seconds no matter how little work it does — this is inherent to Google's infrastructure, not fixable from our code. A genuinely *cold* write (first request in a while) still takes several seconds because it still needs 1-2 such round-trips. The huge win is specifically on repeated/warm requests, which is most of real usage.
- **New tradeoff to know about:** a row edited **by hand directly in the Sheet** (e.g. adding an admin per ADMIN_SETUP.md) can take up to 20 seconds to be picked up by a running backend, since that edit doesn't go through the cache-invalidating write path. Writes made through the app are never stale.

Phase 2 will still add more logic per case — keep the same instincts (avoid new N+1 loops, batch multi-step Apps Script operations, reuse data you already fetched) rather than assuming this problem is gone forever.

### Other gotchas
1. **`passlib==1.7.4` is incompatible with `bcrypt>=4.0`.** Symptom: signup/login crashes on the very first password hash with a misleading `ValueError: password cannot be longer than 72 bytes...`. Fix already applied: `requirements.txt` pins `passlib==1.7.4` + `bcrypt==3.2.2`. Don't let a dependency upgrade silently bump bcrypt.
2. **`pydantic.EmailStr` needs the `email-validator` package** — `requirements.txt` has `pydantic[email]`, keep it that way.
3. **`uvicorn --reload` spawns a child worker process** that does not die when you kill the parent/reloader PID. If a restart doesn't seem to take effect, check for orphaned `python.exe` processes still bound to port 8000 (`Get-CimInstance Win32_Process -Filter "Name='python.exe'"` on Windows) and kill them explicitly.
4. **Windows `netstat` can show stale "LISTENING" entries for PIDs that no longer exist.** Cross-check with `Get-Process`/`Get-CimInstance` before concluding a port is occupied or free.
5. **`CORS_ORIGINS` in `backend/.env` must list every port Vite might land on** (`http://localhost:5173,http://localhost:5174,http://localhost:5175,http://localhost:5176`). A mismatch shows as a browser CORS error, not an obvious backend log line — check this first when signup/login "stops working" after a restart.
6. **`create_admin.py` and `hash_password.py` both use `getpass.getpass()`**, which reads directly from the Windows console and ignores piped/redirected stdin. Must be run in a real interactive terminal — piping input in will hang forever.
7. **A Sheets row with an empty/malformed `created_at` on a Cases row causes a real 500** the next time it's read (Pydantic can't parse `""` as a datetime). Always write a real `sheet_store.now()` value, never `""`, on any Cases insert/update — including ad-hoc scripts/testing.
8. **The `role` column on a Users row must be exactly `student` or `admin`.** Any other value (typo, wrong case, the old `staff`) breaks that specific row on read — see Section 4.
9. **`.env` files are gitignored** and contain real secrets (a real `APPS_SCRIPT_URL` pointing at the user's actual deployed script, `APPS_SCRIPT_SECRET`). Re-read before editing — a human may have changed it deliberately.
10. **`frontend/package.json` has `"allowScripts": {"esbuild@0.21.5": true}`** — required for Vite to work on Windows (esbuild's postinstall fetches its platform binary). Don't remove it.
11. **No locking around Sheet ID generation** in `Code.gs` (`insertRow` computes `max(id)+1` with no `LockService`). Not yet observed causing a real collision, but the severe per-request latency above widens the race window. Flagged in BUG_REPORT.md as a should-fix.
12. **A student uploading the wrong file has no self-service fix** — see Section 4's "Uploaded" state. The backend DELETE endpoint works; nothing in the UI calls it yet.

---

## 9. Working conventions the user has established (follow these without being asked again)

- **Phase-wise, with sign-off.** Finish and get explicit approval on the current phase in the running app before starting the next.
- **Ask before big architecture calls** the user hasn't specified (stack, auth model, storage mechanism, role model), but don't over-ask on execution details — make a reasonable call and keep moving.
- **Match the style guide exactly** (Section 6) — don't freelance a different visual style.
- **Bias toward fewer fields, fewer steps, fewer clicks.** Nearly every UI iteration in this project has been the user asking to *remove* something, not add it.
- **Resilient, forgiving flows.** A partial failure (one document out of several failing to upload) should never block the rest of an operation, and should always leave a retry path.
- **Verify live in the browser and via direct API calls, not just by reading code.** Multiple real bugs here (CORS misconfiguration, the back-button/login bug, the unclearable number input, a 500 from an empty `created_at`, orphaned dev-server processes) were only caught by actually driving the app end-to-end against the real backend.
- **When testing creates data in the user's real Google Sheet, clean it up afterward** — delete every test account/case/course/document you created, and double-check you haven't touched the user's real rows (their real accounts and cases exist alongside your test data in the same live Sheet — there is no separate test environment). If you're not sure whether a row is real or test data, ask rather than guess before deleting *or* before leaving it behind.
- **Never commit secrets.** `.gitignore` excludes `.venv/`, `node_modules/`, `.env` (both frontend and backend).

---

## 10. How to run this project locally

```bash
# Backend
cd backend
python -m venv .venv
source .venv/Scripts/activate   # Windows Git Bash; use .venv\Scripts\Activate.ps1 for PowerShell
pip install -r requirements.txt
cp .env.example .env            # fill in real APPS_SCRIPT_URL / APPS_SCRIPT_SECRET if not already present
uvicorn app.main:app --reload
```

```bash
# Frontend (separate terminal)
cd frontend
npm install
cp .env.example .env
npm run dev
```

To create an admin account, see [ADMIN_SETUP.md](ADMIN_SETUP.md) — either add a row to the Sheet by hand (using `hash_password.py` for the password hash) or run `python create_admin.py` interactively (must be a real terminal, not piped input).

To (re)deploy the Apps Script (Drive + Sheets integration), follow `appsscript/README.md` — no Google Cloud Console involved, just script.google.com. Remember: **any change to `Code.gs` requires a new Web App deployment to take effect** — saving the script alone does not update the live `/exec` endpoint.

---

## 11. Immediate next step for whoever picks this up

1. The performance issue is fixed — no action needed there beyond keeping the same instincts (Section 8) when writing new Phase 2 code.
2. Have the date/CRICOS-data-source conversation from Section 7 and get an explicit decision — do not guess.
3. Only then implement Phase 2: per-course duration calculation, the 92-week total check, the CRICOS comparison rules (Section 2), eligibility flagging with a reason, and syncing final durations to Google Sheets via the **same** Apps Script deployment (add a new action to `Code.gs` rather than a separate integration).
