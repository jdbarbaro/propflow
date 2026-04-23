"""
audit_pending_tab.py — Audits the Pending Requests tab column layout.

Reads the actual header row from the sheet and compares it against
what write_pending_request() writes and update_pending_status() targets.

Run with:
    source venv/bin/activate
    python3 audit_pending_tab.py
"""

import sys
from googleapiclient.errors import HttpError
from sheets_client import get_sheets_service, _col_index_to_letter
from config import GOOGLE_SPREADSHEET_ID, GOOGLE_PENDING_SHEET


def main():
    print(f"\n=== Pending Tab Audit: '{GOOGLE_PENDING_SHEET}' ===\n")

    service = get_sheets_service()

    # ── 1. Read actual header row ─────────────────────────────────────────────
    try:
        resp = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=GOOGLE_SPREADSHEET_ID, range=f"{GOOGLE_PENDING_SHEET}!1:1")
            .execute()
        )
    except HttpError as e:
        print(f"ERROR reading sheet: {e}")
        sys.exit(1)

    raw_headers = (resp.get("values") or [[]])[0]

    print("── Actual sheet headers ──────────────────────────────────────────")
    for i, h in enumerate(raw_headers):
        col_letter = _col_index_to_letter(i)
        print(f"  {col_letter:>3}  {h!r}")

    # ── 2. What write_pending_request() writes ────────────────────────────────
    print("\n── write_pending_request() column positions (code) ──────────────")
    code_columns = [
        "Timestamp",
        "Building ID",
        "Building Address",
        "Super Email",
        "Tenant Email",
        "Unit Number",
        "Issue Type",
        "Urgency",
        "Description",
        "Tenant Requests Row",
        "Super Thread ID",
        "Status  ← hardcoded 'Awaiting Super'",
        "Resolution Note  ← hardcoded ''",
        "Tenant Name",
        "Tenant Thread ID",
    ]
    for i, label in enumerate(code_columns):
        col_letter = _col_index_to_letter(i)
        print(f"  {col_letter:>3}  {label}")

    # ── 3. What update_pending_status() resolves ──────────────────────────────
    print("\n── update_pending_status() dynamic column lookup ─────────────────")
    headers_lower = [h.strip().lower() for h in raw_headers]

    for target in ("status", "resolution note"):
        if target in headers_lower:
            idx = headers_lower.index(target)
            col = _col_index_to_letter(idx)
            print(f"  {target!r:20} → column {col}  (sheet position {idx})")
        else:
            print(f"  {target!r:20} → NOT FOUND IN SHEET HEADERS")

    # ── 4. Mismatch analysis ──────────────────────────────────────────────────
    print("\n── Mismatch analysis ─────────────────────────────────────────────")
    mismatches = []
    for i, code_label in enumerate(code_columns):
        col_letter = _col_index_to_letter(i)
        sheet_header = raw_headers[i] if i < len(raw_headers) else "(no header)"
        code_clean = code_label.split("←")[0].strip()
        if code_clean.lower() != sheet_header.lower():
            mismatches.append((col_letter, code_clean, sheet_header))

    if mismatches:
        print("  MISMATCHES FOUND:")
        for col, code_val, sheet_val in mismatches:
            print(f"  {col}: code expects {code_val!r}, sheet has {sheet_val!r}")
    else:
        print("  No positional mismatches — code and sheet are aligned.")

    # ── 5. Full resolved column map ───────────────────────────────────────────
    print("\n── Full resolved column map (sheet truth) ────────────────────────")
    all_headers = raw_headers + (
        ["(no header)"] * max(0, len(code_columns) - len(raw_headers))
    )
    for i, h in enumerate(all_headers[:max(len(raw_headers), len(code_columns))]):
        col = _col_index_to_letter(i)
        code_label = code_columns[i] if i < len(code_columns) else "(extra)"
        marker = "  ← MISMATCH" if (col, code_label.split("←")[0].strip(), h) in [
            (c, cv, sv) for c, cv, sv in mismatches
        ] else ""
        print(f"  {col:>3}  sheet={h!r:30}  code={code_label!r}{marker}")

    print()


if __name__ == "__main__":
    main()
