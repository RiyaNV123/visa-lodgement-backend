# Bug Report — Google Sheets migration + new qualifications flow

**Scope of this review:** the backend was migrated from SQLite to Google Sheets (via the same Apps Script Web App) since the last check-in, and the frontend qualifications flow was reworked (stream picker is back, added Preview step, Edit/Remove per qualification, a "Change stream / start again" replace flow, and a pending-file + batch-Save mechanism on the case-detail page). This report covers everything found by reading the changed code and then actually exercising it live against the real backend and the real deployed Apps Script/Google Sheet — not just static review. Every timing number below is a real measurement from this session, not an estimate.

Tested: student signup → stream choice → add qualification → preview → submit (case creation + document uploads) → case detail view → adding one more document via the pending/Save flow → staff signup (via script) → staff login → staff dashboard → staff case detail view → cross-student isolation → duplicate-document rejection → the "replace case" (change stream) endpoint.

---

## ✅ RESOLVED (mitigated) — Every read/write was extremely slow, and got worse as data grew

**Original measured timings** (before the fix below): signup ~3–4s, creating a case with 1 course **24.2s**, uploading one document ~25–30s, the admin dashboard with only 2 cases **~26s** — and the dashboard number specifically got worse per case added, not flat.

**Root cause:** every `sheet_store`/`Code.gs` operation did a full-table read with no caching, `initializeDataStore()` re-checked all 4 sheet tabs on every single call, ID generation re-scanned an entire table just to compute `max(id)+1`, and some endpoints (case list, admin dashboard) did an extra round-trip *per row* (N+1). On top of all that, a bare, minimal Apps Script round-trip was independently measured at **2.5–4.5 seconds regardless of how little work it does** — that's Google's inherent per-call overhead for Apps Script Web Apps, not something fixable in our code.

**Fix applied (two parts):**
1. **Backend caching** (`backend/app/sheet_store.py`): every table is now cached in-process for a 20-second TTL, invalidated immediately on any write made through this backend. This eliminates the N+1 pattern entirely (listing N cases no longer means N+1 identical table reads) and makes repeat requests within a session (e.g. clicking between pages) nearly free.
2. **`appsscript/Code.gs` rewrite** (requires redeploying the Web App — done): `tableSheet()` no longer checks all 4 tabs on every call; ID generation now uses a `LockService`-guarded counter in Script Properties instead of rescanning the table (this also closes the ID-collision race condition noted below); and three new batched actions (`createUser`, `createCaseWithCourses`, `uploadCourseDocument`) combine what used to be several sequential round-trips (duplicate-check + insert(s) + Drive folder/upload + case update) into one Apps Script execution each.

**Measured results after the fix** (same real backend, same real Sheet):
| Action | Before | After (cold cache) | After (warm cache, repeat request) |
|---|---|---|---|
| `GET /auth/me` (single simplest authenticated call) | ~3–4s | ~3.6s | **~0.12s** |
| Create a case, 2 courses (`POST /cases`) | 24.2s (1 course) | ~6–16s (variance from Apps Script's own cold-start jitter) | — |
| Upload a document | ~25–30s | ~13–20s | — |
| `GET /cases` (list) | ~26s w/ 2 cases | ~8–11s | **~0.11s** |

The remaining "after (cold cache)" numbers are dominated by the inherent ~2.5–4.5s-per-call Apps Script floor multiplied by however many distinct round-trips a given write still needs (typically 1–2 now, down from up to 8) — that floor is not further reducible without moving off Apps Script Web Apps as the RPC layer. The huge, consistent win is on **repeated reads within a short window** (any page navigation, any list reload), which went from multi-second to imperceptible. This is the realistic day-to-day experience improvement, since most usage isn't one isolated write in a vacuum.

**Known tradeoff to be aware of:** because tables are now cached for up to 20 seconds, a row added or edited **by hand directly in the Sheet** (e.g. following ADMIN_SETUP.md to add an admin) may take up to 20 seconds to be picked up by a running backend — writes made *through* the app invalidate the cache immediately and are always visible right away, this only affects out-of-band manual edits.

---

## 🟠 High — Once a document is uploaded, there is no way to fix it

Verified live: after "Completion Letter" shows **UPLOADED** on the case-detail page, that slot renders **only the status badge — no button of any kind**. There is no Replace, no Remove, no Delete.

This matches the backend on purpose: `POST /cases/{id}/courses/{cid}/documents` now returns **409 "This document is already saved. Only pending documents can be uploaded."** if a document of that `doc_type` already exists for the course (verified directly: first upload → 201, identical second upload → 409).

The backend *does* have a working `DELETE /cases/{case_id}/courses/{course_id}/documents/{document_id}` endpoint for exactly this purpose (verified directly: returned 204 and the document was gone) — **but nothing in the frontend calls it.** `DocUploadSlot.jsx`'s "uploaded" branch renders no interactive element at all.

**Impact:** if a student uploads the wrong file for a required document, there is currently **no way to correct it themselves** — not even by contacting support through the app, since staff have the exact same read-only "Uploaded" badge with no delete control either. The only fix today is someone manually deleting the row from the Google Sheet (and ideally the Drive file) by hand.

**Suggested fix:** add a small "Remove" action to the `uploaded` state in `DocUploadSlot.jsx` that calls the existing `DELETE` endpoint, then lets the slot fall back to `pending` so the student can re-upload. The backend work for this is already done.

---

## 🟡 Medium — No way to add a single new qualification to an existing case

Once a case is submitted, the only edit path visible anywhere in the UI is the **"Change stream / start again"** button on the case-detail page. This routes to `/case?changeStream=true`, which always restarts the qualifications builder at the **stream-choice screen** with an **empty** qualifications list (`CaseNew.jsx`'s `mode` state always initializes to `"stream"` and `qualifications` to `[]`, regardless of what was already saved).

There is no "add one more qualification" affordance anywhere for an already-submitted case — only per-existing-course document slots (which are also one-shot, per the bug above).

**Practical consequence:** a student who forgot one certificate has to click "Change stream / start again," re-pick their stream, re-add **every** qualification they already entered (the form doesn't pre-fill from their saved data), and re-select **every file from their computer again** — even documents that are already correctly sitting in Drive from the first submission. The old `Course`/`Document` rows are deleted from the Sheet by `replaceCaseQualifications` and replaced wholesale; nothing is merged or reused.

**Related data-hygiene note:** `replaceCaseQualifications` in `Code.gs` deletes the old `Courses`/`Documents` **rows**, but never deletes the corresponding files from **Google Drive**. Confirmed by reading the script — there is no `DriveApp` delete/trash call anywhere in the replace path. Every "start again" leaves the previously uploaded files as orphaned, unreferenced files sitting in the case's Drive folder. Not a functional bug (nothing breaks), but worth knowing before this accumulates — a staff member browsing the Drive folder directly will see stale duplicate files with no obvious link to current records.

---

## ✅ RESOLVED — No locking around ID generation (data-integrity risk)

`insertRow()` in `Code.gs` used to compute the next ID as `max(existing ids) + 1` by scanning the table, with no locking — a real (if unobserved) risk of two near-simultaneous inserts computing the same "next ID" and corrupting the `owner_user_id`/`case_id`/`course_id` relationships the backend relies on for access scoping.

**Fixed** as part of the performance work: `Code.gs` now generates IDs from a `LockService.getScriptLock()`-guarded counter in Script Properties (`nextId()`), and every insert/update/delete goes through lock-wrapped helpers (`insertRow`/`updateRow`/`deleteRow`, or the batched operations' own outer lock). This was a natural side effect of fixing the ID-generation performance problem, not a separate change.

---

## 🟢 Low / cosmetic

- **Doc-slot label wrapping:** in the 2-column-inside-2-column course-card grid, "Academic Certificate" wraps onto two lines inside its slot, making that card taller than its siblings in the same grid row. Confirmed via DOM measurement this is just text wrapping (no actual element overlap/collision), but it looks visually uneven. A `truncate` or a slightly wider minimum slot width would clean this up.
- **"Change stream" link disappears once qualifications.length > 0** on the list screen (by design, presumably to avoid losing in-progress entries) — worth confirming this is intentional, since it means a student who's added one qualification and wants to switch streams has to remove everything first rather than just switching.
- **Two duplicate `GET /cases` (and other) requests fire per page load** — visible in the network log as pairs of identical requests. This is very likely React 18 `<StrictMode>` double-invoking effects in development (harmless in prod builds) rather than an app bug, but given how expensive each `GET /cases` call is right now (Critical issue above), it's doubling real load against the Apps Script/Sheets quota in dev. Worth confirming this goes away in a production build (StrictMode's double-effect behavior is dev-only).

---

## ✅ Verified working correctly

- Student signup always forces `role=student`; there is still no way to self-provision a staff account.
- Cross-student case isolation: a second student's token gets a clean `404` on another student's case ID.
- The 4-document-per-course model (CoE/Completion Letter/Transcript/Academic Certificate) with required vs. optional enforcement.
- The full submit pipeline (case creation → sequential document uploads → real Google Drive files, with correct `"{Course Name} - {Doc Label}.{ext}"` naming) works end-to-end against the live deployed Apps Script — files really do land in Drive with working `webViewLink`s.
- The `PUT /cases/{id}/replace` endpoint itself functions correctly (stream and course list do get replaced, verified via direct API call) — the issue above is purely that the frontend never pre-fills it with existing data, not that the endpoint is broken.
- Staff dashboard and staff case-detail view show the correct data (owner email, stream, documents) once they finish loading, and correctly hide the "Change stream" control that only students should see.
- Duplicate-document upload correctly rejected with 409, and the underlying `DELETE` endpoint works correctly when called directly.
- Google Sheets did **not** appear to corrupt or reformat the ISO datetime strings (`created_at`, `uploaded_at`) in this testing — they came back parseable by Pydantic every time. Worth keeping an eye on if a *date-only* field (`start_date`/`end_date`) ever actually gets populated (still always empty today, per the open question already logged in `PROJECT_CONTEXT.md`), since Sheets' locale-based auto-formatting is more aggressive with plain dates than with full ISO timestamps.

---

## Remaining priority order

1. ~~Fix the latency (Critical)~~ — **done**, see above.
2. ~~Add locking around Sheet ID generation (Medium)~~ — **done**, see above.
3. **Add a Remove/Replace control for uploaded documents (High)** — cheap fix, backend already supports it.
4. **Decide on a real "add to / edit an existing case" flow (Medium)** — at minimum, pre-fill the qualifications builder with existing data when "Change stream / start again" is used, and consider whether full replace is really the intended model or whether an additive "add one more qualification" path is needed.
5. Cosmetic items whenever convenient.
