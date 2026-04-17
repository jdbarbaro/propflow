"""
agents/router.py — Phase 2 routing logic for PropFlow.

Handles building and vendor lookups, approval logic, and generates
the notification email bodies for the building super and assigned vendor.
"""

import logging
import anthropic
from sheets_client import get_building, get_vendor, get_vendor_by_name
from agents.contract_reader import get_contract_terms
from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Issue types that always require client approval regardless of threshold
ALWAYS_REQUIRE_APPROVAL = {"Structural", "Elevator"}


def lookup_building(address: str, sheets_service=None) -> dict | None:
    """
    Finds the building record matching the parsed address.

    Args:
        address: The building_address string from parse_email().
        sheets_service: Unused — sheets_client manages its own auth.
                        Kept for interface consistency / testability.

    Returns:
        A building dict with keys: full_address, client_name, borough_city,
        super_email, approval_threshold. Returns None if not found.
    """
    if not address:
        logger.warning("lookup_building called with empty address.")
        return None
    return get_building(address)


def lookup_vendor(
    issue_type: str,
    geography: str,
    urgency: str,
    building_id: str | None = None,
    sheets_service=None,
) -> dict | None:
    """
    Finds the best vendor for the given issue type, geography, and urgency.

    Phase 2B: if building_id is provided, the building's management agreement
    is consulted first. Any preferred vendors named in the contract are tried
    in order before falling back to the priority-ranked Sheets selection.

    For High urgency requests, only vendors with emergency_capable = "Yes"
    are considered. Among qualifying vendors, the one with the lowest
    priority_rank (1 = best) is returned.

    Args:
        issue_type: Parsed issue type e.g. "Plumbing", "HVAC".
        geography: The building's borough_city field.
        urgency: "Low", "Medium", or "High".
        building_id: Optional building ID used to look up the management
                     agreement for preferred vendor names.
        sheets_service: Unused — kept for interface consistency.

    Returns:
        A vendor dict with keys: vendor_name, trade, geography, priority_rank,
        emergency_capable, email, contact_method. Returns None if not found.
    """
    if not issue_type:
        return None

    # ── Phase 2B: try contract preferred vendors first ────────────────────────
    if building_id:
        contract = get_contract_terms(building_id)
        preferred_names = contract.get("preferred_vendors") or []
        for name in preferred_names:
            vendor = get_vendor_by_name(name)
            if vendor:
                logger.info(
                    "Using contract preferred vendor %r for building_id=%r",
                    name, building_id,
                )
                return vendor
        if preferred_names:
            logger.info(
                "Contract preferred vendors %s not found in Sheets — falling back to ranked selection.",
                preferred_names,
            )

    # ── Fall back to priority-ranked Sheets selection ─────────────────────────
    return get_vendor(trade=issue_type, geography=geography, urgency=urgency)


# Estimated job cost proxies used when no real cost estimate is available.
# Keyed as (urgency_lower, issue_type_lower) with urgency checked first.
_COST_PROXIES: dict[tuple[str, str], int] = {
    ("high", "plumbing"):   3000,
    ("high", "hvac"):       3000,
    ("high", "electrical"): 3000,
    ("high", "structural"): 3000,
    ("high", "elevator"):   3000,
    ("high", "pest"):       3000,
    ("high", "security"):   3000,
    ("high", "cleaning"):   3000,
    ("medium", "plumbing"):   1500,
    ("medium", "hvac"):       2000,
    ("medium", "electrical"): 1500,
}
_DEFAULT_ROUTINE_COST = 500   # Low urgency or any trade not listed above
_DEFAULT_URGENT_COST  = 1500  # Medium urgency trade not listed above


def _estimate_cost(issue_type: str, urgency: str) -> int:
    """Returns a rough dollar estimate for a job given issue type and urgency."""
    key = (urgency.strip().lower(), issue_type.strip().lower())
    if key in _COST_PROXIES:
        return _COST_PROXIES[key]
    if urgency.strip().lower() == "low":
        return _DEFAULT_ROUTINE_COST
    return _DEFAULT_URGENT_COST


def check_approval_required(
    issue_type: str,
    urgency: str,
    threshold,
) -> bool:
    """
    Determines whether client approval is required before dispatching a vendor.

    Logic:
      1. Structural and Elevator always require approval regardless of threshold.
      2. Parse threshold as a numeric dollar amount (stripping "$" and commas).
         - If threshold is 0 or missing/unparseable → require approval.
         - If estimated job cost > threshold → require approval.

    Args:
        issue_type: Parsed issue type e.g. "Plumbing", "HVAC".
        urgency: "Low", "Medium", or "High".
        threshold: The approval_threshold value from the building record.
                   Expected to be a number or numeric string (e.g. 5000, "$5,000").

    Returns:
        True if approval is required, False otherwise.
    """
    # Structural and Elevator always require approval
    if issue_type in ALWAYS_REQUIRE_APPROVAL:
        return True

    # Parse threshold — strip currency symbols and commas
    raw = str(threshold or "").strip().replace("$", "").replace(",", "")
    try:
        threshold_value = float(raw)
    except ValueError:
        # Unparseable threshold → require approval to be safe
        logger.warning("Could not parse approval_threshold %r as a number — defaulting to True.", threshold)
        return True

    if threshold_value <= 0:
        return True

    estimated_cost = _estimate_cost(issue_type, urgency)
    return estimated_cost > threshold_value


def build_super_notification(
    parsed_email: dict,
    building: dict,
    vendor: dict | None,
) -> str:
    """
    Generates a professional notification email body for the building super,
    summarising the tenant request and the vendor being dispatched (if any).

    Returns a plain-text email body string. Falls back to a template if the
    API call fails.
    """
    vendor_line = (
        f"Assigned vendor: {vendor['vendor_name']} ({vendor['email']})"
        if vendor
        else "No vendor could be automatically assigned — manual dispatch required."
    )

    prompt = (
        f"Write a brief, professional notification email to the building superintendent "
        f"for {building.get('full_address', 'the building')}. "
        f"A tenant request has been received with the following details:\n\n"
        f"- Tenant: {parsed_email.get('tenant_name') or 'Unknown'} "
        f"({parsed_email.get('tenant_email') or 'no email'})\n"
        f"- Unit: {parsed_email.get('unit_number') or 'Unknown'}\n"
        f"- Issue type: {parsed_email.get('issue_type')}\n"
        f"- Description: {parsed_email.get('issue_description')}\n"
        f"- Urgency: {parsed_email.get('urgency')}\n"
        f"- {vendor_line}\n\n"
        f"Keep the tone direct and professional. 3–5 sentences. "
        f"Do not include a subject line. Sign off as 'PropFlow Property Management'."
    )

    try:
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except anthropic.APIError as e:
        logger.error("Anthropic API error generating super notification: %s", e)
        return (
            f"A new tenant request has been logged for "
            f"{parsed_email.get('unit_number') or 'an unknown unit'} at "
            f"{building.get('full_address', 'your building')}.\n\n"
            f"Issue: {parsed_email.get('issue_type')} — "
            f"{parsed_email.get('issue_description')}\n"
            f"Urgency: {parsed_email.get('urgency')}\n"
            f"{vendor_line}\n\n"
            "Best regards,\nPropFlow Property Management"
        )


def build_vendor_outreach(parsed_email: dict, building: dict) -> str:
    """
    Generates a professional outreach email body for the assigned vendor,
    describing the job, building location, tenant contact, and urgency.

    Returns a plain-text email body string. Falls back to a template if the
    API call fails.
    """
    prompt = (
        f"Write a professional job dispatch email to a vendor/contractor. "
        f"They are being asked to attend to a service request at a commercial property.\n\n"
        f"Job details:\n"
        f"- Building: {building.get('full_address', 'Unknown address')}\n"
        f"- Client: {building.get('client_name', 'Unknown')}\n"
        f"- Issue type: {parsed_email.get('issue_type')}\n"
        f"- Description: {parsed_email.get('issue_description')}\n"
        f"- Unit/location: {parsed_email.get('unit_number') or 'Unknown'}\n"
        f"- Urgency: {parsed_email.get('urgency')}\n"
        f"- Tenant contact: {parsed_email.get('tenant_name') or 'Unknown'} "
        f"({parsed_email.get('tenant_email') or 'no email provided'})\n\n"
        f"Ask the vendor to confirm receipt and provide an estimated arrival time. "
        f"Keep the tone professional and concise — 4–6 sentences. "
        f"Do not include a subject line. Sign off as 'PropFlow Property Management'."
    )

    try:
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except anthropic.APIError as e:
        logger.error("Anthropic API error generating vendor outreach: %s", e)
        return (
            f"You have been assigned a service request at "
            f"{building.get('full_address', 'a managed property')}.\n\n"
            f"Issue: {parsed_email.get('issue_type')} — "
            f"{parsed_email.get('issue_description')}\n"
            f"Unit: {parsed_email.get('unit_number') or 'Unknown'}\n"
            f"Urgency: {parsed_email.get('urgency')}\n"
            f"Tenant contact: {parsed_email.get('tenant_name') or 'Unknown'} "
            f"({parsed_email.get('tenant_email') or 'N/A'})\n\n"
            "Please confirm receipt and provide an ETA.\n\n"
            "Best regards,\nPropFlow Property Management"
        )
