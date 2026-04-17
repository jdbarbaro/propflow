"""
test_parser.py — Smoke test for agents/email_parser.parse_email().
Requires only a valid ANTHROPIC_API_KEY in .env. No Gmail or Sheets needed.
"""

import json
from agents.email_parser import parse_email

TEST_EMAILS = [
    {
        "label": "Emergency — Flooding",
        "sender_email": "sarah.chen@tenant.com",
        "subject": "EMERGENCY: Flooding in my unit",
        "body": (
            "Hi, this is Sarah Chen in unit 2A at 450 Park Avenue. "
            "There is water pouring through my ceiling from the unit above. "
            "The floor is completely flooded. Please send someone immediately, "
            "this is an emergency. My number is 917-555-0192."
        ),
    },
    {
        "label": "Urgent — Broken Lock",
        "sender_email": "m.torres@acmecorp.com",
        "subject": "Broken front door lock - Suite 800",
        "body": (
            "Hello, my name is Marco Torres. I lease Suite 800 at 1221 6th Avenue. "
            "The deadbolt on our front door is completely broken — the key no longer turns. "
            "We cannot secure the office overnight. This needs to be fixed ASAP, "
            "preferably today before 6pm. Thank you."
        ),
    },
    {
        "label": "Routine — Noisy AC",
        "sender_email": "linda.park@gmail.com",
        "subject": "AC unit making strange noise",
        "body": (
            "Hi there, I'm Linda Park, I live in unit 4B at 230 Park Avenue. "
            "My air conditioner has been making a loud rattling sound for the past week. "
            "It still works but the noise is disruptive, especially at night. "
            "Could someone take a look when you get a chance? No rush, just wanted to flag it."
        ),
    },
    {
        "label": "Vague — Broken Sink",
        "sender_email": "tenant_unknown@hotmail.com",
        "subject": "sink",
        "body": "hey the sink is broken again same building",
    },
]


def main():
    for test in TEST_EMAILS:
        print(f"\n{'─' * 60}")
        print(f"  {test['label']}")
        print(f"{'─' * 60}")
        print(f"  From:    {test['sender_email']}")
        print(f"  Subject: {test['subject']}")
        print(f"  Body:    {test['body'][:80]}{'...' if len(test['body']) > 80 else ''}")
        print()

        result = parse_email(
            subject=test["subject"],
            body=test["body"],
            sender_email=test["sender_email"],
        )

        if result:
            print(json.dumps(result, indent=2))
        else:
            print("  [!] parse_email() returned an empty dict — check logs above.")


if __name__ == "__main__":
    main()
