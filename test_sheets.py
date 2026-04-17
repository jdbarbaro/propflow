"""
test_sheets.py — Verifies the Google Sheets connection by appending one
hardcoded test row. No Gmail or Anthropic credentials needed.
"""

from sheets_client import append_row

TEST_ROW = {
    "tenant_name": "Test Tenant",
    "tenant_email": "test@example.com",
    "building_address": "450 Park Avenue",
    "unit_number": "2A",
    "issue_type": "Maintenance",
    "issue_description": "Test row — safe to delete.",
    "urgency": "Low",
}

if __name__ == "__main__":
    print("Appending test row to Google Sheet...")
    try:
        append_row(TEST_ROW)
        print("Success — check your sheet for the new row.")
    except Exception as e:
        print(f"Failed: {e}")
