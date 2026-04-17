"""debug_inbox.py — Lists all unread emails currently in the inbox."""
from gmail_client import get_gmail_service

service = get_gmail_service()
result = service.users().messages().list(
    userId="me", labelIds=["INBOX", "UNREAD"], maxResults=20
).execute()

messages = result.get("messages", [])
print(f"{len(messages)} unread message(s) found:\n")

for stub in messages:
    msg = service.users().messages().get(
        userId="me", id=stub["id"], format="metadata",
        metadataHeaders=["From", "Subject", "Date"]
    ).execute()
    headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
    print(f"  id:      {stub['id']}")
    print(f"  from:    {headers.get('From', '?')}")
    print(f"  subject: {headers.get('Subject', '?')}")
    print(f"  date:    {headers.get('Date', '?')}")
    print()
