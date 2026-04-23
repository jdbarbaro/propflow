"""
test_super_gate.py — Tests for agents/super_gate.py.

No live Gmail or Sheets connection required. Anthropic API is called for
build_super_inquiry() and parse_super_reply().

Run with:
    source venv/bin/activate
    python test_super_gate.py
"""

import logging
import sys
from datetime import datetime, timedelta, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

from agents.super_gate import (
    build_super_inquiry,
    parse_super_reply,
    is_escalation_due,
    build_escalation_email,
    build_inhouse_confirmation,
    build_tenant_holding_message,
)

# ── Shared fixtures ───────────────────────────────────────────────────────────

FAKE_PARSED = {
    "tenant_name":       "Linda Park",
    "tenant_email":      "linda.park@tenant.com",
    "building_address":  "88 Lexington Avenue, New York, NY 10016",
    "unit_number":       "2B",
    "issue_type":        "HVAC",
    "issue_description": "No heat since last night — very cold inside.",
    "urgency":           "High",
}

FAKE_BUILDING = {
    "building_id":        "BLD003",
    "full_address":       "88 Lexington Avenue, New York, NY 10016",
    "client_name":        "Prescott Realty Partners",
    "borough_city":       "Manhattan",
    "super_name":         "James Okafor",
    "super_email":        "j.okafor@prescott88.com",
    "approval_threshold": "$3,500",
}

FAKE_VENDOR = {
    "vendor_name":    "AirPro HVAC",
    "trade":          "HVAC",
    "email":          "sandra@airprohvac.com",
    "contact_method": "email",
    "geography":      "NYC",
}

FAKE_PENDING_ROW = {
    "building_address": "88 Lexington Avenue, New York, NY 10016",
    "issue_type":       "HVAC",
    "urgency":          "High",
    "description":      "No heat since last night — very cold inside.",
    "timestamp":        (datetime.now(timezone.utc) - timedelta(hours=3, minutes=22)).isoformat(),
    "escalate_at":      (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
}


# ── Test functions ────────────────────────────────────────────────────────────

def test_build_super_inquiry() -> bool:
    print("\n" + "─" * 60)
    print("  Test: build_super_inquiry()")
    print("─" * 60)
    try:
        result = build_super_inquiry(FAKE_PARSED, FAKE_BUILDING, FAKE_VENDOR)
        print(result)
        ok = isinstance(result, str) and len(result) > 20
        print(f"\n  {'PASS' if ok else 'FAIL'} — returned {len(result)}-char string")
        return ok
    except Exception as e:
        print(f"  FAIL — raised: {e}")
        return False


def test_build_super_inquiry_no_vendor() -> bool:
    print("\n" + "─" * 60)
    print("  Test: build_super_inquiry() — no vendor")
    print("─" * 60)
    try:
        result = build_super_inquiry(FAKE_PARSED, FAKE_BUILDING, None)
        print(result)
        ok = isinstance(result, str) and len(result) > 20
        print(f"\n  {'PASS' if ok else 'FAIL'}")
        return ok
    except Exception as e:
        print(f"  FAIL — raised: {e}")
        return False


def test_parse_super_reply_raw() -> bool:
    """Calls parse_super_reply for HANDLE and APPROVE and prints raw Claude output."""
    print("\n" + "─" * 60)
    print("  Test: parse_super_reply() — raw Claude response check")
    print("─" * 60)

    import anthropic
    from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    for input_text in ("HANDLE", "APPROVE"):
        prompt = (
            "Classify this building superintendent's reply to a maintenance dispatch request.\n"
            "Return ONLY one word — no punctuation, no explanation:\n\n"
            "  approved  — super wants the vendor dispatched "
            "(APPROVE / yes / go ahead / send them / please send / dispatch)\n"
            "  declined  — super will handle it themselves "
            "(HANDLE / handle / no / we got it / in-house / will manage / taking care of it / "
            "on it / decline / declined / we'll handle / not needed / cancel / no need)\n"
            "  unknown   — ambiguous, unclear, or unrelated\n\n"
            f"Reply:\n{input_text}"
        )
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=10,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        print(f"  input={input_text!r:10}  raw Claude response={raw!r}")

    return True


def test_parse_super_reply() -> bool:
    print("\n" + "─" * 60)
    print("  Test: parse_super_reply() — 5 inputs")
    print("─" * 60)

    cases = [
        ("APPROVE",                "approved"),
        ("yes go ahead",           "approved"),
        ("HANDLE",                 "declined"),
        ("we'll take care of it",  "declined"),
        ("not sure what to do",    "unknown"),
    ]

    all_pass = True
    for body, expected in cases:
        result = parse_super_reply(body)
        ok = result == expected
        all_pass = all_pass and ok
        status = "PASS" if ok else f"FAIL (got {result!r}, expected {expected!r})"
        print(f"  {status:<10}  input={body!r}")

    return all_pass


def test_is_escalation_due() -> bool:
    print("\n" + "─" * 60)
    print("  Test: is_escalation_due()")
    print("─" * 60)

    past   = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()

    past_result   = is_escalation_due({"escalate_at": past})
    future_result = is_escalation_due({"escalate_at": future})
    missing_result = is_escalation_due({})
    bad_result    = is_escalation_due({"escalate_at": "not-a-date"})

    cases = [
        (past_result,    True,  f"past timestamp ({past[:19]})"),
        (future_result,  False, f"future timestamp ({future[:19]})"),
        (missing_result, False, "missing escalate_at"),
        (bad_result,     False, "unparseable escalate_at"),
    ]

    all_pass = True
    for result, expected, label in cases:
        ok = result == expected
        all_pass = all_pass and ok
        print(f"  {'PASS' if ok else 'FAIL'}  {label} → {result}")

    return all_pass


def test_build_escalation_email() -> bool:
    print("\n" + "─" * 60)
    print("  Test: build_escalation_email()")
    print("─" * 60)
    try:
        result = build_escalation_email(FAKE_PENDING_ROW)
        print(result)
        ok = (
            isinstance(result, str)
            and "88 Lexington" in result
            and "HVAC" in result
        )
        print(f"\n  {'PASS' if ok else 'FAIL'}")
        return ok
    except Exception as e:
        print(f"  FAIL — raised: {e}")
        return False


def test_build_inhouse_confirmation_inhouse() -> bool:
    print("\n" + "─" * 60)
    print("  Test: build_inhouse_confirmation() — outcome=inhouse")
    print("─" * 60)
    try:
        result = build_inhouse_confirmation(FAKE_PARSED, FAKE_BUILDING, outcome="inhouse")
        print(result)
        ok = (
            isinstance(result, str)
            and len(result) > 20
            and "Linda" in result
        )
        print(f"\n  {'PASS' if ok else 'FAIL'}")
        return ok
    except Exception as e:
        print(f"  FAIL — raised: {e}")
        return False


def test_build_inhouse_confirmation_vendor() -> bool:
    print("\n" + "─" * 60)
    print("  Test: build_inhouse_confirmation() — outcome=vendor")
    print("─" * 60)
    try:
        result = build_inhouse_confirmation(
            FAKE_PARSED, FAKE_BUILDING, outcome="vendor", vendor=FAKE_VENDOR
        )
        print(result)
        ok = (
            isinstance(result, str)
            and len(result) > 20
            and "Linda" in result
            and "AirPro" in result
        )
        print(f"\n  {'PASS' if ok else 'FAIL'}")
        return ok
    except Exception as e:
        print(f"  FAIL — raised: {e}")
        return False


def test_build_tenant_holding_message() -> bool:
    print("\n" + "─" * 60)
    print("  Test: build_tenant_holding_message()")
    print("─" * 60)
    try:
        result = build_tenant_holding_message(FAKE_PARSED)
        print(result)
        ok = (
            isinstance(result, str)
            and len(result) > 20
            and "Linda" in result
        )
        print(f"\n  {'PASS' if ok else 'FAIL'}")
        return ok
    except Exception as e:
        print(f"  FAIL — raised: {e}")
        return False


# ── Runner ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n=== PropFlow — super_gate.py Tests ===")

    results = [
        test_parse_super_reply_raw(),
        test_build_super_inquiry(),
        test_build_super_inquiry_no_vendor(),
        test_parse_super_reply(),
        test_is_escalation_due(),
        test_build_escalation_email(),
        test_build_inhouse_confirmation_inhouse(),
        test_build_inhouse_confirmation_vendor(),
        test_build_tenant_holding_message(),
    ]

    passed = sum(results)
    total  = len(results)
    print(f"\n{'═' * 60}")
    print(f"  {passed}/{total} tests passed")
    print(f"{'═' * 60}\n")

    sys.exit(0 if passed == total else 1)
