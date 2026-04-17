"""
agents/contract_reader.py — Reads management agreement PDFs from Google Drive
and uses Claude to extract key contract terms for Phase 2B routing.

Authentication uses the same credentials.json service account as the rest of
PropFlow, but with the drive.readonly scope added.
"""

import io
import json
import logging

import anthropic
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from config import (
    ANTHROPIC_API_KEY,
    ANTHROPIC_MODEL,
    GOOGLE_SHEETS_CREDENTIALS_FILE,
    PROPFLOW_DRIVE_FOLDER,
)

logger = logging.getLogger(__name__)

_DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# Returned on any failure so routing always continues without crashing.
_SAFE_DEFAULTS: dict = {
    "approval_threshold": None,
    "emergency_authority": None,
    "preferred_vendors": [],
    "notice_requirements": None,
}

_claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

_EXTRACT_SYSTEM_PROMPT = """\
You are a contract analyst for a property management company.
Read the management agreement text below and extract the following fields.
Respond with a single valid JSON object — no markdown, no code fences, no explanation.

{
  "approval_threshold":  number or null,   // Dollar amount above which client approval is required (digits only, no "$")
  "emergency_authority": number or null,   // Dollar amount the super can authorize without client approval in emergencies
  "preferred_vendors":   array of strings, // Vendor company names explicitly listed as preferred or approved
  "notice_requirements": string or null    // Notice period required before entering units or dispatching vendors (e.g. "48 hours")
}

If a field cannot be determined from the text, use null (or [] for preferred_vendors).
"""


def get_drive_service():
    """Builds and returns an authenticated Google Drive API v3 service object."""
    creds = Credentials.from_service_account_file(
        GOOGLE_SHEETS_CREDENTIALS_FILE, scopes=_DRIVE_SCOPES
    )
    return build("drive", "v3", credentials=creds)


def _resolve_folder_id(folder_path: str, drive_service) -> str | None:
    """
    Walks a slash-separated folder path (e.g. 'PropFlow/Management Agreements')
    from the Drive root and returns the terminal folder's ID.
    Returns None if any segment is not found.
    """
    parts = [p.strip() for p in folder_path.split("/") if p.strip()]
    parent_id = "root"

    for i, part in enumerate(parts):
        # For the first segment we cannot use 'root' in parents because
        # folders shared with a service account don't appear as root children
        # in the Drive API — they are only reachable without a parent filter.
        if i == 0:
            query = (
                f"name = {json.dumps(part)} "
                f"and mimeType = 'application/vnd.google-apps.folder' "
                f"and trashed = false"
            )
        else:
            query = (
                f"name = {json.dumps(part)} "
                f"and mimeType = 'application/vnd.google-apps.folder' "
                f"and '{parent_id}' in parents "
                f"and trashed = false"
            )
        try:
            resp = drive_service.files().list(
                q=query,
                spaces="drive",
                fields="files(id, name)",
                pageSize=1,
            ).execute()
        except HttpError as e:
            logger.error("Drive folder search failed for %r: %s", part, e)
            return None

        files = resp.get("files", [])
        if not files:
            logger.warning("Drive folder not found: %r (parent_id=%r)", part, parent_id)
            return None
        parent_id = files[0]["id"]

    return parent_id


def find_agreement(building_id: str, drive_service=None) -> str | None:
    """
    Searches the configured Drive folder for a PDF whose filename contains
    building_id (case-insensitive).

    Args:
        building_id: Building identifier e.g. "BLD001".
        drive_service: Optional pre-built Drive service (for testing).

    Returns:
        The Drive file ID of the first matching PDF, or None if not found.
    """
    if not building_id:
        return None

    if drive_service is None:
        drive_service = get_drive_service()

    folder_id = _resolve_folder_id(PROPFLOW_DRIVE_FOLDER, drive_service)
    if not folder_id:
        logger.warning(
            "Drive folder %r could not be resolved — skipping contract lookup.",
            PROPFLOW_DRIVE_FOLDER,
        )
        return None

    query = (
        f"name contains {json.dumps(building_id)} "
        f"and mimeType = 'application/pdf' "
        f"and '{folder_id}' in parents "
        f"and trashed = false"
    )
    try:
        resp = drive_service.files().list(
            q=query,
            spaces="drive",
            fields="files(id, name)",
            pageSize=1,
        ).execute()
    except HttpError as e:
        logger.error("Drive file search failed for building_id=%r: %s", building_id, e)
        return None

    files = resp.get("files", [])
    if not files:
        logger.info("No management agreement PDF found for building_id=%r.", building_id)
        return None

    file_id = files[0]["id"]
    logger.info(
        "Found management agreement for %r: %r (file_id=%s)",
        building_id, files[0]["name"], file_id,
    )
    return file_id


def extract_agreement_terms(file_id: str, drive_service=None) -> dict:
    """
    Downloads a Drive PDF, extracts its text with pypdf, and asks Claude to
    parse key contract terms from it.

    Args:
        file_id: Google Drive file ID of the management agreement PDF.
        drive_service: Optional pre-built Drive service.

    Returns:
        Dict with keys: approval_threshold (float|None), emergency_authority (float|None),
        preferred_vendors (list[str]), notice_requirements (str|None).
        Falls back to _SAFE_DEFAULTS on any failure.
    """
    if drive_service is None:
        drive_service = get_drive_service()

    # ── Download PDF bytes ────────────────────────────────────────────────────
    try:
        request = drive_service.files().get_media(fileId=file_id)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        buffer.seek(0)
    except HttpError as e:
        logger.error("Failed to download Drive file id=%s: %s", file_id, e)
        return dict(_SAFE_DEFAULTS)

    # ── Extract text from PDF ─────────────────────────────────────────────────
    try:
        from pypdf import PdfReader  # lazy import — not always installed in tests
        reader = PdfReader(buffer)
        page_texts = [page.extract_text() or "" for page in reader.pages]
        full_text = "\n\n".join(t for t in page_texts if t.strip())
    except Exception as e:
        logger.error("PDF text extraction failed for file_id=%s: %s", file_id, e)
        return dict(_SAFE_DEFAULTS)

    if not full_text.strip():
        logger.warning("PDF file_id=%s yielded no extractable text.", file_id)
        return dict(_SAFE_DEFAULTS)

    # Truncate to ~12 000 chars to stay comfortably within token limits
    truncated = full_text[:12000]

    # ── Ask Claude to extract contract terms ──────────────────────────────────
    try:
        response = _claude.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=512,
            system=_EXTRACT_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": truncated}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        terms = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("Claude returned non-JSON during contract extraction: %.200s", raw)
        return dict(_SAFE_DEFAULTS)
    except anthropic.APIError as e:
        logger.error("Anthropic API error during contract extraction: %s", e)
        return dict(_SAFE_DEFAULTS)

    # ── Normalise result — fill missing keys from safe defaults ───────────────
    result = dict(_SAFE_DEFAULTS)
    for key in result:
        if key in terms:
            result[key] = terms[key]

    # Ensure preferred_vendors is always a clean list of strings
    pv = result.get("preferred_vendors")
    if not isinstance(pv, list):
        result["preferred_vendors"] = []
    else:
        result["preferred_vendors"] = [str(v) for v in pv if v]

    logger.info("Contract terms extracted (file_id=%s): %s", file_id, result)
    return result


def get_contract_terms(building_id: str | None) -> dict:
    """
    Top-level entry point: locates and parses the management agreement for a
    building, returning its key contract terms.

    Args:
        building_id: Building identifier e.g. "BLD001". If None or empty,
                     safe defaults are returned immediately.

    Returns:
        Dict with keys:
          - approval_threshold (float|None)   — dollar threshold for client approval
          - emergency_authority (float|None)  — super's emergency spend authority
          - preferred_vendors (list[str])     — vendor names preferred by contract
          - notice_requirements (str|None)    — required notice before entry/dispatch
        Always returns safe defaults on any failure so routing continues.
    """
    if not building_id:
        return dict(_SAFE_DEFAULTS)

    try:
        drive_service = get_drive_service()
        file_id = find_agreement(building_id, drive_service)
        if not file_id:
            return dict(_SAFE_DEFAULTS)
        return extract_agreement_terms(file_id, drive_service)
    except Exception as e:
        logger.error(
            "get_contract_terms failed for building_id=%r: %s",
            building_id, e, exc_info=True,
        )
        return dict(_SAFE_DEFAULTS)
