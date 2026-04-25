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
from datetime import datetime, timedelta, timezone

from gmail_client import fetch_unread_emails, fetch_thread_reply, send_email, mark_email_as_read
from sheets_client import (
    append_row,
    update_row,
    write_pending_request,
    get_pending_by_thread,
    get_awaiting_super_rows,
    update_pending_status,
    set_review_flag,
)
from agents.email_parser import parse_email, build_acknowledgment
from agents.router import (
    lookup_building,
    lookup_vendor,
    check_approval_required,
    build_super_approval_request,
    build_super_notification,
    build_vendor_outreach,
)
from agents.notifier import notify_super, notify_vendor, notify_tenant
from config import PROPFLOW_OWNER_EMAIL
from agents.super_gate import (
    build_inhouse_confirmation,
    build_escalation_email,
    build_tenant_holding_message,
    is_escalation_due,
    parse_super_reply,
)

POLL_INTERVAL_SECONDS = 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("propflow")


# ── Cycle 1 ───────────────────────────────────────────────────────────────────

def run_cycle1(
    parsed: dict,
    row_number: int | None,
    tenant_thread_id: str | None = None,
    tenant_message_id: str | None = None,
) -> None:
    """
    Executes Cycle 1 after Phase 1 is complete: looks up the building and
    contract, emails the super asking if they can handle it in-house, and
    writes the request to the Pending tab to await the super's reply.

    Any failure is caught and logged — Phase 1 (ACK to tenant) is never affected.

    Args:
        parsed: Structured dict from parse_email().
        row_number: Tenant Requests row number from append_row(), stored in
                    the Pending row so Cycle 2 can update it.
        tenant_thread_id: Gmail thread ID of the tenant's original email thread,
                          stored so Cycle 2 can reply in-thread.
        tenant_message_id: Gmail message ID of the last tenant email (or ACK),
                           stored so Cycle 2 can set In-Reply-To correctly.
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

    # ── Compute escalation deadline ───────────────────────────────────────────
    escalation_hours = 1 if urgency == "High" else 12
    escalate_at = (
        datetime.utcnow() + timedelta(hours=escalation_hours)
    ).strftime("%Y-%m-%d %H:%M:%S")
    logger.info(
        "Cycle 1 escalation window: %dh → escalate_at=%s", escalation_hours, escalate_at
    )

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
        "tenant_name":          parsed.get("tenant_name") or "",
        "tenant_thread_id":     tenant_thread_id or "",
        "tenant_message_id":    tenant_message_id or "",
        "escalate_at":          escalate_at,
    })
    logger.info("Cycle 1 complete — Pending row %s written.", pending_row)


def classify_sender(sender_email: str) -> str:
    """
    Classifies the sender of an incoming email against known super emails.

    Compares sender_email against the super_email field of every building
    in the Buildings tab:

      "super_exact"  — exact case-insensitive match with a known super email
      "super_fuzzy"  — domain matches a known super's domain but address differs
                       (e.g. newperson@hudsonarms.com when super is super@hudsonarms.com)
      "tenant"       — no match; treat as a normal tenant request

    Returns "tenant" on any lookup error so the pipeline is never blocked.

    Args:
        sender_email: The From address of the incoming email.

    Returns:
        One of: "super_exact", "super_fuzzy", "tenant".
    """
    from sheets_client import _read_tab
    from config import GOOGLE_BUILDINGS_SHEET

    sender_lower = sender_email.strip().lower()
    sender_domain = sender_lower.split("@")[-1] if "@" in sender_lower else ""

    try:
        buildings = _read_tab(GOOGLE_BUILDINGS_SHEET)
    except Exception as e:
        logger.warning("classify_sender: could not read Buildings tab: %s", e)
        return "tenant"

    for b in buildings:
        super_email = (b.get("super_email") or "").strip().lower()
        if not super_email:
            continue
        if sender_lower == super_email:
            return "super_exact"
        super_domain = super_email.split("@")[-1] if "@" in super_email else ""
        if super_domain and sender_domain == super_domain:
            return "super_fuzzy"

    return "tenant"


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

    # ── Phase 1: Classify sender (fuzzy super detection) ─────────────────────
    sender_class = classify_sender(sender)
    logger.info("classify_sender: %s → %r", sender, sender_class)
    if sender_class == "super_exact":
        # Super emailed directly — not a tenant request; skip
        logger.info("Skipping %r — sender is a known super (%s).", subject, sender)
        mark_email_as_read(msg_id)
        return

    # ── Phase 1: Log to Tenant Requests tab ──────────────────────────────────
    parsed["thread_id"]   = email.get("thread_id") or ""
    parsed["review_flag"] = "NEEDS_REVIEW" if sender_class == "super_fuzzy" else ""
    row_number = append_row(parsed)
    logger.info("Logged to Tenant Requests (row %s).", row_number)

    if sender_class == "super_fuzzy" and row_number:
        logger.warning(
            "classify_sender: fuzzy super match for %s — row %s flagged NEEDS_REVIEW.",
            sender, row_number,
        )
        # review_flag is already written inline by append_row via parsed["review_flag"]
        # set_review_flag() is a belt-and-suspenders write in case append_row is racing
        try:
            set_review_flag(row_number, "NEEDS_REVIEW")
        except Exception as e:
            logger.error("set_review_flag failed for row %s: %s", row_number, e)

    # ── Phase 1: Conditional ACK — only for High urgency ─────────────────────
    urgency = (parsed.get("urgency") or "Low").strip()
    if urgency == "High":
        ack_body = build_acknowledgment(parsed)
        tenant_thread_id, tenant_message_id = send_email(
            to_address=sender,
            subject=subject,
            body=ack_body,
            thread_id=email["thread_id"],
            message_id=email.get("message_id"),
        )
        logger.info("ACK sent to %s (thread_id=%s).", sender, tenant_thread_id)
    else:
        # Routine request — first email to tenant comes from Cycle 2
        tenant_thread_id  = email["thread_id"]
        tenant_message_id = email.get("message_id")
        logger.info(
            "Routine request (%s urgency) — ACK withheld until super responds.", urgency
        )

    # ── Phase 1: Mark as read ─────────────────────────────────────────────────
    mark_email_as_read(msg_id)
    logger.info("Marked email id=%s as read.", msg_id)

    # ── Cycle 1: Route to super (isolated — never breaks Phase 1) ────────────
    try:
        run_cycle1(
            parsed, row_number,
            tenant_thread_id=tenant_thread_id,
            tenant_message_id=tenant_message_id,
        )
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
    tenant_email      = pending.get("tenant_email") or ""
    tenant_thread_id  = pending.get("tenant_thread_id") or None
    tenant_message_id = pending.get("tenant_message_id") or None

    tenant_row = pending.get("tenant_requests_row")
    tenant_row_int = int(tenant_row) if tenant_row and str(tenant_row).isdigit() else None

    logger.info(
        "Cycle 2 — super reply from=%s thread_id=%s pending_row=%s",
        email["sender_email"], email["thread_id"], pending_row,
    )

    # ── Mark super's reply as read ────────────────────────────────────────────
    mark_email_as_read(msg_id)

    # ── Parse super's reply ───────────────────────────────────────────────────
    decision = parse_super_reply(email["body"])
    logger.info("Cycle 2 super decision: %r", decision)

    # ── Request subject for any outbound emails ───────────────────────────────
    request_subject = f"[PropFlow] {urgency} {issue_type} — {address}"

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if decision == "declined":
        # ── Super is handling it in-house ─────────────────────────────────────
        logger.info("Cycle 2: super handling in-house — notifying tenant and closing ticket.")

        if tenant_email:
            tenant_name = pending.get("tenant_name") or tenant_email
            building_for_confirm = lookup_building(address) or {}
            parsed_for_confirm = {
                "tenant_name": pending.get("tenant_name") or "",
                "issue_type":  issue_type,
            }
            logger.info("Building in-house confirmation for %s.", tenant_name)
            tenant_body = build_inhouse_confirmation(
                parsed_for_confirm, building_for_confirm, outcome="inhouse"
            )
            logger.info("Sending in-house confirmation to %s.", tenant_email)
            notify_tenant(
                tenant_email=tenant_email,
                subject=request_subject,
                body=tenant_body,
                thread_id=tenant_thread_id,
                message_id=tenant_message_id,
            )
            logger.info("In-house confirmation sent ✓")

        resolution_note = f"Super handling in-house. Confirmed at {now_str}"
        update_pending_status(pending_row, "In-House", resolution_note)

        if tenant_row_int:
            update_row(tenant_row_int, {
                "vendor_assigned":       "Super (In-House)",
                "vendor_email":          "",
                "vendor_contact_method": "in-house",
                "super_notified":        "True",
                "approval_required":     "False",
            })

        logger.info("Cycle 2 complete — ticket closed in-house.")

    elif decision == "unknown":
        # ── Super reply was ambiguous — flag for manual review ────────────────
        logger.warning("Cycle 2: super reply unclear — flagging for manual review.")
        reply_preview = (email.get("body") or "")[:100]
        resolution_note = (
            f"Super reply unclear — manual review required. "
            f"Reply received at {now_str}: {reply_preview}"
        )
        update_pending_status(pending_row, "Needs Review", resolution_note)
        logger.info("Cycle 2: Pending row marked 'Needs Review'.")

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
            tenant_name = pending.get("tenant_name") or tenant_email
            parsed_for_confirm = {
                "tenant_name": pending.get("tenant_name") or "",
                "issue_type":  issue_type,
            }
            logger.info("Building vendor dispatch confirmation for %s.", tenant_name)
            tenant_body = build_inhouse_confirmation(
                parsed_for_confirm, building, outcome="vendor", vendor=vendor
            )
            logger.info("Sending vendor dispatch confirmation to %s.", tenant_email)
            notify_tenant(
                tenant_email=tenant_email,
                subject=request_subject,
                body=tenant_body,
                thread_id=tenant_thread_id,
                message_id=tenant_message_id,
            )
            logger.info("Vendor dispatch confirmation sent ✓")

        # Update Pending tab
        vendor_name_str = vendor.get("vendor_name", "unknown vendor") if vendor else "no vendor found"
        resolution_note = (
            f"Vendor dispatched: {vendor_name_str}. Approved by super at {now_str}"
        )
        logger.info(
            "Cycle 2 calling update_pending_status: pending_row=%r status=%r note=%r",
            pending_row, "Vendor Dispatched", resolution_note,
        )
        update_pending_status(pending_row, "Vendor Dispatched", resolution_note)

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


# ── Pending thread poller ─────────────────────────────────────────────────────

def check_pending_threads() -> None:
    """
    Polls Gmail directly for replies on every 'Awaiting Super' thread.

    Called every poll cycle before fetch_unread_emails(). Detects super replies
    regardless of whether they've been opened/read in Gmail, which fixes the
    failure mode where opening a super reply in the inbox before PropFlow polls
    causes it to be invisible to the UNREAD label filter.

    Flow per pending row:
      1. Fetch the Gmail thread for super_thread_id.
      2. Find the first reply not from PropFlow and not a bounce.
      3. If found, run process_super_reply() — which marks the message as read
         and updates the Pending row status, preventing reprocessing.
      4. If not found, log and continue.
    """
    rows = get_awaiting_super_rows()
    if not rows:
        logger.debug("check_pending_threads: no rows awaiting super.")
        return

    logger.info("check_pending_threads: checking %d pending thread(s).", len(rows))

    for pending, pending_row in rows:
        super_tid    = pending.get("super_thread_id", "")
        super_email  = pending.get("super_email", "")
        issue_type   = pending.get("issue_type", "")
        address      = pending.get("building_address", "")

        if not super_tid or super_tid.startswith("dry_run_"):
            logger.debug(
                "check_pending_threads: skipping row %d (thread_id=%r).",
                pending_row, super_tid,
            )
            continue

        logger.info(
            "check_pending_threads: row %d — checking thread %s (%s, %s).",
            pending_row, super_tid, issue_type, address,
        )

        reply = fetch_thread_reply(super_tid, expected_sender=super_email)

        if reply:
            logger.info(
                "check_pending_threads: reply found from %s — routing to Cycle 2.",
                reply["sender_email"],
            )
            try:
                process_super_reply(reply, pending, pending_row)
            except Exception as e:
                logger.error(
                    "check_pending_threads: error processing reply for row %d: %s",
                    pending_row, e, exc_info=True,
                )
        else:
            logger.info(
                "check_pending_threads: no reply yet on thread %s (row %d).",
                super_tid, pending_row,
            )
            # ── Escalation check ──────────────────────────────────────────────
            logger.info(
                "check_pending_threads: row %d escalate_at=%r — checking if due.",
                pending_row, pending.get("escalate_at"),
            )
            if is_escalation_due(pending):
                logger.warning(
                    "check_pending_threads: escalation due for row %d (%s, %s) — notifying owner.",
                    pending_row, issue_type, address,
                )
                # Email owner
                escalation_body = build_escalation_email(pending)
                escalation_subject = (
                    f"[PropFlow] Escalation — super unresponsive: {issue_type} at {address}"
                )
                notify_tenant(
                    tenant_email=PROPFLOW_OWNER_EMAIL,
                    subject=escalation_subject,
                    body=escalation_body,
                )
                logger.info("Escalation email sent to owner (%s).", PROPFLOW_OWNER_EMAIL)

                # Holding message to tenant
                tenant_email = pending.get("tenant_email") or ""
                if tenant_email:
                    parsed_for_holding = {
                        "tenant_name": pending.get("tenant_name") or "",
                        "issue_type":  issue_type,
                    }
                    holding_body = build_tenant_holding_message(parsed_for_holding)
                    holding_subject = f"[PropFlow] Update on your {issue_type} request"
                    notify_tenant(
                        tenant_email=tenant_email,
                        subject=holding_subject,
                        body=holding_body,
                        thread_id=pending.get("tenant_thread_id") or None,
                        message_id=pending.get("tenant_message_id") or None,
                    )
                    logger.info("Tenant holding message sent to %s.", tenant_email)

                # Mark row as Escalated so it won't fire again
                now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                update_pending_status(
                    pending_row,
                    "Escalated",
                    f"No super response by escalate_at. Owner notified at {now_str}.",
                )
                logger.info("check_pending_threads: row %d marked Escalated.", pending_row)


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
        # ── Cycle 2: poll pending threads for super replies ───────────────────
        # Runs before UNREAD fetch so any reply detected here is marked as read
        # before fetch_unread_emails() runs, preventing double-processing.
        try:
            check_pending_threads()
        except Exception as e:
            logger.error("Failed to check pending threads: %s", e)

        # ── Cycle 1: check inbox for new tenant emails ────────────────────────
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
