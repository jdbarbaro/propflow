"""
config.py — Loads and exposes all environment variables for PropFlow.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# Gmail API
GMAIL_USER_EMAIL = os.getenv("GMAIL_USER_EMAIL")  # Mailbox to monitor

# Anthropic API
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

# Google Sheets API
GOOGLE_SHEETS_CREDENTIALS_FILE = os.getenv(
    "GOOGLE_SHEETS_CREDENTIALS_FILE", "credentials.json"
)
GOOGLE_SPREADSHEET_ID = os.getenv("GOOGLE_SPREADSHEET_ID")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Tenant Requests")
GOOGLE_BUILDINGS_SHEET = os.getenv("GOOGLE_BUILDINGS_SHEET", "Buildings")
GOOGLE_VENDORS_SHEET = os.getenv("GOOGLE_VENDORS_SHEET", "Vendors")
GOOGLE_PENDING_SHEET = os.getenv("GOOGLE_PENDING_SHEET", "Pending Requests")

# Google Drive folder path containing management agreement PDFs
PROPFLOW_DRIVE_FOLDER = os.getenv("PROPFLOW_DRIVE_FOLDER", "PropFlow/Management Agreements")

# PropFlow owner/operator email — used for escalation notifications
PROPFLOW_OWNER_EMAIL = os.getenv("PROPFLOW_OWNER_EMAIL", "")

# Google Calendar — PROPFLOW_CALENDAR_ID is the Railway env var name;
# GOOGLE_CALENDAR_ID is the fallback for local .env compatibility.
PROPFLOW_CALENDAR_ID = (
    os.getenv("PROPFLOW_CALENDAR_ID")
    or os.getenv("GOOGLE_CALENDAR_ID", "")
)

# Dry-run mode — print outbound emails to terminal instead of sending
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
