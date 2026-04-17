"""
agents/notifier.py — Sends notification emails to supers, vendors, and tenants
via the Gmail API.

When DRY_RUN=true, emails are printed to the terminal instead of sent.
"""

import logging
import time
from gmail_client import send_email
from config import DRY_RUN

logger = logging.getLogger(__name__)


def _dry_run_log(recipient: str, subject: str, body: str) -> None:
    """Prints the full email to terminal in place of sending it."""
    logger.info(
        "[DRY RUN] Would send email:\n"
        "  To:      %s\n"
        "  Subject: %s\n"
        "  Body:\n%s",
        recipient,
        subject,
        "\n".join(f"    {line}" for line in body.splitlines()),
    )


def notify_super(
    building: dict,
    subject: str,
    body: str,
    gmail_service=None,
) -> tuple[bool, str | None]:
    """
    Sends a notification email to the building super.
    When DRY_RUN=true, prints the email to terminal.

    Returns:
        (sent: bool, thread_id: str | None)
        thread_id is the Gmail thread ID of the sent message, used by Cycle 2
        to match the super's reply back to this pending request.
        In DRY_RUN mode, a placeholder thread_id is returned so the Pending
        tab write still works during local testing.
    """
    super_email = building.get("super_email", "").strip()
    if not super_email:
        logger.warning(
            "No super_email on building %r — skipping super notification.",
            building.get("full_address"),
        )
        return False, None

    if DRY_RUN:
        _dry_run_log(super_email, subject, body)
        return True, f"dry_run_{int(time.time())}"

    try:
        thread_id = send_email(to_address=super_email, subject=subject, body=body)
        logger.info("Super notification sent to %s (thread_id=%s).", super_email, thread_id)
        return True, thread_id
    except Exception as e:
        logger.error("Failed to send super notification to %s: %s", super_email, e)
        return False, None


def notify_vendor(
    vendor: dict,
    subject: str,
    body: str,
    gmail_service=None,
) -> bool:
    """
    Sends a job dispatch email to the assigned vendor.
    When DRY_RUN=true, prints the email to terminal.

    Returns:
        True if the email was sent (or dry-run printed) successfully, False otherwise.
    """
    vendor_email = vendor.get("email", "").strip()
    if not vendor_email:
        logger.warning(
            "No email on vendor %r — skipping vendor notification.",
            vendor.get("vendor_name"),
        )
        return False

    if DRY_RUN:
        _dry_run_log(vendor_email, subject, body)
        return True

    try:
        send_email(to_address=vendor_email, subject=subject, body=body)
        logger.info("Vendor outreach sent to %s (%s).", vendor.get("vendor_name"), vendor_email)
        return True
    except Exception as e:
        logger.error("Failed to send vendor outreach to %s: %s", vendor_email, e)
        return False


def notify_tenant(
    tenant_email: str,
    subject: str,
    body: str,
    thread_id: str = None,
    message_id: str = None,
) -> bool:
    """
    Sends a status update email back to the tenant (used in Cycle 2).
    When DRY_RUN=true, prints the email to terminal.

    Returns:
        True if the email was sent (or dry-run printed) successfully, False otherwise.
    """
    if not tenant_email:
        logger.warning("notify_tenant called with no tenant_email — skipping.")
        return False

    if DRY_RUN:
        _dry_run_log(tenant_email, subject, body)
        return True

    try:
        send_email(
            to_address=tenant_email,
            subject=subject,
            body=body,
            thread_id=thread_id,
            message_id=message_id,
        )
        logger.info("Tenant notification sent to %s.", tenant_email)
        return True
    except Exception as e:
        logger.error("Failed to send tenant notification to %s: %s", tenant_email, e)
        return False
