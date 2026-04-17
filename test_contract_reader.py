"""
test_contract_reader.py — Manual integration tests for Phase 2B contract lookup.

Tests:
  1. BLD001 — expects a PDF in Drive; prints extracted terms.
  2. BLD003 — expects approval_threshold ≈ 3500 if configured.
  3. BLD999 — non-existent building; must return safe defaults without crashing.
  4. None   — None building_id; must return safe defaults immediately.

Run with:
    source venv/bin/activate
    python test_contract_reader.py
"""

import json
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("test_contract_reader")

from agents.contract_reader import get_contract_terms, find_agreement, get_drive_service


def _print_result(label: str, result: dict) -> None:
    print(f"\n{'─' * 60}")
    print(f"  {label}")
    print(f"{'─' * 60}")
    print(json.dumps(result, indent=2))


def test_safe_defaults_on_none() -> bool:
    """get_contract_terms(None) must return safe defaults immediately."""
    result = get_contract_terms(None)
    ok = (
        result["approval_threshold"] is None
        and result["emergency_authority"] is None
        and result["preferred_vendors"] == []
        and result["notice_requirements"] is None
    )
    _print_result("Test: None building_id → safe defaults", result)
    print(f"  PASS" if ok else "  FAIL — expected safe defaults")
    return ok


def test_nonexistent_building() -> bool:
    """BLD999 has no PDF in Drive; must return safe defaults without crashing."""
    result = get_contract_terms("BLD999")
    ok = (
        isinstance(result, dict)
        and result["preferred_vendors"] == []
    )
    _print_result("Test: BLD999 (non-existent) → safe defaults", result)
    print(f"  PASS" if ok else "  FAIL — expected safe defaults dict")
    return ok


def test_bld001() -> bool:
    """BLD001 — attempts real Drive lookup. Prints terms; passes if no exception raised."""
    try:
        result = get_contract_terms("BLD001")
        _print_result("Test: BLD001 — extracted terms", result)
        print("  PASS (no exception)")
        return True
    except Exception as e:
        print(f"\n  FAIL — BLD001 raised: {e}")
        return False


def test_bld003_threshold() -> bool:
    """
    BLD003 — if a PDF exists, approval_threshold should be ~3500.
    If no PDF exists, safe defaults are returned (also acceptable).
    """
    try:
        result = get_contract_terms("BLD003")
        _print_result("Test: BLD003 — extracted terms", result)
        threshold = result.get("approval_threshold")
        if threshold is None:
            print("  INFO — No PDF found for BLD003; safe defaults returned (acceptable).")
            return True
        ok = abs(float(threshold) - 3500) < 1
        print(f"  {'PASS' if ok else 'FAIL'} — approval_threshold={threshold} (expected ~3500)")
        return ok
    except Exception as e:
        print(f"\n  FAIL — BLD003 raised: {e}")
        return False


def test_drive_service_auth() -> bool:
    """Drive service must authenticate without errors."""
    try:
        svc = get_drive_service()
        # A trivial API call to verify credentials are valid
        svc.files().list(pageSize=1, fields="files(id)").execute()
        print("\n  Drive auth: PASS")
        return True
    except Exception as e:
        print(f"\n  Drive auth: FAIL — {e}")
        return False


def diagnostic_list_drive_files() -> None:
    """Lists the first 20 files/folders visible to the service account."""
    print(f"\n{'─' * 60}")
    print("  Diagnostic: files visible to service account (first 20)")
    print(f"{'─' * 60}")
    try:
        svc = get_drive_service()
        resp = svc.files().list(
            pageSize=20,
            fields="files(id, name, mimeType)",
            orderBy="name",
        ).execute()
        files = resp.get("files", [])
        if not files:
            print("  (no files visible — check Drive sharing)")
        for f in files:
            mime_short = f["mimeType"].replace("application/vnd.google-apps.", "gapps/")
            print(f"  {f['name']:<45} {mime_short:<25} {f['id']}")
    except Exception as e:
        print(f"  ERROR — {e}")


if __name__ == "__main__":
    print("\n=== PropFlow Phase 2B — Contract Reader Tests ===\n")

    diagnostic_list_drive_files()

    results = [
        test_safe_defaults_on_none(),
        test_nonexistent_building(),
        test_drive_service_auth(),
        test_bld001(),
        test_bld003_threshold(),
    ]

    passed = sum(results)
    total = len(results)
    print(f"\n{'═' * 60}")
    print(f"  {passed}/{total} tests passed")
    print(f"{'═' * 60}\n")

    sys.exit(0 if passed == total else 1)
