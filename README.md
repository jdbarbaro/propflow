# PropFlow — Phase 1

AI-powered tenant email processing for commercial property management.

Reads unread emails from an Outlook inbox, extracts structured request data
using Claude, logs it to Google Sheets, and sends an acknowledgment reply.

---

## Project Structure

```
propflow/
├── main.py               # Entry point — orchestrates the pipeline
├── config.py             # Loads all env vars from .env
├── graph_client.py       # Microsoft Graph API (read/send Outlook email)
├── sheets_client.py      # Google Sheets API (log parsed requests)
├── agents/
│   └── email_parser.py   # Anthropic API — parses emails, drafts replies
├── requirements.txt
├── .env.template         # Copy to .env and fill in your keys
└── credentials.json      # Google service account key (not committed)
```

---

## Setup

### 1. Clone and install dependencies

```bash
git clone <repo-url>
cd propflow
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment variables

```bash
cp .env.template .env
```

Open `.env` and fill in every value. See comments in the file for where to
find each credential.

### 3. Set up Microsoft Graph API

1. In the [Azure Portal](https://portal.azure.com), go to **App registrations** → **New registration**.
2. Add a **Client secret** under *Certificates & secrets* and copy the value to `GRAPH_CLIENT_SECRET`.
3. Under **API permissions**, add the following **Application** (not Delegated) permissions:
   - `Mail.Read`
   - `Mail.Send`
4. Click **Grant admin consent**.
5. Copy the **Tenant ID** and **Client ID** to your `.env`.

### 4. Set up Google Sheets API

1. In [Google Cloud Console](https://console.cloud.google.com), create a project and enable the **Google Sheets API**.
2. Create a **Service Account** and download the JSON key. Save it as `credentials.json` in the project root.
3. Create a Google Sheet with a tab named **Tenant Requests** (or whatever you set in `GOOGLE_SHEET_NAME`).
4. Share the sheet with the service account email (e.g. `propflow@your-project.iam.gserviceaccount.com`) as **Editor**.
5. Copy the spreadsheet ID from the URL into `GOOGLE_SPREADSHEET_ID`.

### 5. Set up Anthropic API

1. Get an API key from [console.anthropic.com](https://console.anthropic.com).
2. Add it to `ANTHROPIC_API_KEY` in your `.env`.

### 6. Run

```bash
python main.py
```

For continuous processing, wrap `main.py` in a cron job or a polling loop
with `time.sleep()`.

---

## Google Sheet Columns

| Timestamp | Tenant Name | Unit | Request Type | Urgency | Summary | Action Required | Sender Email |
|-----------|-------------|------|--------------|---------|---------|-----------------|--------------|

---

## Deploying on Railway

To deploy on Railway, base64-encode `credentials.json` and set it as the `GOOGLE_CREDENTIALS_B64` environment variable — PropFlow will write the file to disk on startup.

```bash
base64 -i credentials.json | tr -d '\n' | pbcopy   # macOS — copies to clipboard
```

Set all other `.env` values as Railway environment variables, and set `DRY_RUN=false`.

---

## Security Notes

- Never commit `.env` or `credentials.json`. Both are in `.gitignore`.
- The service account key grants access to Gmail, Sheets, and Drive — treat it like a password.
