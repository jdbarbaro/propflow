"""
gmail_client.py — Wrapper around the Gmail API for reading and sending
emails from the configured Gmail mailbox.

Authentication uses a service account with domain-wide delegation.
The service account must be granted the gmail.modify scope in
Google Workspace Admin → Security → API Controls → Domain-wide delegation.
"""

import base64
import email as email_lib
import email.mime.text
import logging
import re
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from config import GOOGLE_SHEETS_CREDENTIALS_FILE, GMAIL_USER_EMAIL

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def get_gmail_service():
    """
    Builds and returns an authenticated Gmail API service object, impersonating
    GMAIL_USER_EMAIL via the service account's domain-wide delegation.
    """
    creds = Credentials.from_service_account_file(
        GOOGLE_SHEETS_CREDENTIALS_FILE, scopes=SCOPES
    )
    delegated = creds.with_subject(GMAIL_USER_EMAIL)
    return build("gmail", "v1", credentials=delegated)


def _get_header(headers: list[dict], name: str) -> str:
    """Returns the value of the first header matching `name`, or empty string."""
    for h in headers:
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


def _decode_body(payload: dict) -> str:
    """
    Recursively walks a Gmail message payload to extract plain-text content.
    Gmail encodes body data as base64url.
    """
    mime_type = payload.get("mimeType", "")

    if mime_type == "text/plain":
        data = payload.get("body", {}).get("data", "")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        return ""

    if mime_type.startswith("multipart/"):
        for part in payload.get("parts", []):
            text = _decode_body(part)
            if text:
                return text

    return ""


def _parse_sender(from_header: str) -> tuple[str, str]:
    """
    Splits a 'From' header like 'John Smith <john@example.com>' into
    (display_name, email_address). Falls back gracefully if no angle brackets.
    """
    match = re.match(r"^(.*?)\s*<([^>]+)>$", from_header.strip())
    if match:
        return match.group(1).strip().strip('"'), match.group(2).strip()
    # Plain address with no display name
    return "", from_header.strip()


def fetch_unread_emails() -> list[dict]:
    """
    Retrieves all unread messages from the INBOX of the monitored mailbox.

    Returns:
        A list of dicts, each with:
            id, subject, body, sender_name, sender_email, thread_id
    """
    service = get_gmail_service()

    try:
        result = (
            service.users()
            .messages()
            .list(userId="me", labelIds=["INBOX", "UNREAD"], maxResults=50)
            .execute()
        )
    except HttpError as e:
        logger.error("Failed to list messages: %s", e)
        return []

    message_stubs = result.get("messages", [])
    if not message_stubs:
        return []

    emails = []
    for stub in message_stubs:
        try:
            msg = (
                service.users()
                .messages()
                .get(userId="me", id=stub["id"], format="full")
                .execute()
            )
        except HttpError as e:
            logger.error("Failed to fetch message %s: %s", stub["id"], e)
            continue

        headers = msg.get("payload", {}).get("headers", [])
        subject = _get_header(headers, "Subject")
        from_header = _get_header(headers, "From")
        sender_name, sender_email = _parse_sender(from_header)
        body = _decode_body(msg.get("payload", {}))

        # Skip emails sent by the monitored mailbox itself (e.g. ACK replies
        # looping back as unread messages).
        if sender_email.lower() == GMAIL_USER_EMAIL.lower():
            logger.debug("Skipping self-sent message id=%s", msg["id"])
            mark_email_as_read(msg["id"])
            continue

        message_id = _get_header(headers, "Message-ID")

        emails.append(
            {
                "id": msg["id"],
                "thread_id": msg["threadId"],
                "message_id": message_id,
                "subject": subject,
                "body": body,
                "sender_name": sender_name,
                "sender_email": sender_email,
            }
        )

    return emails


def send_email(
    to_address: str,
    subject: str,
    body: str,
    thread_id: str = None,
    message_id: str = None,
) -> tuple[str | None, str | None]:
    """
    Sends a plain-text email from the monitored Gmail mailbox.

    Args:
        to_address: Recipient email address.
        subject: Email subject line.
        body: Plain-text email body.
        thread_id: If provided, the message is placed in this Gmail thread.
        message_id: If provided, sets In-Reply-To and References headers so
                    the reply threads correctly in the recipient's email client,
                    and prefixes the subject with 'Re: ' if not already present.

    Returns:
        Tuple of (thread_id, message_id) of the sent message, or (None, None) on failure.
        thread_id is used to track reply threads; message_id is used for In-Reply-To threading.
    """
    service = get_gmail_service()

    if message_id and not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    mime_msg = email.mime.text.MIMEText(body, "plain")
    mime_msg["To"] = to_address
    mime_msg["From"] = GMAIL_USER_EMAIL
    mime_msg["Subject"] = subject
    if message_id:
        mime_msg["In-Reply-To"] = message_id
        mime_msg["References"] = message_id

    raw = base64.urlsafe_b64encode(mime_msg.as_bytes()).decode("utf-8")
    message_body = {"raw": raw}
    if thread_id:
        message_body["threadId"] = thread_id

    try:
        result = service.users().messages().send(userId="me", body=message_body).execute()
        return result.get("threadId"), result.get("id")
    except HttpError as e:
        logger.error("Failed to send email to %s: %s", to_address, e)
        raise


def fetch_thread_reply(thread_id: str, expected_sender: str | None = None) -> dict | None:
    """
    Fetches a Gmail thread and returns the first reply not sent by the monitored
    mailbox, optionally restricted to a specific sender address.

    Skips messages from the monitored mailbox itself and from mailer-daemon /
    postmaster addresses (delivery failure notifications).

    Used by check_pending_threads() to detect super replies regardless of
    whether they have been opened/read in Gmail.

    Args:
        thread_id: Gmail thread ID to inspect.
        expected_sender: If provided, only return a message from this address
                         (case-insensitive). Useful to avoid matching bounces
                         or unrelated replies in the same thread.

    Returns:
        An email dict (same structure as fetch_unread_emails()) for the first
        matching reply, or None if no reply has arrived yet.
    """
    service = get_gmail_service()

    try:
        thread = service.users().threads().get(
            userId="me", id=thread_id, format="full"
        ).execute()
    except HttpError as e:
        logger.error("Failed to fetch thread %s: %s", thread_id, e)
        return None

    skip_patterns = ("mailer-daemon@", "postmaster@", "noreply@", "no-reply@")

    for msg in thread.get("messages", []):
        headers  = msg.get("payload", {}).get("headers", [])
        from_hdr = _get_header(headers, "From")
        sender_name, sender_email = _parse_sender(from_hdr)
        sender_lower = sender_email.lower()

        # Skip our own outbound messages
        if sender_lower == GMAIL_USER_EMAIL.lower():
            continue

        # Skip delivery failure / bounce addresses
        if any(sender_lower.startswith(p) for p in skip_patterns):
            logger.debug("Skipping bounce/daemon message from %s in thread %s", sender_email, thread_id)
            continue

        # If a specific sender is expected, enforce it
        if expected_sender and sender_lower != expected_sender.lower():
            logger.debug(
                "Skipping message from %s — expected %s in thread %s",
                sender_email, expected_sender, thread_id,
            )
            continue

        subject    = _get_header(headers, "Subject")
        message_id = _get_header(headers, "Message-ID")
        body       = _decode_body(msg.get("payload", {}))

        logger.info(
            "fetch_thread_reply: found reply in thread %s from %s",
            thread_id, sender_email,
        )
        return {
            "id":           msg["id"],
            "thread_id":    thread_id,
            "message_id":   message_id,
            "subject":      subject,
            "body":         body,
            "sender_name":  sender_name,
            "sender_email": sender_email,
        }

    return None


def mark_email_as_read(message_id: str) -> None:
    """
    Removes the UNREAD label from a message so it is not reprocessed.

    Args:
        message_id: The Gmail message ID string.
    """
    service = get_gmail_service()

    try:
        service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()
    except HttpError as e:
        logger.error("Failed to mark message %s as read: %s", message_id, e)
        raise
