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
)

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# ── Tenant Requests tab columns (A–N) ────────────────────────────────────────
# Phase 1: A–I  |  Phase 2: J–N
COLUMNS = [
    "Timestamp",            # A
    "Tenant Name",          # B
    "Tenant Email",         # C
    "Building Address",     # D
    "Unit",                 # E
    "Issue Type",           # F
    "Description",          # G
    "Urgency",              # H
    "Status",               # I
    "Vendor Assigned",      # J
    "Vendor Email",         # K
    "Vendor Contact Method",# L
    "Super Notified",       # M
    "Approval Required",    # N
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
            .get(spreadsheetId=GOOGLE_SPREADSHEET_ID, range=f"{tab_name}!A1:Z")
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
    ]

    service = get_sheets_service()
    range_name = f"{GOOGLE_SHEET_NAME}!A1"

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
