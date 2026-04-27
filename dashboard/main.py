"""
dashboard/main.py — PropFlow Mission Control dashboard API.

Read-only FastAPI service that exposes Google Sheets data for the
Mission Control frontend. Does not write to Sheets or Gmail.

Run locally:
    uvicorn dashboard.main:app --reload --port 8000

On Railway: web process in Procfile.
"""

import sys
import os
import logging
import time
from datetime import datetime, timezone, timedelta
from functools import wraps
from typing import Any

# ── Path setup: allow importing propflow's shared modules ─────────────────────
# dashboard/ lives inside the propflow package root; add it to sys.path
# so "from sheets_client import ..." resolves correctly whether this module
# is run from the repo root or from inside dashboard/.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from sheets_client import get_sheets_service
from config import (
    GOOGLE_SPREADSHEET_ID, GOOGLE_SHEET_NAME, GOOGLE_PENDING_SHEET,
    GOOGLE_SHEETS_CREDENTIALS_FILE, GMAIL_USER_EMAIL,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Railway credentials bootstrap ────────────────────────────────────────────
# On Railway, credentials.json is not committed — it is base64-encoded in the
# GOOGLE_CREDENTIALS_B64 env var and written to disk on startup.
_b64 = os.environ.get("GOOGLE_CREDENTIALS_B64")
if _b64:
    import base64 as _b64mod
    _cred_path = os.path.join(_ROOT, "credentials.json")
    with open(_cred_path, "wb") as _f:
        _f.write(_b64mod.b64decode(_b64))
    logger.info("credentials.json written from GOOGLE_CREDENTIALS_B64.")
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="PropFlow Mission Control", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ── Serve the static frontend ─────────────────────────────────────────────────
_STATIC = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=_STATIC), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(os.path.join(_STATIC, "index.html"))


# ── Sheet cache (30s TTL) ─────────────────────────────────────────────────────
_cache: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 30  # seconds


def _cached_read(tab_name: str) -> list[dict]:
    """
    Reads a sheet tab and caches the result for CACHE_TTL seconds.
    Returns list of dicts (first row = headers). Empty list on any error.
    """
    now = time.monotonic()
    if tab_name in _cache:
        ts, data = _cache[tab_name]
        if now - ts < _CACHE_TTL:
            return data

    try:
        service = get_sheets_service()
        resp = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=GOOGLE_SPREADSHEET_ID, range=f"{tab_name}!A:Z")
            .execute()
        )
        values = resp.get("values", [])
        if not values or len(values) < 2:
            _cache[tab_name] = (now, [])
            return []

        headers = [h.strip().lower().replace(" ", "_") for h in values[0]]
        rows = []
        for row in values[1:]:
            padded = row + [""] * (len(headers) - len(row))
            rows.append(dict(zip(headers, padded)))

        _cache[tab_name] = (now, rows)
        return rows
    except Exception as e:
        logger.error("Error reading sheet tab %r: %s", tab_name, e)
        return []


def _tenant_rows() -> list[dict]:
    return _cached_read(GOOGLE_SHEET_NAME)


def _pending_rows() -> list[dict]:
    return _cached_read(GOOGLE_PENDING_SHEET)


_requests_cache: tuple[float, list] | None = None


def _build_requests_with_super_threads() -> list[dict]:
    """
    Joins the Tenant Requests tab with the Pending Requests tab to attach
    super_thread_id to each tenant request row.

    Match key: Pending tab's tenant_requests_row (column J) is the 1-based
    sheet row number of the corresponding Tenant Requests row. Tenant rows
    from the sheet start at row 2 (row 1 = headers), so row index i (0-based)
    corresponds to sheet row i+2.

    Cached for 30 seconds alongside the sheet data.
    """
    global _requests_cache
    now = time.monotonic()
    if _requests_cache is not None:
        ts, data = _requests_cache
        if now - ts < _CACHE_TTL:
            return data

    tenant = _tenant_rows()
    pending = _pending_rows()

    # Build a lookup: sheet_row_number → super_thread_id
    # Use the most recent pending row if there are duplicates (e.g. re-opened tickets)
    super_thread_map: dict[str, str] = {}
    for p in pending:
        row_ref = (p.get("tenant_requests_row") or "").strip()
        tid = (p.get("super_thread_id") or "").strip()
        if row_ref and tid and not tid.startswith("dry_run_"):
            super_thread_map[row_ref] = tid

    result = []
    for i, r in enumerate(tenant):
        sheet_row = str(i + 2)  # row 1 = headers, data starts at row 2
        result.append({
            "timestamp":        r.get("timestamp", ""),
            "tenant_name":      r.get("tenant_name", ""),
            "tenant_email":     r.get("tenant_email", ""),
            "building_address": r.get("building_address", ""),
            "unit_number":      r.get("unit", "") or r.get("unit_number", ""),
            "issue_type":       r.get("issue_type", ""),
            "issue_description":r.get("description", "") or r.get("issue_description", ""),
            "urgency":          r.get("urgency", ""),
            "status":           r.get("status", ""),
            "vendor_assigned":  r.get("vendor_assigned", ""),
            "super_notified":   r.get("super_notified", ""),
            "approval_required":r.get("approval_required", ""),
            "thread_id":        r.get("thread_id", ""),
            "review_flag":      r.get("review_flag", ""),
            "super_thread_id":  super_thread_map.get(sheet_row, ""),
        })

    result.sort(key=lambda x: x["timestamp"], reverse=True)
    result = result[:100]
    _requests_cache = (now, result)
    return result


# ── Helper: parse timestamp strings ──────────────────────────────────────────
_TS_FORMATS = [
    "%Y-%m-%d %H:%M:%S UTC",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
]


def _parse_ts(raw: str) -> datetime | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in _TS_FORMATS:
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _minutes_until(raw: str) -> int | None:
    """Returns minutes until the given timestamp (negative if in the past)."""
    dt = _parse_ts(raw)
    if dt is None:
        return None
    delta = dt - datetime.now(timezone.utc)
    return int(delta.total_seconds() / 60)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/api/debug")
def debug():
    result = {
        "spreadsheet_id": GOOGLE_SPREADSHEET_ID,
        "sheet_name":     GOOGLE_SHEET_NAME,
        "values":         None,
        "error":          None,
    }
    logger.info("debug: spreadsheet_id=%r sheet_name=%r", GOOGLE_SPREADSHEET_ID, GOOGLE_SHEET_NAME)
    try:
        logger.info("debug: calling get_sheets_service()")
        service = get_sheets_service()
        logger.info("debug: sheets service OK — reading first 3 rows")
        resp = service.spreadsheets().values().get(
            spreadsheetId=GOOGLE_SPREADSHEET_ID,
            range=f"{GOOGLE_SHEET_NAME}!A1:O3",
        ).execute()
        result["values"] = resp.get("values", [])
        logger.info("debug: read OK — %d row(s) returned", len(result["values"]))
    except Exception as e:
        result["error"] = str(e)
        logger.error("debug: sheets read failed — %s", e)
    return result


@app.get("/health")
def health():
    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/metrics")
def metrics():
    tenant = _tenant_rows()
    pending = _pending_rows()
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    open_requests = sum(
        1 for r in tenant
        if r.get("status", "").strip().lower() not in ("resolved", "closed", "in-house", "vendor dispatched")
    )

    awaiting_super = sum(
        1 for r in pending
        if r.get("status", "").strip().lower() == "awaiting super"
    )

    emergencies = sum(
        1 for r in tenant
        if r.get("urgency", "").strip().lower() == "high"
        and r.get("status", "").strip().lower() not in ("resolved", "closed", "in-house", "vendor dispatched")
    )

    resolved_today = 0
    for r in tenant:
        status = r.get("status", "").strip().lower()
        if status in ("resolved", "closed", "in-house", "vendor dispatched"):
            ts = _parse_ts(r.get("timestamp", ""))
            if ts and ts >= today_start:
                resolved_today += 1

    # Average wait: hours from timestamp to now for open requests
    wait_hours = []
    for r in tenant:
        if r.get("status", "").strip().lower() in ("resolved", "closed", "in-house", "vendor dispatched"):
            continue
        ts = _parse_ts(r.get("timestamp", ""))
        if ts:
            wait_hours.append((now - ts).total_seconds() / 3600)

    avg_wait = round(sum(wait_hours) / len(wait_hours), 1) if wait_hours else 0.0

    return {
        "open_requests": open_requests,
        "awaiting_super": awaiting_super,
        "emergencies": emergencies,
        "resolved_today": resolved_today,
        "avg_wait_hours": avg_wait,
    }


@app.get("/api/pipeline")
def pipeline():
    tenant = _tenant_rows()
    pending = _pending_rows()

    # "received" = all tenant request rows
    received = len(tenant)

    # "super_notified" = rows that have super_notified = True (any case)
    super_notified = sum(
        1 for r in tenant
        if r.get("super_notified", "").strip().lower() == "true"
    )

    awaiting_reply = sum(
        1 for r in pending
        if r.get("status", "").strip().lower() == "awaiting super"
    )

    vendor_dispatched = sum(
        1 for r in pending
        if r.get("status", "").strip().lower() == "vendor dispatched"
    ) + sum(
        1 for r in tenant
        if r.get("status", "").strip().lower() == "vendor dispatched"
    )
    # dedupe: pending is the source of truth for dispatched
    vendor_dispatched = sum(
        1 for r in pending
        if r.get("status", "").strip().lower() == "vendor dispatched"
    )

    escalated = sum(
        1 for r in pending
        if r.get("status", "").strip().lower() == "escalated"
    )

    in_house = sum(
        1 for r in pending
        if r.get("status", "").strip().lower() == "in-house"
    )

    return {
        "received": received,
        "super_notified": super_notified,
        "awaiting_reply": awaiting_reply,
        "vendor_dispatched": vendor_dispatched,
        "escalated": escalated,
        "in_house": in_house,
    }


@app.get("/api/requests")
def requests():
    return _build_requests_with_super_threads()


@app.get("/api/pending")
def pending_endpoint():
    rows = _pending_rows()

    exclude_statuses = {"resolved", "in-house", "closed"}
    result = []

    for r in rows:
        status = r.get("status", "").strip().lower()
        if status in exclude_statuses:
            continue

        escalates_in = _minutes_until(r.get("escalate_at", ""))

        result.append({
            "timestamp":           r.get("timestamp", ""),
            "building_address":    r.get("building_address", ""),
            "super_email":         r.get("super_email", ""),
            "tenant_email":        r.get("tenant_email", ""),
            "tenant_name":         r.get("tenant_name", ""),
            "unit_number":         r.get("unit_number", ""),
            "issue_type":          r.get("issue_type", ""),
            "urgency":             r.get("urgency", ""),
            "description":         r.get("description", ""),
            "status":              r.get("status", ""),
            "resolution_note":     r.get("resolution_note", ""),
            "escalate_at":         r.get("escalate_at", ""),
            "escalates_in_minutes": escalates_in,
        })

    # Sort: high urgency first, then by escalation proximity
    urgency_order = {"high": 0, "medium": 1, "low": 2}
    result.sort(key=lambda x: (
        urgency_order.get(x["urgency"].lower(), 9),
        x["escalates_in_minutes"] if x["escalates_in_minutes"] is not None else 9999,
    ))

    return result


# ── Gmail thread endpoint ─────────────────────────────────────────────────────

import base64
import re as _re
from google.oauth2.service_account import Credentials as _SACredentials
from googleapiclient.discovery import build as _build
from googleapiclient.errors import HttpError as _HttpError

_GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
_thread_cache: dict[str, tuple[float, dict]] = {}


def _get_gmail_service():
    creds = _SACredentials.from_service_account_file(
        GOOGLE_SHEETS_CREDENTIALS_FILE, scopes=_GMAIL_SCOPES
    )
    delegated = creds.with_subject(GMAIL_USER_EMAIL)
    return _build("gmail", "v1", credentials=delegated)


def _get_header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _strip_html(html: str) -> str:
    """Very light HTML → plain text: strip tags, decode common entities."""
    text = _re.sub(r"<br\s*/?>", "\n", html, flags=_re.IGNORECASE)
    text = _re.sub(r"<p[^>]*>", "\n", text, flags=_re.IGNORECASE)
    text = _re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&") \
               .replace("&lt;", "<").replace("&gt;", ">") \
               .replace("&quot;", '"').replace("&#39;", "'")
    # Collapse excessive blank lines
    text = _re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _decode_part(payload: dict) -> str:
    """Recursively extract plain-text body from a Gmail message payload."""
    mime = payload.get("mimeType", "")
    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    if mime == "text/html":
        data = payload.get("body", {}).get("data", "")
        if data:
            html = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            return _strip_html(html)
    if mime.startswith("multipart/"):
        # Prefer text/plain among the parts
        plain = ""
        html_fallback = ""
        for part in payload.get("parts", []):
            part_mime = part.get("mimeType", "")
            if part_mime == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    plain = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            elif part_mime == "text/html" and not plain:
                data = part.get("body", {}).get("data", "")
                if data:
                    html = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
                    html_fallback = _strip_html(html)
            elif part_mime.startswith("multipart/"):
                nested = _decode_part(part)
                if nested and not plain:
                    plain = nested
        return plain or html_fallback
    return ""


def _parse_from(from_header: str) -> tuple[str, str]:
    """Split 'Name <email>' into (name, email)."""
    m = _re.match(r'^(.*?)\s*<([^>]+)>$', from_header.strip())
    if m:
        return m.group(1).strip().strip('"'), m.group(2).strip()
    return "", from_header.strip()


@app.get("/api/thread/{thread_id}")
def get_thread(thread_id: str):
    # Cache check
    now = time.monotonic()
    if thread_id in _thread_cache:
        ts, data = _thread_cache[thread_id]
        if now - ts < _CACHE_TTL:
            return data

    try:
        gmail = _get_gmail_service()
        thread = gmail.users().threads().get(
            userId="me", id=thread_id, format="full"
        ).execute()
    except _HttpError as e:
        if e.resp.status == 404:
            return {"thread_id": thread_id, "messages": [], "error": "not found"}
        logger.error("Gmail API error fetching thread %s: %s", thread_id, e)
        return {"thread_id": thread_id, "messages": [], "error": str(e)}
    except Exception as e:
        logger.error("Error fetching thread %s: %s", thread_id, e)
        return {"thread_id": thread_id, "messages": [], "error": str(e)}

    messages = []
    for msg in thread.get("messages", []):
        headers = msg.get("payload", {}).get("headers", [])
        from_hdr    = _get_header(headers, "From")
        to_hdr      = _get_header(headers, "To")
        subject     = _get_header(headers, "Subject")
        date_hdr    = _get_header(headers, "Date")
        from_name, from_email = _parse_from(from_hdr)

        # Parse date → ISO 8601
        ts_iso = ""
        if date_hdr:
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(date_hdr)
                ts_iso = dt.astimezone(timezone.utc).isoformat()
            except Exception:
                ts_iso = date_hdr

        body = _decode_part(msg.get("payload", {}))
        is_propflow = (
            GMAIL_USER_EMAIL and
            from_email.lower() == GMAIL_USER_EMAIL.lower()
        )

        messages.append({
            "message_id":  msg.get("id", ""),
            "from":        from_email,
            "from_name":   from_name or from_email,
            "to":          to_hdr,
            "subject":     subject,
            "timestamp":   ts_iso,
            "body":        body.strip(),
            "is_propflow": is_propflow,
        })

    result = {"thread_id": thread_id, "messages": messages}
    _thread_cache[thread_id] = (now, result)
    return result
