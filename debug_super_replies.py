"""
debug_super_replies.py — Diagnoses why super replies aren't being detected.

Checks:
  a. All "Awaiting Super" rows in the Pending tab (with super_thread_id)
  b. All unread emails in the inbox (subject, from, thread_id, message_id)
  c. Thread ID match between pending rows and unread emails
  d. If no match, fetches the super's thread directly from Gmail to check
     whether it exists and whether messages are marked read/unread

Run with:
    source venv/bin/activate
    python3 debug_super_replies.py
"""

import sys
from gmail_client import get_gmail_service, fetch_unread_emails
from sheets_client import get_sheets_service
from config import GOOGLE_SPREADSHEET_ID, GOOGLE_PENDING_SHEET


def get_awaiting_super_rows() -> list[dict]:
    """Returns all Pending rows with status 'Awaiting Super'."""
    service = get_sheets_service()
    resp = service.spreadsheets().values().get(
        spreadsheetId=GOOGLE_SPREADSHEET_ID,
        range=f"{GOOGLE_PENDING_SHEET}!A:Z",
    ).execute()
    values = resp.get("values", [])
    if not values or len(values) < 2:
        return []
    headers = [h.strip().lower().replace(" ", "_") for h in values[0]]
    rows = []
    for row_idx, row in enumerate(values[1:], start=2):
        padded = row + [""] * (len(headers) - len(row))
        d = dict(zip(headers, padded))
        if d.get("status", "").strip().lower() == "awaiting super":
            d["_row_number"] = row_idx
            rows.append(d)
    return rows


def main():
    print("\n" + "═" * 65)
    print("  PropFlow — Super Reply Detection Diagnostic")
    print("═" * 65)

    # ── a. Awaiting Super rows ────────────────────────────────────────────────
    print("\n── a. Pending rows with status 'Awaiting Super' ─────────────────\n")
    pending_rows = get_awaiting_super_rows()

    if not pending_rows:
        print("  (none — no rows currently awaiting super approval)")
    else:
        for p in pending_rows:
            print(f"  Row {p['_row_number']}:")
            print(f"    building_address : {p.get('building_address')!r}")
            print(f"    issue_type       : {p.get('issue_type')!r}")
            print(f"    super_email      : {p.get('super_email')!r}")
            print(f"    super_thread_id  : {p.get('super_thread_id')!r}")
            print(f"    tenant_email     : {p.get('tenant_email')!r}")
            print(f"    timestamp        : {p.get('timestamp')!r}")
            print()

    # ── b. Unread emails ──────────────────────────────────────────────────────
    print("── b. Unread emails in inbox ─────────────────────────────────────\n")
    unread = fetch_unread_emails()

    if not unread:
        print("  (no unread emails found)")
    else:
        for em in unread:
            print(f"  id         : {em['id']}")
            print(f"  subject    : {em['subject']!r}")
            print(f"  from       : {em['sender_email']!r}")
            print(f"  thread_id  : {em['thread_id']!r}")
            print(f"  message_id : {em.get('message_id')!r}")
            print()

    # ── c. Match check ────────────────────────────────────────────────────────
    print("── c. Thread ID match: pending ↔ unread emails ──────────────────\n")

    if not pending_rows:
        print("  No pending rows to match against.")
    elif not unread:
        print("  No unread emails to match against.")
    else:
        unread_thread_ids = {em["thread_id"] for em in unread}
        for p in pending_rows:
            super_tid = p.get("super_thread_id", "")
            if super_tid in unread_thread_ids:
                matched = next(e for e in unread if e["thread_id"] == super_tid)
                print(f"  ✓ MATCH FOUND — row {p['_row_number']} thread_id={super_tid!r}")
                print(f"    Matched email from: {matched['sender_email']!r}")
                print(f"    Subject: {matched['subject']!r}")
            else:
                print(f"  ✗ NO MATCH — row {p['_row_number']} super_thread_id={super_tid!r}")
        print()

    # ── d. Fetch super thread directly from Gmail ─────────────────────────────
    print("── d. Direct Gmail thread lookup (checking read/unread status) ───\n")

    if not pending_rows:
        print("  No pending rows to inspect.")
        print()
        return

    gmail = get_gmail_service()

    for p in pending_rows:
        super_tid = p.get("super_thread_id", "")
        if not super_tid or super_tid.startswith("dry_run_"):
            print(f"  Row {p['_row_number']}: skipping (thread_id={super_tid!r})")
            print()
            continue

        print(f"  Row {p['_row_number']} — fetching thread {super_tid!r}")
        try:
            thread = gmail.users().threads().get(
                userId="me", id=super_tid, format="metadata",
                metadataHeaders=["Subject", "From", "Date"],
            ).execute()

            messages = thread.get("messages", [])
            print(f"  Thread has {len(messages)} message(s):")
            for msg in messages:
                headers = {h["name"].lower(): h["value"]
                           for h in msg.get("payload", {}).get("headers", [])}
                labels = msg.get("labelIds", [])
                read_status = "UNREAD" if "UNREAD" in labels else "read"
                inbox_status = "INBOX" if "INBOX" in labels else "not in INBOX"
                print(f"    msg_id   : {msg['id']!r}")
                print(f"    subject  : {headers.get('subject')!r}")
                print(f"    from     : {headers.get('from')!r}")
                print(f"    date     : {headers.get('date')!r}")
                print(f"    labels   : {labels}")
                print(f"    status   : {read_status} / {inbox_status}")
                print()

        except Exception as e:
            print(f"  ERROR fetching thread {super_tid!r}: {e}")
            print()

    print("═" * 65)
    print("  Diagnostic complete.")
    print("═" * 65 + "\n")


if __name__ == "__main__":
    main()
