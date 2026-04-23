"""
agents/super_gate.py — Super approval gate for PropFlow's two-cycle workflow.

Handles the outbound inquiry to the building super, parsing their reply,
escalation timing, and escalation email generation.
"""

import logging
from datetime import datetime, timezone

import anthropic

from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL, PROPFLOW_OWNER_EMAIL

logger = logging.getLogger(__name__)

_claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Email header prefixes Claude sometimes prepends to generated bodies.
# Any line containing one of these strings (case-insensitive) is stripped.
HEADER_PATTERNS = ["subject:", "to:", "from:", "cc:", "bcc:", "date:", "re:"]


def _strip_headers(text: str) -> str:
    """Removes lines that contain any email header pattern from Claude output."""
    filtered = [
        line for line in text.splitlines()
        if not any(pattern in line.lower() for pattern in HEADER_PATTERNS)
    ]
    return "\n".join(filtered).strip()


def build_super_inquiry(parsed_email: dict, building: dict, vendor: dict | None) -> str:
    """
    Returns a brief email body asking the super whether they can handle the
    issue in-house or want PropFlow to dispatch the identified vendor.

    Includes issue details, the vendor PropFlow has lined up, and clear
    reply instructions (APPROVE / HANDLE).

    Args:
        parsed_email: Structured dict from parse_email().
        building: Building dict from lookup_building().
        vendor: Vendor dict from lookup_vendor(), or None if no match found.

    Returns:
        Plain-text email body string.
    """
    issue_type  = parsed_email.get("issue_type") or "maintenance issue"
    description = parsed_email.get("issue_description") or ""
    unit        = parsed_email.get("unit_number") or "unknown unit"
    urgency     = parsed_email.get("urgency") or "Low"
    address     = building.get("full_address") or "the building"
    vendor_name = vendor.get("vendor_name") if vendor else None

    vendor_line = (
        f"I have {vendor_name} lined up and ready to go."
        if vendor_name
        else "I don't have a vendor lined up yet — let me know and I'll source one."
    )

    approve_line = (
        f"Reply **APPROVE** to dispatch {vendor_name}."
        if vendor_name
        else "Reply **APPROVE** if you'd like me to find and dispatch a vendor."
    )

    prompt = (
        f"Write a short, direct email to the building super at {address}. "
        f"A tenant in Unit {unit} has a {urgency.lower()}-urgency {issue_type} issue: "
        f"{description or '(no further detail)'}. "
        f"Vendor on standby: {vendor_name or 'none identified yet'}. "
        f"Ask clearly: can they handle it in-house, or should PropFlow dispatch the vendor? "
        f"Include exactly these reply instructions on their own lines:\n"
        f"  Reply APPROVE to dispatch {vendor_name or 'a vendor'}.\n"
        f"  Reply HANDLE if you can manage it in-house.\n"
        f"Tone: brief, human, like a quick text from a colleague. "
        f"No formal greeting. Sign off with just 'J' on its own line."
    )

    try:
        response = _claude.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=250,
            messages=[{"role": "user", "content": prompt}],
        )
        return _strip_headers(response.content[0].text)
    except anthropic.APIError as e:
        logger.error("Anthropic API error in build_super_inquiry: %s", e)
        # Fallback template
        return (
            f"Unit {unit} at {address} has a {urgency.lower()}-urgency "
            f"{issue_type} issue: {description or 'see details on file'}.\n\n"
            f"{vendor_line}\n\n"
            f"{approve_line}\n"
            f"Reply **HANDLE** if you can manage it in-house.\n\n"
            "J"
        )


def parse_super_reply(email_body: str) -> str:
    """
    Classifies a super's reply to a PropFlow approval inquiry.

    Sends the email body to Claude with max_tokens=10 and maps the response
    to one of three outcomes:
      "approved"  — super wants PropFlow to dispatch the vendor
                    (APPROVE, yes, go ahead, send them, etc.)
      "declined"  — super will handle it themselves
                    (HANDLE, handle, I will handle, handling it, I'll handle,
                     we'll handle, will handle, handle it from my end,
                     taking care of it, I will take care of it, we got it,
                     on it, in-house, no need, decline, declined,
                     managing it, we will manage)
      "unknown"   — ambiguous or unclassifiable reply

    Returns "unknown" on any API failure (caller should escalate or re-ask).

    Args:
        email_body: Plain-text body of the super's reply email.

    Returns:
        One of: "approved", "declined", "unknown".
    """
    prompt = (
        "Classify this building superintendent's reply to a maintenance dispatch request.\n"
        "Return ONLY one word — no punctuation, no explanation.\n\n"
        "Key rule:\n"
        "  If the super indicates THEY will personally handle the issue = declined.\n"
        "  If the super indicates an external vendor should be sent = approved.\n\n"
        "  approved  — super wants the vendor dispatched\n"
        "    (APPROVE / yes / go ahead / send them / please send / dispatch / send the vendor)\n"
        "  declined  — super will handle it themselves\n"
        "    (HANDLE / handle / I will handle / handling it / I'll handle / we'll handle /\n"
        "     will handle / handle it from my end / taking care of it / I will take care of it /\n"
        "     we got it / on it / in-house / no need / decline / declined /\n"
        "     managing it / we will manage)\n"
        "  unknown   — ambiguous, unclear, or unrelated\n\n"
        f"Reply:\n{email_body}"
    )

    try:
        response = _claude.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=10,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        logger.info("parse_super_reply: raw Claude response=%r", raw)
        raw_lower = raw.lower()
        if "approved" in raw_lower:
            return "approved"
        if "declined" in raw_lower:
            return "declined"
        return "unknown"
    except anthropic.APIError as e:
        logger.error("Anthropic API error in parse_super_reply: %s", e)
        return "unknown"


def is_escalation_due(pending_row: dict) -> bool:
    """
    Returns True if the escalation window for a pending request has passed.

    Compares the current UTC time against the `escalate_at` field in the
    pending row dict. The field is expected to be a UTC timestamp string in
    the format "YYYY-MM-DD HH:MM:SS" (e.g. "2026-04-17 14:00:00"), as written
    by run_cycle1(). ISO 8601 strings with T separator and timezone offsets
    are also accepted.

    Returns False if `escalate_at` is missing, empty, or unparseable —
    so the caller never crashes on bad data.

    Args:
        pending_row: Dict representing a row from the Pending Sheets tab,
                     expected to contain an `escalate_at` key.

    Returns:
        True if now > escalate_at, False otherwise.
    """
    raw = (pending_row.get("escalate_at") or "").strip()
    if not raw:
        return False

    _FORMATS = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
    ]

    dt = None
    for fmt in _FORMATS:
        try:
            dt = datetime.strptime(raw, fmt)
            break
        except ValueError:
            continue

    if dt is None:
        # Last-resort: fromisoformat (handles offset-aware strings)
        try:
            dt = datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            logger.warning("is_escalation_due: could not parse escalate_at=%r", raw)
            return False

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) > dt


def build_inhouse_confirmation(
    parsed_email: dict,
    building: dict,
    outcome: str,
    vendor: dict | None = None,
) -> str:
    """
    Returns a brief tenant notification body for Cycle 2 resolution.

    outcome="inhouse"  — super is handling it themselves.
      "Hey [tenant_name] — [super_first_name] is going to take care of the
       [issue_type] personally. They'll be in touch to schedule a time. J"

    outcome="vendor"   — vendor has been dispatched.
      "Hey [tenant_name] — we've got [vendor_name] coming out for the
       [issue_type]. They'll be in touch to confirm a time. J"

    Falls back to "there" if tenant_name is missing.

    Args:
        parsed_email: Dict with at least tenant_name and issue_type.
        building: Building dict with super_name.
        outcome: "inhouse" or "vendor".
        vendor: Vendor dict (required when outcome="vendor").

    Returns:
        Plain-text email body string (header-stripped, ≤4 lines).
    """
    tenant_name  = (parsed_email.get("tenant_name") or "").strip() or "there"
    issue_type   = (parsed_email.get("issue_type") or "the issue").lower()
    super_name   = (building.get("super_name") or "").strip()
    super_first  = super_name.split()[0] if super_name else "your super"

    if outcome == "inhouse":
        prompt = (
            f"Write a casual 3-4 line message to a tenant named {tenant_name}. "
            f"Let them know {super_first} is going to handle the {issue_type} personally "
            f"and will reach out to schedule a time. "
            f"Start with 'Hey {tenant_name} —'. Sign off on its own line as just 'J'. "
            f"Use 'they'll' not he/she. Warm, brief, no formalities. Under 4 lines total."
        )
    else:
        vendor_name = (vendor.get("vendor_name") if vendor else None) or "a vendor"
        prompt = (
            f"Write a casual 3-4 line message to a tenant named {tenant_name}. "
            f"Let them know {vendor_name} is coming out to take care of the {issue_type} "
            f"and will be in touch to confirm a time. "
            f"Start with 'Hey {tenant_name} —'. Sign off on its own line as just 'J'. "
            f"Warm, brief, no formalities. Under 4 lines total."
        )

    try:
        response = _claude.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
        )
        return _strip_headers(response.content[0].text)
    except anthropic.APIError as e:
        logger.error("Anthropic API error in build_inhouse_confirmation: %s", e)
        if outcome == "inhouse":
            return (
                f"Hey {tenant_name} — {super_first} is going to handle the "
                f"{issue_type} personally and will be in touch to schedule a time.\n\nJ"
            )
        else:
            vendor_name = (vendor.get("vendor_name") if vendor else None) or "a vendor"
            return (
                f"Hey {tenant_name} — we've got {vendor_name} coming out for the "
                f"{issue_type}. They'll be in touch to confirm a time.\n\nJ"
            )


def build_tenant_holding_message(parsed_email: dict) -> str:
    """
    Returns a brief holding message to send the tenant when PropFlow escalates
    to the owner because the super hasn't responded.

    Tone: "We're on it, someone will be out shortly."

    Args:
        parsed_email: Dict with at least tenant_name and issue_type.

    Returns:
        Plain-text email body string.
    """
    tenant_name = (parsed_email.get("tenant_name") or "").strip() or "there"
    issue_type  = (parsed_email.get("issue_type") or "your request").lower()

    prompt = (
        f"Write a very brief holding message to a tenant named {tenant_name}. "
        f"Let them know we're following up on their {issue_type} request, "
        f"coordinating on our end, and will have someone out to them shortly. "
        f"Start with 'Hey {tenant_name} —'. Sign off on its own line as just 'J'. "
        f"2-3 lines max. Calm and reassuring, no formalities."
    )

    try:
        response = _claude.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        return _strip_headers(response.content[0].text)
    except anthropic.APIError as e:
        logger.error("Anthropic API error in build_tenant_holding_message: %s", e)
        return (
            f"Hey {tenant_name} — just following up on your {issue_type} request. "
            f"We're coordinating on our end and will have someone out to you shortly.\n\nJ"
        )


def build_escalation_email(pending_row: dict) -> str:
    """
    Returns a brief email body to PROPFLOW_OWNER_EMAIL notifying them that
    the building super has not responded within the escalation window.

    Includes building, issue type, urgency, and elapsed time since the
    request was created. Tone is factual and brief — not alarming.

    Args:
        pending_row: Dict representing a row from the Pending Sheets tab.
                     Expected keys: building_address, issue_type, urgency,
                     description, timestamp (ISO string of request creation).

    Returns:
        Plain-text email body string.
    """
    address     = pending_row.get("building_address") or "Unknown building"
    issue_type  = pending_row.get("issue_type") or "Unknown issue"
    urgency     = pending_row.get("urgency") or "Unknown"
    description = pending_row.get("description") or ""
    created_raw = (pending_row.get("timestamp") or "").strip()

    # Calculate elapsed time since request was created
    elapsed_str = "unknown duration"
    if created_raw:
        try:
            created_dt = datetime.fromisoformat(created_raw)
            if created_dt.tzinfo is None:
                created_dt = created_dt.replace(tzinfo=timezone.utc)
            delta = datetime.now(timezone.utc) - created_dt
            hours = int(delta.total_seconds() // 3600)
            minutes = int((delta.total_seconds() % 3600) // 60)
            if hours > 0:
                elapsed_str = f"{hours}h {minutes}m"
            else:
                elapsed_str = f"{minutes}m"
        except (ValueError, TypeError):
            pass

    owner = PROPFLOW_OWNER_EMAIL or "the PropFlow owner"
    desc_clean = description.rstrip(".")

    body = (
        f"Hi,\n\n"
        f"The super at {address} hasn't responded to the approval request for a "
        f"{urgency.lower()}-urgency {issue_type} issue"
        f"{f': {desc_clean}' if desc_clean else ''}.\n\n"
        f"Time since request: {elapsed_str}.\n\n"
        f"You may want to follow up directly or dispatch a vendor manually.\n\n"
        f"PropFlow"
    )
    return _strip_headers(body)
