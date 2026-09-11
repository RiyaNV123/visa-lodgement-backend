import base64

import httpx

from app.config import settings


class DriveServiceError(RuntimeError):
    pass


def call_apps_script(payload: dict) -> dict:
    if not settings.apps_script_url or not settings.apps_script_secret:
        raise DriveServiceError(
            "Apps Script integration is not configured. Set APPS_SCRIPT_URL and APPS_SCRIPT_SECRET in .env."
        )

    body = {**payload, "secret": settings.apps_script_secret}
    try:
        response = httpx.post(settings.apps_script_url, json=body, timeout=30.0, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise DriveServiceError(f"Apps Script request failed: {exc}") from exc

    data = response.json()
    if not data.get("ok"):
        raise DriveServiceError(data.get("error", "Unknown Apps Script error"))
    return data


def ensure_case_folder(case_name: str) -> str:
    """Create (or reuse) a Drive subfolder for a case. Returns the folder id."""
    data = call_apps_script({"action": "ensureFolder", "caseName": case_name})
    return data["folderId"]


def upload_file(folder_id: str, file_name: str, mime_type: str, content: bytes) -> tuple[str, str]:
    """Upload a file into a Drive folder. Returns (file_id, web_view_link)."""
    data = call_apps_script(
        {
            "action": "uploadFile",
            "folderId": folder_id,
            "fileName": file_name,
            "mimeType": mime_type,
            "dataBase64": base64.b64encode(content).decode("ascii"),
        }
    )
    return data["fileId"], data["webViewLink"]


def download_file(file_id: str) -> bytes:
    """Re-reads an already-uploaded file's own content back out of Drive --
    lets a document already on file be re-processed by extraction (e.g. after
    a regex fix) without asking the student to upload it again.
    """
    data = call_apps_script({"action": "downloadFile", "fileId": file_id})
    return base64.b64decode(data["dataBase64"])


def ocr_file(file_id: str) -> str:
    """Runs Drive's own OCR conversion against an already-uploaded file and
    returns the text it recovers -- the fallback for a scanned/image-based
    document that has no embedded text layer at all (extract_pdf_text on it
    comes back empty; there's nothing there for regex to search). Uses
    Drive's built-in OCR, not an external AI model.
    """
    data = call_apps_script({"action": "ocrFile", "fileId": file_id})
    return data["text"]
