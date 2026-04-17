"""
agents/email_parser.py — Uses the Anthropic API to extract structured data
from raw tenant email content and generate acknowledgment replies.
"""

import json
import logging
import anthropic
from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL

logger = logging.getLogger(__name__)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """\
You are an AI assistant for a commercial property management company in New York City.
Your job is to read tenant emails and extract structured information from them.

You MUST respond with a single valid JSON object — no explanation, no markdown, no code fences.
If a field cannot be determined from the email, use null for strings and "Other" for issue_type.

The JSON object must have exactly these keys:
{
  "tenant_name":        string or null,
  "tenant_email":       string or null,
  "building_address":   string or null,
  "unit_number":        string or null,
  "issue_type":         one of the categories below,
  "issue_description":  string or null,
  "urgency":            "Low" | "Medium" | "High"
}

Issue type categories — pick the single best match:
- Plumbing:        leaks, dripping faucets, clogged drains, burst pipes, water pressure, toilets, sinks
- HVAC:            heating, cooling, air conditioning, thermostat, ventilation, no heat, AC broken
- Electrical:      power outage, outlets not working, flickering lights, tripped breakers, wiring
- Structural:      cracks in walls/ceilings/floors, mold, water stains, broken windows or doors
- Elevator:        elevator down, slow, stuck, noisy, door malfunction
- Pest:            rodents, mice, rats, cockroaches, bed bugs, insects, infestation
- Lease Inquiry:   lease terms, renewal, subletting, move-in/out dates, contract questions
- Payment Issue:   rent payment, invoices, incorrect charges, late fees, billing disputes
- Noise Complaint: noise from neighbors, construction, common areas, or outside after hours
- Security:        broken locks, lost keys, access cards, cameras, unauthorized access, gas smell, fire
- Cleaning:        common area cleanliness, garbage, trash overflow, janitorial issues
- Other:           does not fit any category above, or clearly not a tenant property request

One example per category:
- Plumbing:        "The pipe under my kitchen sink is leaking and water is pooling on the floor."
- HVAC:            "My heat has not been working for two days and it is very cold inside."
- Electrical:      "The outlets in my living room stopped working after a storm last night."
- Structural:      "There is a large crack in my bedroom wall and mold forming near the window."
- Elevator:        "Both elevators in the building have been out of service since this morning."
- Pest:            "I have seen several mice in my kitchen over the past week."
- Lease Inquiry:   "I wanted to ask about renewing my lease — it expires at the end of next month."
- Payment Issue:   "I was charged a late fee but my rent was submitted on time. Please advise."
- Noise Complaint: "The tenants above me are playing loud music past midnight every night."
- Security:        "The deadbolt on my front door is broken and I cannot lock my apartment."
- Cleaning:        "The trash in the lobby hallway has not been collected in several days."
- Other:           "Hi, do you know if there's parking available near the building?"

Urgency guide:
- High: safety hazard, no heat/hot water, flood, fire, gas smell, lock-out, elevator failure, or urgent language ("emergency", "ASAP", "immediately")
- Medium: ongoing disruption (broken appliance, persistent leak, mold, pest), response needed within 48 hours
- Low: general questions, lease inquiries, routine or minor requests with no time pressure
"""


def parse_email(subject: str, body: str, sender_email: str = None) -> dict:
    """
    Calls Claude to extract structured data from a raw tenant email.

    Args:
        subject: Email subject line.
        body: Plain-text email body.
        sender_email: Sender address from the email header (used as fallback
                      if Claude cannot extract it from the body).

    Returns:
        A dict with keys: tenant_name, tenant_email, building_address,
        unit_number, issue_type, issue_description, urgency.
        All values are strings or None. Returns an empty dict on failure.
    """
    user_message = f"Subject: {subject}\n\n{body}"

    try:
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=512,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        raw = response.content[0].text.strip()
        # Strip markdown code fences if Claude wraps the JSON in ```json ... ```
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("Claude returned non-JSON output: %s", raw)
        return {}
    except anthropic.APIError as e:
        logger.error("Anthropic API error: %s", e)
        return {}

    # Normalise keys — ensure all expected keys are present
    expected_keys = [
        "tenant_name", "tenant_email", "building_address",
        "unit_number", "issue_type", "issue_description", "urgency",
    ]
    result = {k: parsed.get(k) for k in expected_keys}

    # Fall back to header address if Claude couldn't extract one from the body
    if not result["tenant_email"] and sender_email:
        result["tenant_email"] = sender_email

    return result


def parse_super_reply(subject: str, body: str) -> str:
    """
    Classifies a building super's reply to a PropFlow approval request.

    Returns:
        "can_handle" — super says they can fix it in-house, no outside vendor needed.
        "needs_vendor" — super says they need a contractor, or the reply is unclear.
                         Defaults to "needs_vendor" on any API failure (safe default).
    """
    prompt = (
        "You are reading a building superintendent's reply to a maintenance request.\n"
        "Classify their response as exactly one of these two labels:\n\n"
        "  can_handle   — the super says they can fix it themselves in-house, "
        "no outside contractor needed\n"
        "  needs_vendor — the super says they need an outside contractor, "
        "cannot handle it, or the reply is unclear or non-committal\n\n"
        "Reply with ONLY the label — no punctuation, no explanation.\n\n"
        f"Subject: {subject}\n\n{body}"
    )
    try:
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=10,
            messages=[{"role": "user", "content": prompt}],
        )
        result = response.content[0].text.strip().lower()
        return "can_handle" if "can_handle" in result else "needs_vendor"
    except anthropic.APIError as e:
        logger.error("Anthropic API error parsing super reply: %s", e)
        return "needs_vendor"  # safe default — dispatch vendor if uncertain


def build_acknowledgment(parsed: dict) -> str:
    """
    Generates a short, professional acknowledgment email body to send back
    to the tenant based on the structured data from parse_email().

    Args:
        parsed: The dict returned by parse_email().

    Returns:
        A plain-text string containing the acknowledgment email body.
        Falls back to a generic message if the API call fails.
    """
    description = parsed.get("issue_description") or ""
    urgency = (parsed.get("urgency") or "Low").lower()
    is_emergency = urgency == "high"

    prompt = (
        f"Write a very short reply to a tenant about a {'urgent ' if is_emergency else ''}"
        f"{parsed.get('issue_type') or 'maintenance'} issue"
        f"{f': {description}' if description else ''}.\n\n"
        "Rules:\n"
        "- 3 to 5 lines maximum, no exceptions\n"
        "- Sound like a busy human typing quickly on their phone\n"
        "- Start with something like 'Got it', 'On it', or 'Thanks for the heads up'\n"
        "- Reference the specific issue naturally, e.g. 'we'll get someone to look at the heating' "
        "not 'your request has been received'\n"
        + ("- Add one line showing you're on it, e.g. 'I'm on it' or 'getting someone out today' "
           "— keep it realistic, no overpromising on exact timing\n"
           if is_emergency else "")
        + "- No formal greetings like 'Dear' or 'Hello'\n"
        "- No sign-off phrases like 'Best regards' or 'Sincerely'\n"
        "- NEVER mention 'automated', 'system', 'agent', 'ticket', or 'request has been logged'\n"
        "- No bullet points or formal structure\n"
        "- Sign off with just 'J' on its own line\n"
        "- Important: double-check spacing between all words before returning. "
        "Never run two words together without a space.\n\n"
        "Example for urgent issue:\n"
        "Got it — thanks for the heads up. We'll get someone over to look at the heating today. "
        "I'm on it.\nJ\n\n"
        "Example for routine issue:\n"
        "Thanks for letting us know. We'll get someone in to take a look this week and will "
        "follow up with timing.\nJ"
    )

    try:
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except anthropic.APIError as e:
        logger.error("Anthropic API error generating ACK: %s", e)
        if is_emergency:
            return "On it — getting someone out to take a look today.\nJ"
        return "Thanks for letting us know. We'll get someone in to take a look and follow up with timing.\nJ"
