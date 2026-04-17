"""
check_sheets.py — Reads and writes row 14 in the Tenant Requests tab
to verify that update_row() works correctly against the live sheet.
"""

from sheets_client import get_sheets_service, update_row
from config import GOOGLE_SPREADSHEET_ID, GOOGLE_SHEET_NAME

ROW = 14
COLS_ALL   = "A:N"
COLS_P2    = "J:N"
COL_LABELS = list("ABCDEFGHIJKLMN")

DIVIDER = "─" * 50


def read_row(service, row: int, col_range: str) -> list[str]:
    """Returns the cell values for a single row across the given column range."""
    result = service.spreadsheets().values().get(
        spreadsheetId=GOOGLE_SPREADSHEET_ID,
        range=f"{GOOGLE_SHEET_NAME}!{col_range}{row}:{col_range.split(':')[-1]}{row}",
    ).execute()
    return result.get("values", [[]])[0]


def print_row(values: list[str], start_col: str = "A") -> None:
    start = ord(start_col)
    for i, val in enumerate(values):
        col = chr(start + i)
        print(f"  {col}  {val!r}")


def main():
    service = get_sheets_service()

    # ── Step 1: Read A:N before write ─────────────────────────────────────────
    print(f"\n{DIVIDER}")
    print(f"  Row {ROW} — current values (A:N)")
    print(DIVIDER)
    result = service.spreadsheets().values().get(
        spreadsheetId=GOOGLE_SPREADSHEET_ID,
        range=f"{GOOGLE_SHEET_NAME}!A{ROW}:N{ROW}",
    ).execute()
    before = result.get("values", [[]])[0]
    for i, val in enumerate(before):
        print(f"  {COL_LABELS[i]}  {val!r}")

    # ── Step 2: Write test values to J:N ──────────────────────────────────────
    print(f"\n{DIVIDER}")
    print(f"  Writing test values to row {ROW}, columns J–N")
    print(DIVIDER)
    test_fields = {
        "vendor_assigned":       "TEST VENDOR",
        "vendor_email":          "test@vendor.com",
        "vendor_contact_method": "email",
        "super_notified":        "DRY RUN",
        "approval_required":     "False",
    }
    for k, v in test_fields.items():
        print(f"  {k:<24} → {v!r}")

    update_row(ROW, test_fields)
    print("\n  update_row() completed.")

    # ── Step 3: Read J:N after write ──────────────────────────────────────────
    print(f"\n{DIVIDER}")
    print(f"  Row {ROW} — columns J–N after write")
    print(DIVIDER)
    result = service.spreadsheets().values().get(
        spreadsheetId=GOOGLE_SPREADSHEET_ID,
        range=f"{GOOGLE_SHEET_NAME}!J{ROW}:N{ROW}",
    ).execute()
    after = result.get("values", [[]])[0]
    expected = list(test_fields.values())
    all_match = True
    for i, col in enumerate("JKLMN"):
        actual = after[i] if i < len(after) else ""
        match = actual == expected[i]
        all_match = all_match and match
        status = "✓" if match else "✗"
        print(f"  {col}  {actual!r}  {status}")

    print(f"\n  {'All values confirmed.' if all_match else 'MISMATCH — check output above.'}")
    print()


if __name__ == "__main__":
    main()
