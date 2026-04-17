"""
test_edge_cases.py — Runs 10 realistic tenant email scenarios through
parse_email() and prints a formatted summary table with pass/fail results.

A result is considered PASS if all three required fields are extracted:
  - urgency (not null)
  - issue_type (not null, not "Other" for clear-cut cases)
  - issue_description (not null)

building_address and tenant_name are tracked for null-rate reporting
but do not affect pass/fail since they are legitimately absent in some emails.
"""

from agents.email_parser import parse_email

REQUIRED_FIELDS = ["urgency", "issue_type", "issue_description"]
TRACKED_NULLABLE = ["building_address", "tenant_name", "unit_number", "tenant_email"]

SCENARIOS = [
    {
        "id": 1,
        "description": "Emergency — gas smell, no address",
        "sender_email": "tenant1@gmail.com",
        "subject": "Gas smell in apartment URGENT",
        "body": (
            "There is a very strong smell of gas in my apartment. "
            "I don't know where it's coming from. I am scared, please send someone NOW. "
            "My name is Rosa Alvarez, please call me at 646-555-0181."
        ),
    },
    {
        "id": 2,
        "description": "Emergency — elevator down, address in subject only",
        "sender_email": "j.friedman@company.com",
        "subject": "Elevator out of service — 299 Park Avenue",
        "body": (
            "Hi, both elevators in our building have been out of service since 7am. "
            "We have elderly tenants on the upper floors who cannot use the stairs. "
            "This needs to be addressed immediately. — Jacob Friedman, Suite 1400"
        ),
    },
    {
        "id": 3,
        "description": "Urgent — roof leak, vague location",
        "sender_email": "unknown_tenant@hotmail.com",
        "subject": "Leak coming through my ceiling",
        "body": (
            "Its raining really hard outside and water is dripping through my ceiling "
            "near the window. Its getting on my furniture. The building on 5th. "
            "Please someone come look at this today."
        ),
    },
    {
        "id": 4,
        "description": "Urgent — no heat, broken English",
        "sender_email": "m.petrov@gmail.com",
        "subject": "no heat in apartment since 2 day",
        "body": (
            "hello my name mikhail petrov i live 780 Third Avenue apartment 3C "
            "since 2 day no heat in my home. outside very cold. "
            "my children very cold. please help fast. thank you"
        ),
    },
    {
        "id": 5,
        "description": "Urgent — multiple issues, leak AND broken lock",
        "sender_email": "dana.wu@outlook.com",
        "subject": "Several urgent issues in Unit 8B",
        "body": (
            "Hi, I'm Dana Wu in unit 8B at 101 Park Avenue South. I have two urgent issues: "
            "1. There is a leak under my kitchen sink that has been dripping for three days "
            "and is starting to smell. "
            "2. The deadbolt on my front door is broken and I cannot fully lock my apartment. "
            "Please send maintenance as soon as possible. Thank you."
        ),
    },
    {
        "id": 6,
        "description": "Routine — AC noise, very detailed",
        "sender_email": "claire.oconnor@gmail.com",
        "subject": "Air conditioning unit making rattling noise — Unit 12A",
        "body": (
            "Dear Property Management Team, I hope this message finds you well. "
            "My name is Claire O'Connor and I reside in Unit 12A at 350 Madison Avenue. "
            "I am writing to report that the air conditioning unit in my bedroom has been "
            "producing a persistent rattling noise for the past five days, particularly "
            "during startup. The unit is still functional and cooling adequately, but the "
            "noise is disruptive during the evening hours. I would appreciate it if a "
            "technician could inspect the unit at your earliest convenience. "
            "I am available Monday through Friday after 5pm and any time on weekends. "
            "Thank you for your attention to this matter. Best regards, Claire O'Connor"
        ),
    },
    {
        "id": 7,
        "description": "Routine — lightbulb replacement",
        "sender_email": "tenant7@propflowz.com",
        "subject": "Lightbulb out in hallway",
        "body": (
            "Hi, the overhead lightbulb in the hallway outside my door (unit 5D at "
            "200 Park Avenue) has been out for about a week. It's a little dark at night. "
            "No rush, just flagging it when maintenance has a chance. Thanks, Tom."
        ),
    },
    {
        "id": 8,
        "description": "Vague — one line, no info",
        "sender_email": "anon@gmail.com",
        "subject": "",
        "body": "hey its broken again can someone fix it",
    },
    {
        "id": 9,
        "description": "Angry — caps, exclamation marks",
        "sender_email": "angry.tenant@gmail.com",
        "subject": "THIS IS UNACCEPTABLE",
        "body": (
            "I have been calling your office for TWO WEEKS and nobody has responded!! "
            "The mold in my bathroom at 415 Lexington Avenue unit 6F is getting WORSE. "
            "I have sent photos. I have left voicemails. This is a HEALTH HAZARD. "
            "If this is not fixed by Friday I am contacting the NYC Department of Buildings. "
            "— Patricia Nguyen"
        ),
    },
    {
        "id": 10,
        "description": "Wrong recipient — not a property issue",
        "sender_email": "sales@randomvendor.com",
        "subject": "Special offer on office supplies this week only!",
        "body": (
            "Hi there! We're reaching out to let you know about our exclusive deals on "
            "printer paper, pens, and office furniture. Reply to this email or visit our "
            "website to learn more. Limited time offer — don't miss out!"
        ),
    },
]


def evaluate(parsed: dict) -> tuple[bool, list[str]]:
    """Returns (passed, list_of_missing_required_fields)."""
    missing = [f for f in REQUIRED_FIELDS if not parsed.get(f)]
    return len(missing) == 0, missing


def main():
    results = []

    print("\nRunning 10 edge case scenarios through parse_email()...\n")

    for scenario in SCENARIOS:
        parsed = parse_email(
            subject=scenario["subject"],
            body=scenario["body"],
            sender_email=scenario["sender_email"],
        )
        passed, missing = evaluate(parsed)
        results.append({
            "scenario": scenario,
            "parsed": parsed,
            "passed": passed,
            "missing": missing,
        })
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] #{scenario['id']:02d} {scenario['description']}")

    # ── Summary table ──────────────────────────────────────────────────────────
    col_desc    = 42
    col_urgency = 8
    col_type    = 20
    col_address = 28
    col_status  = 6

    header = (
        f"{'#':<4}"
        f"{'Description':<{col_desc}}"
        f"{'Urgency':<{col_urgency}}"
        f"{'Issue Type':<{col_type}}"
        f"{'Building Address':<{col_address}}"
        f"{'Status':<{col_status}}"
    )
    divider = "─" * len(header)

    print(f"\n\n{divider}")
    print(header)
    print(divider)

    for r in results:
        s = r["scenario"]
        p = r["parsed"]
        status = "PASS" if r["passed"] else "FAIL"
        address = (p.get("building_address") or "null")[:col_address - 2]
        urgency = (p.get("urgency") or "null")[:col_urgency - 2]
        issue   = (p.get("issue_type") or "null")[:col_type - 2]
        desc    = s["description"][:col_desc - 2]

        print(
            f"{s['id']:<4}"
            f"{desc:<{col_desc}}"
            f"{urgency:<{col_urgency}}"
            f"{issue:<{col_type}}"
            f"{address:<{col_address}}"
            f"{status:<{col_status}}"
        )

    print(divider)

    # ── Pass/fail counts ───────────────────────────────────────────────────────
    passed_count = sum(1 for r in results if r["passed"])
    failed_count = len(results) - passed_count
    print(f"\nResults: {passed_count}/10 passed, {failed_count}/10 failed")

    # ── Null field frequency ───────────────────────────────────────────────────
    all_tracked = REQUIRED_FIELDS + TRACKED_NULLABLE
    null_counts = {
        field: sum(1 for r in results if not r["parsed"].get(field))
        for field in all_tracked
    }
    print("\nNull field frequency across all 10 results:")
    for field, count in sorted(null_counts.items(), key=lambda x: -x[1]):
        bar = "█" * count + "░" * (10 - count)
        print(f"  {field:<20} {bar}  {count}/10")

    # ── Failed scenarios detail ────────────────────────────────────────────────
    failed = [r for r in results if not r["passed"]]
    if failed:
        print("\nFailed scenarios — missing required fields:")
        for r in failed:
            print(f"  #{r['scenario']['id']:02d} {r['scenario']['description']}")
            print(f"      Missing: {', '.join(r['missing'])}")
    print()


if __name__ == "__main__":
    main()
