# Deploy the Google Drive + Google Sheets Apps Script

This one Apps Script Web App stores all application records in Google Sheets and all uploaded files in Google Drive. No Google Cloud Console or service-account JSON key is used.

1. Create or choose a Google Drive folder for all student case folders. Copy its ID from `https://drive.google.com/drive/folders/<folder-id>`.
2. Create a blank Google Sheet for this app (for example, **485 Visa Calculator Data**). Copy its ID from `https://docs.google.com/spreadsheets/d/<sheet-id>/edit`.
3. Go to [script.google.com](https://script.google.com) using the company Google account that owns both the folder and Sheet. Create a project and replace its default code with [Code.gs](Code.gs).
4. Set the three constants at the top of `Code.gs`:
   - `PARENT_FOLDER_ID` — the Drive folder ID from step 1.
   - `SPREADSHEET_ID` — the Sheet ID from step 2.
   - `SHARED_SECRET` — a long private random string. It must match `APPS_SCRIPT_SECRET` in `backend/.env`.
5. Deploy it: **Deploy → New deployment → Web app**. Set **Execute as: Me** and **Who has access: Anyone**, then authorize both Drive and Sheets permissions.
6. Copy the deployed `/exec` URL into `backend/.env` as `APPS_SCRIPT_URL`. Put the matching secret in `APPS_SCRIPT_SECRET`.
7. Restart the FastAPI backend. On its first Sheets request, the script automatically creates four tabs with their headers: `Users`, `Cases`, `Courses`, and `Documents`.

Whenever `Code.gs` changes, update the Web App deployment (or create a new deployment) before testing. Saving the script alone does not update the live `/exec` endpoint.

## Important migration note

SQLite is no longer used by the running app. Existing records in a local `visa_calculator.db` remain on the computer only and are **not** automatically transferred. New signups and all future app records are created in the Google Sheet after the new script deployment.
