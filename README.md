# 485 Visa Lodgement Calculator — Backend

FastAPI backend for the 485 Visa Lodgement Date Calculator: the qualification
duration/CRICOS eligibility check, document validity check, and lodgement
date calculation, backed by a Google Sheets data store (via the Apps Script
project in `appsscript/`).

Split out from the original combined `485-visa-lodgement-calculator` repo
into its own repo — this half is the backend + Apps Script data layer only.
The frontend lives in a separate repo: `485-visa-lodgement-frontend`.

## Running locally

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m uvicorn app.main:app --port 8000 --reload
```

Copy `.env.example` to `.env` and fill in the required values first.

## What's here

- `app/` — the FastAPI application (routers, business logic, Sheets store).
- `appsscript/` — the Google Apps Script project backing the Sheets data
  store (deploy this separately as a Web App; see `appsscript/README.md`).
- `BUSINESS_LOGIC.md` — full reference for every business rule (qualification
  duration/CRICOS, document validity, lodgement date), including the
  verbatim source of the pure calculation modules.
- `PROJECT_DOCUMENTATION.md` — the fuller project write-up (architecture,
  delivered features, roles).
- `ADMIN_SETUP.md`, `BUG_REPORT.md`, `PROJECT_CONTEXT.md` — supporting docs
  carried over from the original combined repo.
