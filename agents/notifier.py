"""
agents/notifier.py — Sends Phase 2 notification emails to building supers
and assigned vendors via the Gmail API.

When DRY_RUN=true, emails are printed to the terminal instead of sent.
"""

import logging
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
) -> bool:
    """
    Sends a notification email to the building super.
    When DRY_RUN=true, prints the email to terminal and returns True.

    Returns:
        True if the email was sent (or dry-run printed) successfully, False otherwise.
    """
    super_email = building.get("super_email", "").strip()
    if not super_email:
        logger.warning(
            "No super_email on building %r — skipping super notification.",
            building.get("full_address"),
        )
        return False

    if DRY_RUN:
        _dry_run_log(super_email, subject, body)
        return True

    try:
        send_email(to_address=super_email, subject=subject, body=body)
        logger.info("Super notification sent to %s.", super_email)
        return True
    except Exception as e:
        logger.error("Failed to send super notification to %s: %s", super_email, e)
        return False


def notify_vendor(
    vendor: dict,
    subject: str,
    body: str,
    gmail_service=None,
) -> bool:
    """
    Sends a job dispatch email to the assigned vendor.
    When DRY_RUN=true, prints the email to terminal and returns True.

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
