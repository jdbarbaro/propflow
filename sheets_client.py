"""
sheets_client.py — Wrapper around the Google Sheets API for logging
parsed tenant requests as new rows and supporting Phase 2 routing lookups.

Authentication uses the same service account JSON key file as gmail_client.
The service account must be shared as an Editor on the target spreadsheet.
No domain-wide delegation is needed for Sheets — the service account
accesses the sheet directly via its own identity.

Tab structure expected in the spreadsheet:
  Tenant Requests — Phase 1 + Phase 2 request log (see COLUMNS below)
  Buildings       — one row per managed building (see BUILDING_COLUMNS)
  Vendors         — one row per vendor (see VENDOR_COLUMNS)
"""

import logging
import re
from datetime import datetime, timezone
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from config import (
    GOOGLE_SHEETS_CREDENTIALS_FILE,
    GOOGLE_SPREADSHEET_ID,
    GOOGLE_SHEET_NAME,
    GOOGLE_BUILDINGS_SHEET,
    GOOGLE_VENDORS_SHEET,
    GOOGLE_PENDING_SHEET,
)

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# ── Tenant Requests tab columns (A–P) ────────────────────────────────────────
# Phase 1: A–I  |  Phase 2: J–N  |  Threading: O  |  Review: P
COLUMNS = [
    "Timestamp",            # A  [0]
    "Tenant Name",          # B  [1]
    "Tenant Email",         # C  [2]
    "Building Address",     # D  [3]
    "Unit",                 # E  [4]
    "Issue Type",           # F  [5]
    "Description",          # G  [6]
    "Urgency",              # H  [7]
    "Status",               # I  [8]
    "Vendor Assigned",      # J  [9]
    "Vendor Email",         # K  [10]
    "Vendor Contact Method",# L  [11]
    "Super Notified",       # M  [12]
    "Approval Required",    # N  [13]
    "thread_id",            # O  [14] — Gmail thread ID for dashboard email viewer
    "review_flag",          # P  [15] — NEEDS_REVIEW if sender classification is fuzzy
]

# ── Buildings tab expected headers (row 1) ────────────────────────────────────
BUILDING_COLUMNS = [
    "building_id", "full_address", "client_name", "borough_city", "super_email", "approval_threshold"
]

# ── Vendors tab expected headers (row 1) ──────────────────────────────────────
VENDOR_COLUMNS = [
    "vendor_name", "trade", "geography", "priority_rank",
    "emergency_capable", "email", "contact_method"
]


def get_sheets_service():
    """Builds and returns an authenticated Google Sheets API service object."""
    creds = Credentials.from_service_account_file(
        GOOGLE_SHEETS_CREDENTIALS_FILE, scopes=SCOPES
    )
    return build("sheets", "v4", credentials=creds)


def _rows_to_dicts(values: list[list]) -> list[dict]:
    """
    Converts a 2-D list from the Sheets API (first row = headers) into a list
    of dicts. Pads short rows with empty strings to match the header count.
    """
    if not values or len(values) < 2:
        return []
    headers = [h.strip().lower().replace(" ", "_") for h in values[0]]
    result = []
    for row in values[1:]:
        padded = row + [""] * (len(headers) - len(row))
        result.append(dict(zip(headers, padded)))
    return result


def _read_tab(tab_name: str) -> list[dict]:
    """Reads all rows from a named tab and returns them as a list of dicts."""
    service = get_sheets_service()
    try:
        response = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=GOOGLE_SPREADSHEET_ID, range=f"{tab_name}!A:Z")
            .execute()
        )
        return _rows_to_dicts(response.get("values", []))
    except HttpError as e:
        logger.error("Failed to read tab %r: %s", tab_name, e)
        return []


# ── Phase 1 ───────────────────────────────────────────────────────────────────

def append_row(parsed: dict) -> int | None:
    """
    Appends a single Phase 1 row to the Tenant Requests tab.

    Returns:
        The 1-based row number of the newly appended row, or None on failure.
        The row number is used by Phase 2 to update the same row with routing data.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    row = [
        timestamp,
        parsed.get("tenant_name") or "",
        parsed.get("tenant_email") or "",
        parsed.get("building_address") or "",
        parsed.get("unit_number") or "",
        parsed.get("issue_type") or "",
        parsed.get("issue_description") or "",
        parsed.get("urgency") or "",
        "Open",
        "",  # J Vendor Assigned   — filled by Cycle 2
        "",  # K Vendor Email
        "",  # L Vendor Contact Method
        "",  # M Super Notified
        "",  # N Approval Required
        parsed.get("thread_id") or "",     # O [14] thread_id
        parsed.get("review_flag") or "",   # P [15] review_flag
    ]

    service = get_sheets_service()
    range_name = f"{GOOGLE_SHEET_NAME}!A1"

    logger.info(
        "append_row: writing %d values — thread_id[14]=%r review_flag[15]=%r full_row=%r",
        len(row),
        row[14] if len(row) > 14 else "MISSING",
        row[15] if len(row) > 15 else "MISSING",
        row,
    )

    try:
        response = service.spreadsheets().values().append(
            spreadsheetId=GOOGLE_SPREADSHEET_ID,
            range=range_name,
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
        logger.info("Row appended for tenant: %s", parsed.get("tenant_email"))

        # Extract the row number from updatedRange e.g. "'Tenant Requests'!A12:I12"
        updated_range = response.get("updates", {}).get("updatedRange", "")
        match = re.search(r":?[A-Z]+(\d+)$", updated_range)
        return int(match.group(1)) if match else None
    except HttpError as e:
        logger.error("Failed to append row to Google Sheet: %s", e)
        raise


# ── Phase 2 ───────────────────────────────────────────────────────────────────

def get_building(address: str) -> dict | None:
    """
    Looks up a building in the Buildings tab using fuzzy address matching.
    Match succeeds if the parsed address string is found anywhere inside the
    tab's full_address field (case-insensitive).

    Returns a building dict with keys from BUILDING_COLUMNS, or None if not found.
    """
    if not address:
        return None

    rows = _read_tab(GOOGLE_BUILDINGS_SHEET)
    needle = address.strip().lower()

    for row in rows:
        haystack = row.get("full_address", "").lower()
        if needle in haystack or haystack in needle:
            return row

    logger.warning("No building found matching address: %r", address)
    return None


def get_vendor(trade: str, geography: str, urgency: str) -> dict | None:
    """
    Looks up the best available vendor for a given trade, geography, and urgency.

    Matching rules:
      1. vendor trade must match issue_type (case-insensitive)
      2. vendor geography must contain the building's borough_city, OR be "all"/"nyc"
      3. if urgency is "High", vendor emergency_capable must be "yes"
      4. among matches, return the one with the lowest priority_rank (1 = best)

    Returns a vendor dict with keys from VENDOR_COLUMNS, or None if no match found.
    """
    rows = _read_tab(GOOGLE_VENDORS_SHEET)
    trade_lower = trade.strip().lower()
    geo_lower = (geography or "").strip().lower()
    is_emergency = urgency.strip().lower() == "high"

    candidates = []
    for row in rows:
        # Trade match
        if row.get("trade", "").strip().lower() != trade_lower:
            continue

        # Geography match
        vendor_geo = row.get("geography", "").strip().lower()
        geo_match = (
            vendor_geo in ("all", "nyc")
            or geo_lower in vendor_geo
            or vendor_geo in geo_lower
        )
        if not geo_match:
            continue

        # Emergency capability filter
        if is_emergency and row.get("emergency_capable", "").strip().lower() != "yes":
            continue

        try:
            rank = int(row.get("priority_rank", 999))
        except ValueError:
            rank = 999

        candidates.append((rank, row))

    if not candidates:
        logger.warning(
            "No vendor found for trade=%r geography=%r emergency=%s",
            trade, geography, is_emergency,
        )
        return None

    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


# ── Pending tab ───────────────────────────────────────────────────────────────
# Columns A–Q track requests awaiting super approval before vendor dispatch.
PENDING_COLUMNS = [
    "Timestamp",           # A
    "Building ID",         # B
    "Building Address",    # C
    "Super Email",         # D
    "Tenant Email",        # E
    "Unit Number",         # F
    "Issue Type",          # G
    "Urgency",             # H
    "Description",         # I
    "Tenant Requests Row", # J
    "Super Thread ID",     # K
    "Status",              # L  — Awaiting Super | In-House | Vendor Dispatched | Closed
    "Resolution Note",     # M
    "Tenant Name",         # N
    "Tenant Thread ID",    # O
    "Tenant Message ID",   # P
    "escalate_at",         # Q  — ISO 8601 UTC timestamp for escalation deadline
]


def get_awaiting_super_rows() -> list[tuple[dict, int]]:
    """
    Returns all (pending_dict, row_number) pairs from the Pending tab
    where status is exactly "Awaiting Super".

    Used by check_pending_threads() to find requests that are still waiting
    for a super response, so their Gmail threads can be polled directly.

    Returns:
        List of (pending_dict, 1-based row number) tuples, oldest first.
        Empty list on error or if no rows are awaiting.
    """
    service = get_sheets_service()
    try:
        response = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=GOOGLE_SPREADSHEET_ID, range=f"{GOOGLE_PENDING_SHEET}!A:Z")
            .execute()
        )
    except HttpError as e:
        logger.error("Failed to read Pending tab in get_awaiting_super_rows: %s", e)
        return []

    values = response.get("values", [])
    if not values or len(values) < 2:
        return []

    headers    = [h.strip().lower().replace(" ", "_") for h in values[0]]
    status_col = headers.index("status") if "status" in headers else -1

    results = []
    for row_idx, row in enumerate(values[1:], start=2):
        padded = row + [""] * (len(headers) - len(row))
        status = padded[status_col].strip().lower() if status_col >= 0 else ""
        if status == "awaiting super":
            results.append((dict(zip(headers, padded)), row_idx))

    return results


def write_pending_request(data: dict) -> int | None:
    """
    Writes a new row to the Pending tab for a request awaiting super approval.

    Args:
        data: Dict with keys: building_id, building_address, super_email,
              tenant_email, unit_number, issue_type, urgency, description,
              tenant_requests_row, super_thread_id, tenant_name,
              tenant_thread_id.

    Pending tab column layout (A–Q):
      A  Timestamp          B  Building ID        C  Building Address
      D  Super Email        E  Tenant Email       F  Unit Number
      G  Issue Type         H  Urgency            I  Description
      J  Tenant Requests Row  K  Super Thread ID  L  Status
      M  Resolution Note    N  Tenant Name        O  Tenant Thread ID
      P  Tenant Message ID  Q  escalate_at

    Returns:
        The 1-based row number of the new Pending row, or None on failure.
    """
    from datetime import datetime, timezone
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    row = [
        timestamp,
        data.get("building_id") or "",
        data.get("building_address") or "",
        data.get("super_email") or "",
        data.get("tenant_email") or "",
        data.get("unit_number") or "",
        data.get("issue_type") or "",
        data.get("urgency") or "",
        data.get("description") or "",
        str(data.get("tenant_requests_row") or ""),
        data.get("super_thread_id") or "",
        "Awaiting Super",
        "",
        data.get("tenant_name") or "",
        data.get("tenant_thread_id") or "",
        data.get("tenant_message_id") or "",
        data.get("escalate_at") or "",
    ]

    service = get_sheets_service()
    try:
        response = service.spreadsheets().values().append(
            spreadsheetId=GOOGLE_SPREADSHEET_ID,
            range=f"{GOOGLE_PENDING_SHEET}!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
        updated_range = response.get("updates", {}).get("updatedRange", "")
        match = re.search(r":?[A-Z]+(\d+)$", updated_range)
        row_number = int(match.group(1)) if match else None
        logger.info("Pending request written (row %s, thread_id=%s).", row_number, data.get("super_thread_id"))
        return row_number
    except HttpError as e:
        logger.error("Failed to write Pending row: %s", e)
        return None


def get_pending_by_thread(thread_id: str) -> tuple[dict, int] | tuple[None, None]:
    """
    Finds an open pending request whose super_thread_id matches the given thread_id.
    Only rows with status "Awaiting Super" are returned — resolved rows are skipped.

    Returns:
        (pending_dict, 1-based_row_number) if found, else (None, None).
    """
    if not thread_id:
        return None, None

    service = get_sheets_service()
    try:
        response = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=GOOGLE_SPREADSHEET_ID, range=f"{GOOGLE_PENDING_SHEET}!A:Z")
            .execute()
        )
    except HttpError as e:
        logger.error("Failed to read Pending tab: %s", e)
        return None, None

    values = response.get("values", [])
    if not values or len(values) < 2:
        return None, None

    headers = [h.strip().lower().replace(" ", "_") for h in values[0]]

    # Locate the columns we need by header name
    try:
        thread_col = headers.index("super_thread_id")
    except ValueError:
        logger.error("Pending tab missing 'Super Thread ID' column.")
        return None, None

    status_col = headers.index("status") if "status" in headers else -1

    for row_idx, row in enumerate(values[1:], start=2):  # row 2 = first data row
        padded = row + [""] * (len(headers) - len(row))
        if padded[thread_col] != thread_id:
            continue
        # Only match rows still awaiting a response
        status = padded[status_col].strip().lower() if status_col >= 0 else ""
        if status not in ("awaiting super", ""):
            continue
        return dict(zip(headers, padded)), row_idx

    return None, None


def _col_index_to_letter(index: int) -> str:
    """Converts a 0-based column index to a Sheets column letter (A, B, … Z, AA, …)."""
    result = ""
    while index >= 0:
        result = chr(ord("A") + index % 26) + result
        index = index // 26 - 1
    return result


def _pending_col(header_name: str, service) -> str | None:
    """
    Looks up the column letter for a given header name in the Pending tab.
    Returns None if the header is not found.
    """
    try:
        resp = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=GOOGLE_SPREADSHEET_ID, range=f"{GOOGLE_PENDING_SHEET}!1:1")
            .execute()
        )
    except HttpError as e:
        logger.error("Failed to read Pending headers: %s", e)
        return None

    headers = [(h.strip().lower()) for h in (resp.get("values") or [[]])[0]]
    try:
        idx = headers.index(header_name.strip().lower())
        return _col_index_to_letter(idx)
    except ValueError:
        logger.error("Pending tab missing header %r", header_name)
        return None


def update_pending_status(row_number: int, status: str, note: str = "") -> None:
    """
    Updates the Status and Resolution Note columns of a Pending row.

    Column positions are looked up dynamically by header name so the function
    stays correct if columns are reordered or inserted.

    Args:
        row_number: 1-based row number in the Pending tab.
        status: New status string e.g. "In-House", "Vendor Dispatched", "Closed".
        note: Optional resolution note written to the Resolution Note column.
    """
    logger.info(
        "update_pending_status: row_number=%r status=%r note=%r",
        row_number, status, note,
    )

    service = get_sheets_service()

    status_col = _pending_col("status", service)
    note_col   = _pending_col("resolution note", service)

    if not status_col:
        logger.error("Cannot update Pending row %d — 'Status' column not found.", row_number)
        return

    logger.info(
        "update_pending_status: Status→%s, Resolution Note→%s (row %d)",
        status_col, note_col, row_number,
    )

    data = [{"range": f"{GOOGLE_PENDING_SHEET}!{status_col}{row_number}", "values": [[status]]}]
    if note and note_col:
        data.append({
            "range":  f"{GOOGLE_PENDING_SHEET}!{note_col}{row_number}",
            "values": [[note]],
        })

    for entry in data:
        logger.info(
            "update_pending_status: writing to spreadsheet=%s range=%s value=%s",
            GOOGLE_SPREADSHEET_ID, entry["range"], entry["values"][0][0],
        )

    try:
        response = service.spreadsheets().values().batchUpdate(
            spreadsheetId=GOOGLE_SPREADSHEET_ID,
            body={"valueInputOption": "USER_ENTERED", "data": data},
        ).execute()
        logger.info("update_pending_status: API response=%s", response)
        logger.info("Pending row %d updated: status=%r note=%r", row_number, status, note)
    except HttpError as e:
        logger.error("Failed to update Pending row %d: %s", row_number, e)
        raise


def get_vendor_by_name(vendor_name: str) -> dict | None:
    """
    Looks up a vendor by name (case-insensitive exact match on vendor_name column).

    Returns a vendor dict with keys from VENDOR_COLUMNS, or None if not found.
    """
    if not vendor_name:
        return None
    rows = _read_tab(GOOGLE_VENDORS_SHEET)
    name_lower = vendor_name.strip().lower()
    for row in rows:
        if row.get("vendor_name", "").strip().lower() == name_lower:
            return row
    logger.warning("No vendor found with name: %r", vendor_name)
    return None


def update_row(row_number: int, fields: dict) -> None:
    """
    Updates Phase 2 columns (J–N) in an existing Tenant Requests row.

    Args:
        row_number: 1-based row number returned by append_row().
        fields: Dict with any subset of keys:
                vendor_assigned, vendor_email, vendor_contact_method,
                super_notified, approval_required.
    """
    # Map field names to column letters (J=10 through N=14, 1-indexed)
    col_map = {
        "vendor_assigned":       "J",
        "vendor_email":          "K",
        "vendor_contact_method": "L",
        "super_notified":        "M",
        "approval_required":     "N",
    }

    data = []
    for key, col in col_map.items():
        if key in fields:
            val = fields[key]
            data.append({
                "range": f"{GOOGLE_SHEET_NAME}!{col}{row_number}",
                "values": [[str(val)]],
            })

    if not data:
        return

    service = get_sheets_service()
    try:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=GOOGLE_SPREADSHEET_ID,
            body={"valueInputOption": "USER_ENTERED", "data": data},
        ).execute()
        logger.info("Updated Phase 2 fields for row %d: %s", row_number, list(fields.keys()))
    except HttpError as e:
        logger.error("Failed to update row %d: %s", row_number, e)
        raise


def set_review_flag(row_number: int, flag: str = "NEEDS_REVIEW") -> None:
    """
    Writes a review flag to column P (review_flag) of a Tenant Requests row.

    Called when classify_sender() returns "super_fuzzy" — the email was
    treated as a tenant request but the sender address resembles a known
    super's domain, warranting manual review.

    Args:
        row_number: 1-based row number in the Tenant Requests tab.
        flag: Value to write — defaults to "NEEDS_REVIEW". Pass "" to clear.
    """
    service = get_sheets_service()
    range_ = f"{GOOGLE_SHEET_NAME}!P{row_number}"
    try:
        service.spreadsheets().values().update(
            spreadsheetId=GOOGLE_SPREADSHEET_ID,
            range=range_,
            valueInputOption="USER_ENTERED",
            body={"values": [[flag]]},
        ).execute()
        logger.info("set_review_flag: row %d → %r (column P)", row_number, flag)
    except HttpError as e:
        logger.error("Failed to set review_flag on row %d: %s", row_number, e)
        raise
