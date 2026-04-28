"""
calendar_client.py — Google Calendar integration for PropFlow.

Creates and manages repair appointment placeholder events when vendors
are dispatched. Uses the same service account as gmail_client / sheets_client,
with domain-wide delegation scoped to the Calendar API.

Scope required in Google Workspace Admin:
    https://www.googleapis.com/auth/calendar
"""

import base64
import logging
import os
from datetime import datetime, timedelta, timezone

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import (
    GOOGLE_SHEETS_CREDENTIALS_FILE,
    GMAIL_USER_EMAIL,
    PROPFLOW_CALENDAR_ID,
)

logger = logging.getLogger(__name__)

_CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"

# Google Calendar colorId mapping by urgency
_URGENCY_COLOR = {
    "high":   "11",  # Tomato (red)
    "medium": "5",   # Banana (yellow)
    "low":    "10",  # Basil  (green)
}

# The attendee who receives all calendar invites
_OWNER_ATTENDEE = "jbone@propflowz.com"


def get_calendar_service():
    """
    Returns an authenticated Google Calendar API service object.

    Uses the service account credentials with domain-wide delegation,
    delegated to GMAIL_USER_EMAIL. Bootstraps credentials.json from
    GOOGLE_CREDENTIALS_B64 if running on Railway and the file is absent.
    """
    # Bootstrap credentials if running on Railway and file doesn't exist yet
    _b64 = os.environ.get("GOOGLE_CREDENTIALS_B64")
    if _b64 and not os.path.exists(GOOGLE_SHEETS_CREDENTIALS_FILE):
        with open(GOOGLE_SHEETS_CREDENTIALS_FILE, "wb") as _f:
            _f.write(base64.b64decode(_b64))
        logger.info("calendar_client: credentials.json written from GOOGLE_CREDENTIALS_B64.")

    creds = Credentials.from_service_account_file(
        GOOGLE_SHEETS_CREDENTIALS_FILE, scopes=[_CALENDAR_SCOPE]
    )
    delegated = creds.with_subject(GMAIL_USER_EMAIL)
    return build("calendar", "v3", credentials=delegated)


def create_placeholder_event(
    parsed_email: dict,
    building: dict,
    vendor: dict | None,
) -> str | None:
    """
    Creates a placeholder Calendar event when a vendor is dispatched.

    The event uses tomorrow 9am–11am as a placeholder time (real time TBD
    when the vendor confirms). Color reflects urgency. The owner attendee
    is always added.

    Args:
        parsed_email: Parsed request dict (issue_type, issue_description,
                      unit_number, urgency, tenant_name, tenant_email).
        building:     Building dict from lookup_building().
        vendor:       Vendor dict from lookup_vendor(), or None.

    Returns:
        The created event's ID string, or None on failure.
    """
    if not PROPFLOW_CALENDAR_ID:
        logger.warning("create_placeholder_event: PROPFLOW_CALENDAR_ID not set — skipping.")
        return None

    issue_type   = (parsed_email.get("issue_type") or "Request").strip()
    building_addr = (
        building.get("full_address")
        or building.get("client_name")
        or parsed_email.get("building_address")
        or "Unknown Building"
    )
    urgency = (parsed_email.get("urgency") or "medium").strip().lower()

    title = f"{issue_type} — {building_addr}"

    tenant_name  = parsed_email.get("tenant_name") or ""
    tenant_email = parsed_email.get("tenant_email") or ""
    tenant_str   = f"{tenant_name} ({tenant_email})" if tenant_name else tenant_email

    vendor_name  = vendor.get("vendor_name", "TBD") if vendor else "TBD"
    vendor_email = vendor.get("email", "") if vendor else ""
    vendor_str   = f"{vendor_name} ({vendor_email})" if vendor_email else vendor_name

    description = (
        f"Tenant: {tenant_str}\n"
        f"Unit: {parsed_email.get('unit_number') or ''}\n"
        f"Issue: {parsed_email.get('issue_description') or ''}\n"
        f"Vendor: {vendor_str}\n"
        f"Urgency: {urgency.title()}\n"
        f"Status: Awaiting confirmation"
    )

    # Placeholder: tomorrow 9am–11am in UTC
    tomorrow = datetime.now(timezone.utc).replace(
        hour=9, minute=0, second=0, microsecond=0
    ) + timedelta(days=1)
    start_dt = tomorrow.isoformat()
    end_dt   = (tomorrow + timedelta(hours=2)).isoformat()

    color_id = _URGENCY_COLOR.get(urgency, "5")

    event_body = {
        "summary":     title,
        "description": description,
        "colorId":     color_id,
        "start":       {"dateTime": start_dt},
        "end":         {"dateTime": end_dt},
        "attendees":   [{"email": _OWNER_ATTENDEE}],
        "status":      "tentative",
    }

    try:
        service = get_calendar_service()
        event = service.events().insert(
            calendarId=PROPFLOW_CALENDAR_ID,
            body=event_body,
            sendUpdates="none",
        ).execute()
        event_id = event.get("id")
        logger.info(
            "create_placeholder_event: created event %r (%s)", event_id, title
        )
        return event_id
    except HttpError as e:
        logger.error("create_placeholder_event: Google API error: %s", e)
        return None
    except Exception as e:
        logger.error("create_placeholder_event: unexpected error: %s", e)
        return None


def update_event_time(
    event_id: str,
    start_datetime: datetime | str,
    end_datetime: datetime | str,
) -> dict | None:
    """
    Updates an existing calendar event with a confirmed appointment time.

    Args:
        event_id:       Google Calendar event ID.
        start_datetime: Confirmed start (datetime object or ISO string).
        end_datetime:   Confirmed end   (datetime object or ISO string).

    Returns:
        The updated event dict from the API, or None on failure.
    """
    if not PROPFLOW_CALENDAR_ID:
        logger.warning("update_event_time: PROPFLOW_CALENDAR_ID not set — skipping.")
        return None

    def _to_iso(dt):
        if isinstance(dt, datetime):
            return dt.astimezone(timezone.utc).isoformat()
        return str(dt)

    try:
        service = get_calendar_service()

        # Fetch existing event first to avoid overwriting other fields
        existing = service.events().get(
            calendarId=PROPFLOW_CALENDAR_ID, eventId=event_id
        ).execute()

        existing["start"] = {"dateTime": _to_iso(start_datetime)}
        existing["end"]   = {"dateTime": _to_iso(end_datetime)}
        existing["status"] = "confirmed"

        updated = service.events().update(
            calendarId=PROPFLOW_CALENDAR_ID,
            eventId=event_id,
            body=existing,
            sendUpdates="none",
        ).execute()

        logger.info("update_event_time: event %r updated.", event_id)
        return updated
    except HttpError as e:
        logger.error("update_event_time: Google API error for event %r: %s", event_id, e)
        return None
    except Exception as e:
        logger.error("update_event_time: unexpected error: %s", e)
        return None


def get_upcoming_events(days: int = 7) -> list[dict]:
    """
    Returns all events from PROPFLOW_CALENDAR_ID for the next [days] days,
    sorted by start time ascending.

    Returns:
        List of event dicts with keys:
        event_id, title, start, end, description, color_id, urgency, status.
        Empty list on error or if PROPFLOW_CALENDAR_ID is not set.
    """
    if not PROPFLOW_CALENDAR_ID:
        logger.warning("get_upcoming_events: PROPFLOW_CALENDAR_ID not set.")
        return []

    now     = datetime.now(timezone.utc)
    time_min = now.isoformat()
    time_max = (now + timedelta(days=days)).isoformat()

    # Reverse-map colorId → urgency for the dashboard
    _color_urgency = {v: k for k, v in _URGENCY_COLOR.items()}

    try:
        service = get_calendar_service()
        result  = service.events().list(
            calendarId=PROPFLOW_CALENDAR_ID,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
        ).execute()

        events = []
        for item in result.get("items", []):
            start_raw = item.get("start", {}).get("dateTime") or item.get("start", {}).get("date", "")
            end_raw   = item.get("end",   {}).get("dateTime") or item.get("end",   {}).get("date", "")
            color_id  = item.get("colorId", "5")
            urgency   = _color_urgency.get(color_id, "medium").title()

            events.append({
                "event_id":    item.get("id", ""),
                "title":       item.get("summary", ""),
                "start":       start_raw,
                "end":         end_raw,
                "description": item.get("description", ""),
                "color_id":    color_id,
                "urgency":     urgency,
                "status":      item.get("status", ""),
            })

        logger.info("get_upcoming_events: fetched %d event(s).", len(events))
        return events
    except HttpError as e:
        logger.error("get_upcoming_events: Google API error: %s", e)
        return []
    except Exception as e:
        logger.error("get_upcoming_events: unexpected error: %s", e)
        return []


def cancel_event(event_id: str) -> bool:
    """
    Cancels (deletes) a Calendar event.

    Used when a request is cancelled or handled entirely in-house.

    Returns:
        True on success, False on failure.
    """
    if not PROPFLOW_CALENDAR_ID:
        logger.warning("cancel_event: PROPFLOW_CALENDAR_ID not set — skipping.")
        return False

    try:
        service = get_calendar_service()
        service.events().delete(
            calendarId=PROPFLOW_CALENDAR_ID,
            eventId=event_id,
            sendUpdates="none",
        ).execute()
        logger.info("cancel_event: event %r deleted.", event_id)
        return True
    except HttpError as e:
        if e.resp.status == 410:
            # Already deleted
            logger.warning("cancel_event: event %r already deleted (410).", event_id)
            return True
        logger.error("cancel_event: Google API error for event %r: %s", event_id, e)
        return False
    except Exception as e:
        logger.error("cancel_event: unexpected error: %s", e)
        return False
