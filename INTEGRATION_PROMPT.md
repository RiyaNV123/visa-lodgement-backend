I'm attaching BUSINESS_LOGIC.md — the complete business-logic spec (plus verbatim,
executable Python source in its Appendix, §13) for a 485 visa eligibility/lodgement-date
calculator from another project. I want to integrate this exact logic into this project.

Read the whole file first — sections 1–12 are the spec (every rule, threshold, formula,
and why it exists), and §13 is the literal Python implementation of the three checks
(`eligibility.py`), the document-field regex extraction (`document_extract.py`), and the
CRICOS government registry lookup (`cricos_lookup.py`).

What I need you to do:

1. **Port the three core logic files from §13 as close to verbatim as possible.**
   These are pure functions with no I/O (except `cricos_lookup.py`'s one HTTP call) —
   they should drop into this project with minimal changes, just translated to this
   project's language/framework if it isn't Python. Do NOT change the constants,
   thresholds, formulas, regex patterns, or status/reason wording unless I explicitly
   ask — they're all confirmed business rules, not arbitrary choices.

2. **Wire them into this project's own data model and API**, adapting to what already
   exists here rather than copying the original project's specific schema:
   - Work out where a "Case"/application and its "Course"/qualification records should
     live in this project's existing data layer (see §2 for the exact fields needed).
   - Add whatever new fields this needs (§2.1–2.3) using this project's existing
     migration/schema convention.
   - Expose three endpoints (or equivalent) matching §1.2/§3/§4/§8: run the
     qualification check, run the document-validity check, run the lodgement-date
     calculation — each reading the upstream check's *already-stored* status (never
     recomputing it) to enforce the cascading gate described in §1.2, including the
     not_eligible-vs-pending blocked-message distinction.
   - Persist each check's "breakdown" (§7) alongside its status/reason, exactly as
     described in §2.4 — don't skip this, it's what lets the result survive a page
     reload without recomputing.

3. **Re-implement the self-healing re-extraction described in §9** if this project has
   its own document storage/upload flow — re-run extraction against an already-uploaded
   document whenever a needed field is still missing, rather than only extracting once
   at upload time.

4. **Match this project's own conventions** for auth, file storage, and error handling
   — the spec is intentionally silent on those since they're implementation details,
   not business rules. Use whatever this project already does.

5. **If you build a UI for this**, follow §10's auto-run model (no manual "Check X"
   buttons — each check runs automatically based on whether its breakdown/status still
   needs computing) unless I tell you otherwise for this project.

6. **Ask me before assuming** anything about this project's existing structure that
   isn't obvious from the codebase — e.g. where user-uploaded documents currently get
   stored, what auth pattern is already in use, or whether an existing "application" or
   "case" concept should be extended rather than creating a new one from scratch.

7. **Write tests** mirroring the rules in §3, §4, and §8 (boundary cases especially: the
   92-week threshold exactly, the 6-month window edge, an AFP Receipt vs Certificate, a
   CRICOS-registry miss, an invalid New CoE) before considering this done.

Don't start with any other refactor or cleanup beyond what's needed to land this
feature — this is meant to be a faithful port of the existing logic, not a rewrite or
improvement of it.
