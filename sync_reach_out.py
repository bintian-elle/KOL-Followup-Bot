"""Import unanswered Bluevua first outreach into Reach_Out_Track.

Config notes that Upfluence has no Gmail marker or API, so subject and first
message in the conversation are the available proxy. This script never sends
Slack messages or modifies Gmail.
"""

import json
import os
import re
import time
import warnings
from datetime import datetime
from email.utils import getaddresses, parseaddr
from pathlib import Path
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sheet_state import SheetState



ROOT = Path(__file__).resolve().parent
TAB = "Reach_Out_Track"
HEADER = ["Gmail Thread ID", "Reach Out Sent At", "Campaign / Context", "Gmail Thread Link", "KOL Name", "Email", "Status", "Gmail Message ID"]


def route_complete(event):
    return bool(event.get("labeled"))


def load_env():
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"'))


def call(request):
    for attempt in range(10):
        try:
            return request.execute()
        except HttpError as exc:
            if exc.resp.status not in (403, 429, 500, 502, 503) or attempt == 9:
                raise
            time.sleep(min(2 ** attempt, 60))


def list_messages(gmail, query):
    results = []
    token = None
    while True:
        page = call(gmail.messages().list(userId="me", q=query, maxResults=500, pageToken=token))
        results.extend(page.get("messages", []))
        token = page.get("nextPageToken")
        if not token:
            return results


def creator_name_from_subject(subject):
    """Use a name only when the Bluevua x creator pattern is unambiguous."""
    match = re.search(
        r"\bBluevua\s*[x×]\s*(.+?)(?=\s*(?::|\||--|\s[-–]\s)|$)",
        subject, re.IGNORECASE,
    )
    if not match:
        return ""
    name = match.group(1).strip()
    # Some Upfluence invitations put the campaign title immediately after the
    # creator handle, without punctuation separating the two.
    name = re.sub(r"\s+(?:Back-to-School|BTS) Campaign$", "", name, flags=re.IGNORECASE)
    return name


def first_human_reply(conversation, internal):
    """Find an external human reply after the original outreach message."""
    for message in conversation[1:]:
        headers = {h["name"].lower(): h["value"] for h in message["payload"].get("headers", [])}
        sender = parseaddr(headers.get("from", ""))[1].lower()
        if (sender and sender not in internal
                and not sender.startswith(("no-reply@", "noreply@", "reminders@", "mailer-daemon@", "postmaster@"))):
            return message
    return None


def main():
    load_env()
    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    sheet_credentials = service_account.Credentials.from_service_account_file(
        str(ROOT / os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    sheets = build("sheets", "v4", credentials=sheet_credentials, cache_discovery=False).spreadsheets()
    state = SheetState(sheets, sheet_id)
    config_rows = call(sheets.values().get(spreadsheetId=sheet_id, range="'Config'!A1:B100")).get("values", [])
    config = {row[0]: row[1] for row in config_rows if len(row) > 1}
    tz = ZoneInfo(config.get("timezone", "America/Los_Angeles"))
    start = datetime.strptime(config["gmail_scan_start_at"].strip(), "%Y-%m-%d").replace(tzinfo=tz)
    mailbox = config["gmail_mailbox"].strip().lower()
    brand = config.get("campaign_name_contains", "Bluevua").strip()
    existing = call(sheets.values().get(spreadsheetId=sheet_id, range=f"'{TAB}'!A1:H10000")).get("values", [])
    if not existing or existing[0] != HEADER:
        raise RuntimeError("Reach_Out_Track columns changed; refusing to write")
    by_thread = {row[0]: row for row in existing[1:] if row and row[0]}
    transitions = state.load("reach_out_replies", {})

    gmail_credentials = Credentials(
        None, refresh_token=os.environ["GMAIL_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GMAIL_CLIENT_ID"], client_secret=os.environ["GMAIL_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/gmail.modify"],
    )
    gmail_credentials.refresh(Request())
    gmail = build("gmail", "v1", credentials=gmail_credentials, cache_discovery=False).users()
    period = f"after:{int(start.timestamp())}"
    candidates = list_messages(gmail, f'in:sent {period} from:{mailbox} subject:{brand} -subject:Re: -subject:Fwd:')
    print(f"Candidate messages: {len(candidates)}", flush=True)

    selected = []
    skipped = 0
    replied = 0
    selected_threads = set()
    for index, item in enumerate(candidates, 1):
        thread = call(gmail.threads().get(
            userId="me", id=item["threadId"], format="metadata",
            metadataHeaders=["From", "To", "Cc", "Subject"],
        ))
        time.sleep(float(os.getenv("GMAIL_THREAD_INTERVAL_SECONDS", "2.5")))
        conversation = sorted(thread["messages"], key=lambda msg: int(msg["internalDate"]))
        message = conversation[0]
        if message["id"] != item["id"]:
            skipped += 1
            continue
        if int(message["internalDate"]) < int(start.timestamp() * 1000):
            skipped += 1
            continue
        headers = {h["name"].lower(): h["value"] for h in message["payload"].get("headers", [])}
        sender = parseaddr(headers.get("from", ""))[1].lower()
        subject = headers.get("subject", "").strip()
        if sender != mailbox or brand.casefold() not in subject.casefold() or subject.casefold().startswith(("re:", "fwd:", "fw:")):
            skipped += 1
            continue
        snippet = message.get("snippet", "").casefold()
        if any(term in (subject + " " + snippet).casefold() for term in ("invoice", "payment request", "attached the contract", "attached the agreement")):
            skipped += 1
            continue
        recipients = [(name, address) for name, address in getaddresses([headers.get("to", ""), headers.get("cc", "")]) if address and address.lower() != mailbox]
        if not recipients:
            skipped += 1
            continue
        if item["threadId"] in selected_threads:
            continue
        selected_threads.add(item["threadId"])
        internal = {mailbox, "partnerships@bluevua.com", "pr@bluevua.com"}
        internal.update(x.strip().lower() for x in config.get("brand_team_emails", "").split(","))
        bounce = False
        for later in conversation[1:]:
            later_headers = {h["name"].lower(): h["value"] for h in later["payload"].get("headers", [])}
            later_sender = parseaddr(later_headers.get("from", ""))[1].lower()
            later_subject = later_headers.get("subject", "").casefold()
            if "mailer-daemon" in later_sender or "postmaster" in later_sender:
                if "failure" in later_subject or "address not found" in later.get("snippet", "").casefold():
                    bounce = True
        if first_human_reply(conversation, internal):
            if item["threadId"] in by_thread:
                event = transitions.get(item["threadId"], {})
                if not route_complete(event):
                    raise RuntimeError("A tracked Reach Out received a reply before Active routing completed; preserving Track rows")
            replied += 1
            continue
        name, email = recipients[0]
        sent_at = datetime.fromtimestamp(int(message["internalDate"]) / 1000, tz).strftime("%Y-%m-%d %H:%M:%S %Z")
        status = "Address not found" if bounce else "Reach out sent"
        thread_id = item["threadId"]
        previous = by_thread.get(thread_id, [])
        name = (previous[4] if len(previous) > 4 else "") or name or creator_name_from_subject(subject)
        row = [thread_id, sent_at, subject,
               f"https://mail.google.com/mail/u/0/#all/{thread_id}",
               name, email, status, item["id"]]
        selected.append((int(message["internalDate"]), row))
        if index % 100 == 0:
            print(f"Checked {index}/{len(candidates)}", flush=True)

    # Keep bounced addresses together at the top for quick cleanup. Within
    # both the bounced and normal groups, preserve newest-first sent order.
    selected.sort(key=lambda pair: (pair[1][6] == "Address not found", pair[0]), reverse=True)
    rows = [row for _, row in selected]
    old_count = len(existing) - 1
    values = rows + [[""] * len(HEADER) for _ in range(max(0, old_count - len(rows)))]
    if values:
        call(sheets.values().update(
            spreadsheetId=sheet_id, range=f"'{TAB}'!A2:H{len(values) + 1}",
            valueInputOption="RAW", body={"values": values},
        ))
    new_count = sum(row[0] not in by_thread for row in rows)
    print(f"Tracked unanswered: {len(rows)}; new: {new_count}; replied excluded: {replied}; skipped: {skipped}; bounced: {sum(row[6] == 'Address not found' for row in rows)}")


if __name__ == "__main__":
    warnings.filterwarnings("ignore", category=FutureWarning)
    main()
