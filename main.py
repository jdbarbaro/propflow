"""
main.py — Entry point for PropFlow (Phase 1 + Phase 2A/2B + two-cycle approval).

CYCLE 1 — triggered by a new tenant email:
  1. Parse email with Claude (Phase 1).
  2. Log to Tenant Requests tab.
  3. Send ACK reply to tenant.
  4. Look up building + contract terms (Phase 2A/2B).
  5. Email super: "Can you handle this in-house?"
  6. Write request to Pending tab (status: Awaiting Super).
  Stop — do not dispatch vendor yet.

CYCLE 2 — triggered by the super's reply:
  7. Match reply to open Pending row by Gmail thread ID.
  8. Parse reply: can_handle or needs_vendor.
  If can_handle:
    → Notify tenant their super is handling it.
    → Mark Pending row "In-House".
    → Update Tenant Requests row.
  If needs_vendor:
    → Look up best vendor (contract preferred vendors first).
    → Dispatch vendor outreach email.
    → Notify tenant a vendor has been assigned.
    → Mark Pending row "Vendor Dispatched".
    → Update Tenant Requests row with vendor details.
"""

import base64
import logging
import os
import time

from gmail_client import fetch_unread_emails, send_email, mark_email_as_read
from sheets_client import (
    append_row,
    update_row,
    write_pending_request,
    get_pending_by_thread,
    update_pending_status,
)
from agents.email_parser import parse_email, build_acknowledgment, parse_super_reply
from agents.router import (
    lookup_building,
    lookup_vendor,
    check_approval_required,
    build_super_approval_request,
    build_super_notification,
    build_vendor_outreach,
)
from agents.notifier import notify_super, notify_vendor, notify_tenant

POLL_INTERVAL_SECONDS = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("propflow")


# ── Cycle 1 ───────────────────────────────────────────────────────────────────

def run_cycle1(parsed: dict, row_number: int | None) -> None:
    """
    Executes Cycle 1 after Phase 1 is complete: looks up the building and
    contract, emails the super asking if they can handle it in-house, and
    writes the request to the Pending tab to await the super's reply.

    Any failure is caught and logged — Phase 1 (ACK to tenant) is never affected.

    Args:
        parsed: Structured dict from parse_email().
        row_number: Tenant Requests row number from append_row(), stored in
                    the Pending row so Cycle 2 can update it.
    """
    address  = parsed.get("building_address") or ""
    issue_type = parsed.get("issue_type") or ""
    urgency  = parsed.get("urgency") or "Low"

    logger.info("Cycle 1 starting — address=%r issue=%r urgency=%r", address, issue_type, urgency)

    # ── Building lookup ───────────────────────────────────────────────────────
    building = lookup_building(address)
    if not building:
        logger.warning("Cycle 1: building not found for %r — cannot route.", address)
        return

    building_id = building.get("building_id") or None
    logger.info("Cycle 1 building: %s (id=%r)", building.get("full_address"), building_id)

    # ── Email super: can you handle this in-house? ────────────────────────────
    request_subject = (
        f"[PropFlow] {urgency} {issue_type} — "
        f"{building.get('full_address') or address}"
    )
    approval_body = build_super_approval_request(parsed, building)
    super_sent, super_thread_id = notify_super(building, request_subject, approval_body)

    if not super_sent:
        logger.warning("Cycle 1: super notification failed — Pending row not written.")
        return

    logger.info("Cycle 1 super approval request sent (thread_id=%s).", super_thread_id)

    # ── Write to Pending tab ──────────────────────────────────────────────────
    pending_row = write_pending_request({
        "building_id":          building_id,
        "building_address":     building.get("full_address") or address,
        "super_email":          building.get("super_email") or "",
        "tenant_email":         parsed.get("tenant_email") or "",
        "unit_number":          parsed.get("unit_number") or "",
        "issue_type":           issue_type,
        "urgency":              urgency,
        "description":          parsed.get("issue_description") or "",
        "tenant_requests_row":  row_number,
        "super_thread_id":      super_thread_id,
    })
    logger.info("Cycle 1 complete — Pending row %s written.", pending_row)


def process_tenant_request(email: dict) -> None:
    """
    Full Cycle 1 pipeline for a new tenant email.

    Phase 1: parse → log to Sheets → ACK tenant → mark read.
    Cycle 1: building lookup → super approval email → write Pending row.
    """
    msg_id  = email["id"]
    sender  = email["sender_email"]
    subject = email["subject"]

    logger.info("Tenant request — id=%s from=%s subject=%r", msg_id, sender, subject)

    # ── Phase 1: Parse ────────────────────────────────────────────────────────
    parsed = parse_email(subject=subject, body=email["body"], sender_email=sender)
    if not parsed:
        logger.warning("Parse failed for email id=%s — skipping.", msg_id)
        return

    logger.info(
        "Parsed  issue_type=%r  urgency=%r  tenant=%r",
        parsed.get("issue_type"), parsed.get("urgency"), parsed.get("tenant_name"),
    )

    # ── Filter: skip non-tenant emails ───────────────────────────────────────
    if parsed.get("issue_type") == "Other" and not (parsed.get("issue_description") or "").strip():
        logger.info("Skipped %r — does not appear to be a tenant request.", subject)
        mark_email_as_read(msg_id)
        return

    # ── Phase 1: Log to Tenant Requests tab ──────────────────────────────────
    row_number = append_row(parsed)
    logger.info("Logged to Tenant Requests (row %s).", row_number)

    # ── Phase 1: Send ACK reply to tenant ────────────────────────────────────
    ack_body = build_acknowledgment(parsed)
    send_email(
        to_address=sender,
        subject=subject,
        body=ack_body,
        thread_id=email["thread_id"],
        message_id=email.get("message_id"),
    )
    logger.info("ACK sent to %s.", sender)

    # ── Phase 1: Mark as read ─────────────────────────────────────────────────
    mark_email_as_read(msg_id)
    logger.info("Marked email id=%s as read.", msg_id)

    # ── Cycle 1: Route to super (isolated — never breaks Phase 1) ────────────
    try:
        run_cycle1(parsed, row_number)
    except Exception as e:
        logger.error("Cycle 1 failed for email id=%s: %s", msg_id, e, exc_info=True)


# ── Cycle 2 ───────────────────────────────────────────────────────────────────

def process_super_reply(email: dict, pending: dict, pending_row: int) -> None:
    """
    Cycle 2 pipeline: handles a super's reply to a Cycle 1 approval request.

    Parses the super's reply, then either:
      - can_handle  → notifies tenant, closes the Pending row.
      - needs_vendor → looks up and dispatches a vendor, notifies tenant,
                       updates both Pending and Tenant Requests rows.

    Args:
        email: Unread email dict from fetch_unread_emails().
        pending: Pending row dict matched by thread_id.
        pending_row: 1-based row number in the Pending tab.
    """
    msg_id     = email["id"]
    issue_type = pending.get("issue_type") or ""
    urgency    = pending.get("urgency") or "Low"
    address    = pending.get("building_address") or ""
    building_id = pending.get("building_id") or None
    tenant_email = pending.get("tenant_email") or ""

    tenant_row = pending.get("tenant_requests_row")
    tenant_row_int = int(tenant_row) if tenant_row and str(tenant_row).isdigit() else None

    logger.info(
        "Cycle 2 — super reply from=%s thread_id=%s pending_row=%s",
        email["sender_email"], email["thread_id"], pending_row,
    )

    # ── Mark super's reply as read ────────────────────────────────────────────
    mark_email_as_read(msg_id)

    # ── Parse super's reply ───────────────────────────────────────────────────
    decision = parse_super_reply(email["subject"], email["body"])
    logger.info("Cycle 2 super decision: %r", decision)

    # ── Request subject for any outbound emails ───────────────────────────────
    request_subject = f"[PropFlow] {urgency} {issue_type} — {address}"

    if decision == "can_handle":
        # ── Super is handling it in-house ─────────────────────────────────────
        logger.info("Cycle 2: super handling in-house — notifying tenant and closing ticket.")

        if tenant_email:
            tenant_body = (
                f"Just to let you know — your building superintendent is taking care of "
                f"your {issue_type.lower()} request and will be in touch with you directly.\n\nJ"
            )
            notify_tenant(
                tenant_email=tenant_email,
                subject=request_subject,
                body=tenant_body,
            )

        update_pending_status(pending_row, "In-House", "Super handling in-house")

        if tenant_row_int:
            update_row(tenant_row_int, {
                "vendor_assigned":       "Super (In-House)",
                "vendor_email":          "",
                "vendor_contact_method": "in-house",
                "super_notified":        "True",
                "approval_required":     "False",
            })

        logger.info("Cycle 2 complete — ticket closed in-house.")

    else:
        # ── Super needs a vendor dispatched ───────────────────────────────────
        logger.info("Cycle 2: super needs vendor — looking up and dispatching.")

        # Re-look up building to get full dict (geography, approval threshold, etc.)
        building = lookup_building(address)
        if not building:
            logger.error("Cycle 2: building not found for %r — cannot dispatch vendor.", address)
            update_pending_status(pending_row, "Error", "Building not found for vendor dispatch")
            return

        geography = building.get("borough_city", "")

        # Vendor lookup — contract preferred vendors checked first
        vendor = lookup_vendor(issue_type, geography, urgency, building_id=building_id)
        logger.info("Cycle 2 vendor lookup: %s", vendor)

        if vendor:
            # Reconstruct a minimal parsed dict for the email builders
            parsed_for_body = {
                "issue_type":        issue_type,
                "issue_description": pending.get("description") or "",
                "unit_number":       pending.get("unit_number") or "",
                "urgency":           urgency,
                "tenant_name":       None,
                "tenant_email":      tenant_email,
            }
            vendor_body = build_vendor_outreach(parsed_for_body, building)
            notify_vendor(vendor, request_subject, vendor_body)
            logger.info("Cycle 2 vendor outreach sent to %s.", vendor.get("vendor_name"))

        # Notify tenant a vendor has been assigned
        if tenant_email:
            if vendor:
                tenant_body = (
                    f"We've assigned a vendor to address your {issue_type.lower()} request. "
                    f"They'll be in touch to arrange access.\n\nJ"
                )
            else:
                tenant_body = (
                    f"We're working on getting someone to address your "
                    f"{issue_type.lower()} request and will follow up shortly.\n\nJ"
                )
            notify_tenant(
                tenant_email=tenant_email,
                subject=request_subject,
                body=tenant_body,
            )

        # Update Pending tab
        vendor_note = vendor.get("vendor_name", "No vendor found") if vendor else "No vendor found"
        update_pending_status(pending_row, "Vendor Dispatched", vendor_note)

        # Update Tenant Requests row
        if tenant_row_int:
            threshold = building.get("approval_threshold", "")
            approval_required = check_approval_required(issue_type, urgency, threshold)
            update_row(tenant_row_int, {
                "vendor_assigned":       vendor.get("vendor_name", "") if vendor else "",
                "vendor_email":          vendor.get("email", "") if vendor else "",
                "vendor_contact_method": vendor.get("contact_method", "") if vendor else "",
                "super_notified":        "True",
                "approval_required":     str(approval_required),
            })

        logger.info("Cycle 2 complete — vendor dispatched.")


# ── Email router ──────────────────────────────────────────────────────────────

def process_email(email: dict) -> None:
    """
    Routes a single unread email to Cycle 1 or Cycle 2.

    Cycle 2 if the email's thread_id matches an open row in the Pending tab
    (i.e. it is a super replying to a PropFlow approval request).
    Cycle 1 for all other emails (new tenant requests).
    """
    thread_id = email.get("thread_id", "")

    # Check for a matching open Pending row first
    pending, pending_row = get_pending_by_thread(thread_id)
    if pending:
        logger.info(
            "Routing to Cycle 2 (super reply) — thread_id=%s pending_row=%s",
            thread_id, pending_row,
        )
        process_super_reply(email, pending, pending_row)
    else:
        process_tenant_request(email)


# ── Main polling loop ─────────────────────────────────────────────────────────

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
                        email.get("id"), e, exc_info=True,
                    )

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
