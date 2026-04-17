"""
main.py — Entry point for PropFlow Phase 1 + Phase 2.

Phase 1 pipeline (per email):
  1. Fetch unread emails from Gmail.
  2. Parse with Claude → structured dict.
  3. Log to Google Sheets (Tenant Requests tab).
  4. Send ACK reply to tenant.
  5. Mark email as read.

Phase 2 pipeline (runs after Phase 1 ACK, never blocks it):
  6. Look up building record by address.
  7. Look up best vendor by issue type, geography, and urgency.
  8. Determine if client approval is required.
  9. Send super notification + vendor outreach in parallel.
  10. Update the Sheets row with Phase 2 routing fields.
"""

import base64
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from gmail_client import fetch_unread_emails, send_email, mark_email_as_read
from sheets_client import append_row, update_row
from agents.email_parser import parse_email, build_acknowledgment
from agents.router import (
    lookup_building,
    lookup_vendor,
    check_approval_required,
    build_super_notification,
    build_vendor_outreach,
)
from agents.notifier import notify_super, notify_vendor

POLL_INTERVAL_SECONDS = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("propflow")


def run_phase2(parsed: dict, row_number: int | None) -> None:
    """
    Executes the Phase 2 routing pipeline for a single parsed request.
    Any failure here is caught and logged — Phase 1 is never affected.

    Args:
        parsed: The structured dict from parse_email().
        row_number: The Sheets row number returned by append_row(), used to
                    write Phase 2 fields back to the same row. If None, the
                    row update step is skipped.
    """
    address = parsed.get("building_address") or ""
    issue_type = parsed.get("issue_type") or ""
    urgency = parsed.get("urgency") or "Low"
    subject = parsed.get("issue_description") or address or "unknown"

    logger.info("Phase 2 starting for email: %s", subject)

    # ── Step 6: Building lookup ───────────────────────────────────────────────
    building = lookup_building(address)
    logger.info("Phase 2 building lookup: %s", building)
    if not building:
        logger.warning("Phase 2: building not found for address %r — stopping routing.", address)
        return

    geography = building.get("borough_city", "")
    building_id = building.get("building_id") or None

    # ── Step 7: Vendor lookup (Phase 2B: tries contract preferred vendors first)
    vendor = lookup_vendor(issue_type, geography, urgency, building_id=building_id)
    logger.info("Phase 2 vendor lookup (building_id=%r): %s", building_id, vendor)

    # ── Step 8: Approval check ────────────────────────────────────────────────
    threshold = building.get("approval_threshold", "")
    approval_required = check_approval_required(issue_type, urgency, threshold)
    logger.info("Phase 2 approval required: %s", approval_required)

    # ── Step 9: Send super + vendor notifications in parallel ─────────────────
    request_subject = (
        f"[PropFlow] {urgency} {issue_type} — "
        f"{parsed.get('building_address') or 'Unknown Address'}"
    )

    super_body = build_super_notification(parsed, building, vendor)
    super_sent = False
    vendor_sent = False

    tasks = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks["super"] = pool.submit(
            notify_super, building, request_subject, super_body
        )
        if vendor:
            vendor_body = build_vendor_outreach(parsed, building)
            tasks["vendor"] = pool.submit(
                notify_vendor, vendor, request_subject, vendor_body
            )

    super_sent = tasks["super"].result() if "super" in tasks else False
    vendor_sent = tasks["vendor"].result() if "vendor" in tasks else False

    # ── Step 10: Update Sheets row with Phase 2 fields ────────────────────────
    if row_number:
        phase2_fields = {
            "vendor_assigned":       vendor.get("vendor_name", "") if vendor else "",
            "vendor_email":          vendor.get("email", "") if vendor else "",
            "vendor_contact_method": vendor.get("contact_method", "") if vendor else "",
            "super_notified":        str(super_sent),
            "approval_required":     str(approval_required),
        }
        logger.info("Phase 2 update_row called with row_number: %s fields: %s", row_number, phase2_fields)
        result = update_row(row_number, phase2_fields)
        logger.info("Phase 2 sheets update result: %s", result)

    logger.info("Phase 2 complete")


def process_email(email: dict) -> None:
    """
    Runs the full Phase 1 + Phase 2 pipeline for a single unread email.

    Args:
        email: A dict as returned by fetch_unread_emails(), containing:
               id, thread_id, subject, body, sender_name, sender_email.
    """
    msg_id = email["id"]
    sender = email["sender_email"]
    subject = email["subject"]

    logger.info("Processing email id=%s from=%s subject=%r", msg_id, sender, subject)

    # ── Phase 1: Parse ────────────────────────────────────────────────────────
    parsed = parse_email(
        subject=subject,
        body=email["body"],
        sender_email=sender,
    )

    if not parsed:
        logger.warning("Parsing failed for email id=%s — skipping.", msg_id)
        return

    logger.info(
        "Parsed  issue_type=%r  urgency=%r  tenant=%r",
        parsed.get("issue_type"),
        parsed.get("urgency"),
        parsed.get("tenant_name"),
    )

    # ── Filter: skip non-tenant emails ────────────────────────────────────────
    if parsed.get("issue_type") == "Other" and not (parsed.get("issue_description") or "").strip():
        logger.info("Skipped %r — does not appear to be a tenant request.", subject)
        mark_email_as_read(msg_id)
        return

    # ── Phase 1: Log to Google Sheets ─────────────────────────────────────────
    row_number = append_row(parsed)
    logger.info("Logged to Google Sheets (row %s).", row_number)
    logger.info("Phase 1 append_row returned row_number: %s", row_number)

    # ── Phase 1: Send ACK reply ───────────────────────────────────────────────
    ack_body = build_acknowledgment(parsed)
    send_email(
        to_address=sender,
        subject=subject,
        body=ack_body,
        thread_id=email["thread_id"],
        message_id=email.get("message_id"),
    )
    logger.info("Acknowledgment sent to %s.", sender)

    # ── Phase 1: Mark as read ─────────────────────────────────────────────────
    mark_email_as_read(msg_id)
    logger.info("Marked email id=%s as read.", msg_id)

    # ── Phase 2: Routing (isolated — never breaks Phase 1) ───────────────────
    try:
        run_phase2(parsed, row_number)
    except Exception as e:
        logger.error("Phase 2 routing failed for email id=%s: %s", msg_id, e, exc_info=True)


def run() -> None:
    """
    Main polling loop. Checks for unread emails every POLL_INTERVAL_SECONDS.
    A failure on any single email is caught and logged so the loop continues.
    """
    # ── Railway credentials bootstrap ────────────────────────────────────────
    b64 = os.environ.get("GOOGLE_CREDENTIALS_B64")
    if b64:
        with open("credentials.json", "wb") as f:
            f.write(base64.b64decode(b64))
        logger.info("credentials.json written from GOOGLE_CREDENTIALS_B64.")
    # ─────────────────────────────────────────────────────────────────────────

    logger.info("PropFlow started. Polling every %ds.", POLL_INTERVAL_SECONDS)

    while True:
        try:
            emails = fetch_unread_emails()
        except Exception as e:
            logger.error("Failed to fetch emails: %s", e)
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        if not emails:
            logger.info("No unread emails found.")
        else:
            logger.info("Found %d unread email(s).", len(emails))
            for email in emails:
                try:
                    process_email(email)
                except Exception as e:
                    logger.error(
                        "Unhandled error processing email id=%s: %s",
                        email.get("id"),
                        e,
                        exc_info=True,
                    )

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
