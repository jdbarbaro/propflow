"""
test_phase2.py — Tests the Phase 2 routing pipeline end to end.
No emails are sent. Tests lookup, approval logic, and AI-generated copy.
"""

import json
from sheets_client import get_sheets_service
from config import GOOGLE_SPREADSHEET_ID, GOOGLE_BUILDINGS_SHEET, GOOGLE_VENDORS_SHEET
from agents.router import (
    lookup_building,
    lookup_vendor,
    check_approval_required,
    build_super_notification,
    build_vendor_outreach,
)

PARSED_EMAIL = {
    "tenant_name": "Sarah Chen",
    "tenant_email": "sarah@test.com",
    "building_address": "450 Park Avenue",
    "unit_number": "2A",
    "issue_type": "Plumbing",
    "issue_description": "Water is leaking from the pipe under the kitchen sink.",
    "urgency": "High",
}

DIVIDER = "─" * 60


def section(title: str) -> None:
    print(f"\n{DIVIDER}")
    print(f"  {title}")
    print(DIVIDER)


def test_approval_scenarios() -> None:
    """Runs three targeted approval threshold scenarios and prints pass/fail."""
    section("Approval Threshold Scenarios")

    scenarios = [
        {
            "label":    "Routine Plumbing at $5,000 threshold → expect False",
            "issue":    "Plumbing",
            "urgency":  "Low",
            "threshold": "$5,000",
            "expected": False,
        },
        {
            "label":    "Emergency HVAC at $2,500 threshold  → expect True",
            "issue":    "HVAC",
            "urgency":  "High",
            "threshold": "$2,500",
            "expected": True,
        },
        {
            "label":    "Structural (any urgency/threshold)   → always True",
            "issue":    "Structural",
            "urgency":  "Low",
            "threshold": "$50,000",
            "expected": True,
        },
    ]

    all_passed = True
    for s in scenarios:
        result = check_approval_required(s["issue"], s["urgency"], s["threshold"])
        passed = result == s["expected"]
        all_passed = all_passed and passed
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {s['label']}")
        print(f"         issue={s['issue']!r}  urgency={s['urgency']!r}  "
              f"threshold={s['threshold']!r}  got={result}  expected={s['expected']}")

    print(f"\n  {'All approval scenarios passed.' if all_passed else 'One or more scenarios FAILED.'}")


def diagnose_tabs() -> None:
    """Prints the raw header row and first data row from Buildings and Vendors tabs."""
    service = get_sheets_service()
    tabs = {
        "Buildings": GOOGLE_BUILDINGS_SHEET,
        "Vendors":   GOOGLE_VENDORS_SHEET,
    }

    for label, tab_name in tabs.items():
        section(f"Raw tab: {label} ({tab_name!r})")
        response = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=GOOGLE_SPREADSHEET_ID, range=f"{tab_name}!A1:Z2")
            .execute()
        )
        rows = response.get("values", [])
        if not rows:
            print("  [!] Tab is empty or not found.")
            continue

        headers = rows[0]
        print(f"  Headers ({len(headers)} columns):")
        for i, h in enumerate(headers):
            col_letter = chr(ord("A") + i)
            print(f"    {col_letter}  {h!r}")

        if len(rows) > 1:
            data = rows[1]
            print(f"\n  First data row ({len(data)} values):")
            for i, val in enumerate(data):
                header = headers[i] if i < len(headers) else f"col_{i}"
                print(f"    {header!r:<30} {val!r}")
        else:
            print("\n  [!] No data rows found (only a header row).")


def main():
    print("\nPropFlow — Phase 2 Routing Test")
    print(f"Input: {json.dumps(PARSED_EMAIL, indent=2)}")

    # ── Tab diagnostics ───────────────────────────────────────────────────────
    diagnose_tabs()

    # ── Approval scenarios ────────────────────────────────────────────────────
    test_approval_scenarios()

    # ── Step 1: Building lookup ───────────────────────────────────────────────
    section("Step 1: lookup_building()")
    building = lookup_building(PARSED_EMAIL["building_address"])
    if building:
        for k, v in building.items():
            print(f"  {k:<22} {v}")
    else:
        print("  [!] No building found — check Buildings tab for '450 Park Avenue'")

    # ── Step 2: Vendor lookup ─────────────────────────────────────────────────
    section("Step 2: lookup_vendor()")
    geography = building.get("borough_city", "") if building else ""
    vendor = lookup_vendor(
        issue_type=PARSED_EMAIL["issue_type"],
        geography=geography,
        urgency=PARSED_EMAIL["urgency"],
    )
    if vendor:
        for k, v in vendor.items():
            print(f"  {k:<22} {v}")
    else:
        print("  [!] No vendor found — check Vendors tab for Plumbing in this geography")

    # ── Step 3: Approval check (live data) ────────────────────────────────────
    section("Step 3: check_approval_required() — live building data")
    threshold = building.get("approval_threshold", "") if building else ""
    approval = check_approval_required(
        issue_type=PARSED_EMAIL["issue_type"],
        urgency=PARSED_EMAIL["urgency"],
        threshold=threshold,
    )
    print(f"  approval_threshold (building): {threshold!r}")
    print(f"  estimated cost proxy:          ${3000 if PARSED_EMAIL['urgency'] == 'High' else 500}")
    print(f"  approval_required:             {approval}")

    # ── Step 4: Super notification ────────────────────────────────────────────
    section("Step 4: build_super_notification()")
    if building:
        super_body = build_super_notification(PARSED_EMAIL, building, vendor)
        print(super_body)
    else:
        print("  [!] Skipped — no building record available.")

    # ── Step 5: Vendor outreach ───────────────────────────────────────────────
    section("Step 5: build_vendor_outreach()")
    if building:
        vendor_body = build_vendor_outreach(PARSED_EMAIL, building)
        print(vendor_body)
    else:
        print("  [!] Skipped — no building record available.")

    # ── Summary ───────────────────────────────────────────────────────────────
    section("Summary")
    print(f"  Building found:     {'Yes — ' + building.get('full_address', '') if building else 'No'}")
    print(f"  Vendor found:       {'Yes — ' + vendor.get('vendor_name', '') if vendor else 'No'}")
    print(f"  Approval required:  {approval if building else 'N/A'}")
    print(f"  Super email:        {building.get('super_email', 'N/A') if building else 'N/A'}")
    print(f"  Vendor email:       {vendor.get('email', 'N/A') if vendor else 'N/A'}")
    print()


if __name__ == "__main__":
    main()
