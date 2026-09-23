"""Sync the Active parent and configured member child labels into Track.

This utility only reads Gmail and writes Google Sheets. It never calls Slack.
"""

import os
import warnings
from datetime import datetime
from email.utils import getaddresses, parseaddr
from pathlib import Path
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from gmail_active_labels import resolve_active_labels


ROOT = Path(__file__).resolve().parent
TRACK = "Bluevua_KOL_Active_Track"


def owner_assignment(old_owner, old_assigned_at, child_owners, now):
    """A single member Gmail label overrides any earlier Sheet assignment."""
    if len(child_owners) != 1:
        return old_owner, old_assigned_at
    member = next(iter(child_owners))
    if member.casefold() == old_owner.casefold():
        return old_owner or member, old_assigned_at or now
    return member, now


def load_env():
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"'))


def main():
    load_env()
    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    service_file = ROOT / os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"]
    sheet_credentials = service_account.Credentials.from_service_account_file(
        str(service_file), scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    sheets = build("sheets", "v4", credentials=sheet_credentials, cache_discovery=False).spreadsheets()
    config_rows = sheets.values().get(spreadsheetId=sheet_id, range="'Config'!A1:E100").execute().get("values", [])
    config = {row[0]: row[1] for row in config_rows if len(row) > 1}
    label_name = config["gmail_active_label"].strip()
    timezone = ZoneInfo(config.get("timezone", "America/Los_Angeles"))
    internal = {config.get("gmail_mailbox", "").lower(), "partnerships@bluevua.com", "pr@bluevua.com"}
    internal.update(x.strip().lower() for x in config.get("brand_team_emails", "").split(","))

    gmail_credentials = Credentials(
        None, refresh_token=os.environ["GMAIL_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GMAIL_CLIENT_ID"],
        client_secret=os.environ["GMAIL_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/gmail.modify"],
    )
    gmail_credentials.refresh(Request())
    gmail = build("gmail", "v1", credentials=gmail_credentials, cache_discovery=False).users()
    labels = gmail.labels().list(userId="me").execute().get("labels", [])
    active = resolve_active_labels(labels, config_rows)

    existing = sheets.values().get(spreadsheetId=sheet_id, range=f"'{TRACK}'!A1:L10000").execute().get("values", [])
    expected_header = ["Gmail Thread ID", "Reach Out Sent At", "Reply Received At", "Campaign / Context", "KOL  Name", "Email", "Gmail Thread Link", "Added_At", "Assigned Owner", "Assigned At", "Reply Classification", "Gmail Message ID"]
    if not existing or existing[0] != expected_header:
        raise RuntimeError("Track header differs from expected columns; refusing to append")
    by_thread = {row[0]: row for row in existing[1:] if row and row[0]}
    messages = []
    owners_by_thread = {}
    for label_id in active["active_ids"]:
        page_token = None
        while True:
            page = gmail.messages().list(userId="me", labelIds=[label_id], maxResults=500, pageToken=page_token).execute()
            batch = page.get("messages", [])
            messages.extend(batch)
            owner = active["owner_by_label_id"][label_id]
            if owner:
                for item in batch:
                    owners_by_thread.setdefault(item["threadId"], set()).add(owner)
            page_token = page.get("nextPageToken")
            if not page_token:
                break

    rows = []
    manual_owner_changes = []
    owner_conflicts = []
    for thread_id in dict.fromkeys(item["threadId"] for item in messages):
        thread = gmail.threads().get(
            userId="me", id=thread_id, format="metadata",
            metadataHeaders=["From", "To", "Cc", "Subject"],
        ).execute()
        conversation = sorted(thread["messages"], key=lambda msg: int(msg["internalDate"]))
        parsed = []
        for message in conversation:
            headers = {h["name"].lower(): h["value"] for h in message["payload"].get("headers", [])}
            sender_name, sender_email = parseaddr(headers.get("from", ""))
            recipients = getaddresses([headers.get("to", ""), headers.get("cc", "")])
            parsed.append((message, headers, sender_name, sender_email, recipients))
        external_senders = [entry for entry in parsed if entry[3].lower() not in internal]
        external_recipients = [
            (name, address) for _, _, _, _, recipients in parsed
            for name, address in recipients if address.lower() not in internal
        ]
        counterpart = (
            (external_senders[-1][2], external_senders[-1][3]) if external_senders
            else external_recipients[-1] if external_recipients else ("", "")
        )
        latest = parsed[-1]
        latest_from_internal = latest[3].lower() in internal
        # An internal forward to an internal mailbox still needs a human response.
        latest_to_external = any(address.lower() not in internal for _, address in latest[4])
        status = "waiting response" if latest_from_internal and latest_to_external else "needs reply"
        outbound = [entry for entry in parsed if entry[3].lower() in internal and any(address.lower() not in internal for _, address in entry[4])]
        inbound = [entry for entry in parsed if entry[3].lower() not in internal]
        def timestamp(entry):
            return datetime.fromtimestamp(int(entry[0]["internalDate"]) / 1000, timezone).strftime("%Y-%m-%d %H:%M:%S %Z") if entry else ""
        subject = next((entry[1].get("subject", "") for entry in parsed if entry[1].get("subject")), "")
        old = by_thread.get(thread_id, [])
        def old_value(index):
            return old[index] if len(old) > index else ""
        child_owners = owners_by_thread.get(thread_id, set())
        now_text = datetime.now(timezone).strftime("%Y-%m-%d %H:%M:%S %Z")
        owner, assigned_at = owner_assignment(old_value(8), old_value(9), child_owners, now_text)
        if len(child_owners) > 1:
            owner_conflicts.append((thread_id, sorted(child_owners)))
        elif child_owners and owner.casefold() != old_value(8).casefold():
            manual_owner_changes.append((thread_id, old_value(8), owner))
        row = [
            thread_id, timestamp(outbound[-1]) if outbound else "",
            timestamp(inbound[-1]) if inbound else "", old_value(3) or subject,
            old_value(4) or counterpart[0], old_value(5) or counterpart[1],
            f"https://mail.google.com/mail/u/0/#all/{thread_id}",
            old_value(7) if old else datetime.now(timezone).strftime("%Y-%m-%d %H:%M:%S %Z"),
            owner, assigned_at, status, latest[0]["id"],
        ]
        # Threads without an inbound reply belong after all dated replies.
        rows.append((int(inbound[-1][0]["internalDate"]) if inbound else -1, row))
    rows.sort(key=lambda value: value[0], reverse=True)
    # Rewrite complete rows so owner assignments stay attached to their thread.
    old_count = max(0, len(existing) - 1)
    values = [row for _, row in rows] + [[""] * 12 for _ in range(max(0, old_count - len(rows)))]
    if values:
        sheets.values().update(
            spreadsheetId=sheet_id, range=f"'{TRACK}'!A2:L{len(values) + 1}",
            valueInputOption="RAW", body={"values": values},
        ).execute()
    for thread_id, prior, owner in manual_owner_changes:
        print(f"Member label override: {thread_id}: {prior or 'Unassigned'} -> {owner}", flush=True)
    for thread_id, owners in owner_conflicts:
        print(f"Member label conflict: {thread_id}: {', '.join(owners)}; kept existing owner", flush=True)
    print(f"Active label family: {label_name}; labeled messages: {len(messages)}; Track conversations: {len(rows)}")


if __name__ == "__main__":
    warnings.filterwarnings("ignore", category=FutureWarning)
    main()
