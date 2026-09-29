"""Move completed or declined Gmail threads out of Active tracking.

The user applies one closure label in Gmail. This worker preserves that label,
removes all live Bluevua tracking labels, and upserts a visible Closed_Track row.
It never deletes or archives email and never sends Slack messages.
"""

import os
from datetime import datetime
from email.utils import getaddresses, parseaddr
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from assign_active import HEADER as ACTIVE_HEADER, ROOT, TAB as ACTIVE_TAB, cell, load_env
from gmail_active_labels import closed_label_names, resolve_active_labels, resolve_closed_labels
from sheet_state import SheetState
from sync_needs_review import call


TAB = "Closed_Track"
HEADER = [
    "Gmail Thread ID", "Closed At", "Outcome", "KOL Name", "Email",
    "Subject", "Assigned Owner", "Gmail Thread Link", "Previous Bluevua Labels",
]


def ensure_tab(sheets, spreadsheet_id):
    metadata = call(sheets.get(
        spreadsheetId=spreadsheet_id,
        fields="sheets(properties(sheetId,title))",
    ))
    found = next((item for item in metadata["sheets"]
                  if item["properties"]["title"] == TAB), None)
    if found:
        rows = call(sheets.values().get(
            spreadsheetId=spreadsheet_id, range=f"'{TAB}'!A1:I1",
        )).get("values", [])
        if not rows or rows[0] != HEADER:
            raise RuntimeError("Closed_Track header differs from expected columns")
        return found["properties"]["sheetId"]
    response = call(sheets.batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": [{
        "addSheet": {"properties": {
            "title": TAB, "hidden": False,
            "gridProperties": {"frozenRowCount": 1, "rowCount": 1000, "columnCount": len(HEADER)},
        }},
    }]}))
    sheet_id = response["replies"][0]["addSheet"]["properties"]["sheetId"]
    call(sheets.values().update(
        spreadsheetId=spreadsheet_id, range=f"'{TAB}'!A1:I1",
        valueInputOption="RAW", body={"values": [HEADER]},
    ))
    call(sheets.batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": [{
        "repeatCell": {
            "range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {
                "backgroundColor": {"red": 0.12, "green": 0.47, "blue": 0.71},
                "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat)",
        },
    }]}))
    return sheet_id


def list_thread_ids(gmail, label_id):
    ids = set()
    token = None
    while True:
        page = call(gmail.threads().list(
            userId="me", labelIds=[label_id], maxResults=500, pageToken=token,
        ))
        ids.update(item["id"] for item in page.get("threads", []))
        token = page.get("nextPageToken")
        if not token:
            return ids


def counterpart(thread, internal):
    conversation = sorted(thread["messages"], key=lambda message: int(message["internalDate"]))
    external_senders = []
    external_recipients = []
    subject = ""
    for message in conversation:
        headers = {h["name"].lower(): h["value"] for h in message["payload"].get("headers", [])}
        subject = subject or headers.get("subject", "")
        name, address = parseaddr(headers.get("from", ""))
        if address and address.casefold() not in internal:
            external_senders.append((name, address))
        external_recipients.extend(
            (name, address) for name, address in getaddresses([headers.get("to", ""), headers.get("cc", "")])
            if address and address.casefold() not in internal
        )
    name, email = external_senders[-1] if external_senders else (
        external_recipients[-1] if external_recipients else ("", "")
    )
    return name, email, subject


def clear_notification_state(state_store, thread_id):
    for namespace in ("assignment_slack_sent", "reply_reminders_sent"):
        state = state_store.load(namespace, {})
        if thread_id in state:
            state.pop(thread_id)
            state_store.save(namespace, state)


def main():
    load_env()
    spreadsheet_id = os.environ["GOOGLE_SHEET_ID"]
    sheet_credentials = service_account.Credentials.from_service_account_file(
        str(ROOT / os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    sheets = build("sheets", "v4", credentials=sheet_credentials, cache_discovery=False).spreadsheets()
    config_rows = call(sheets.values().get(
        spreadsheetId=spreadsheet_id, range="'Config'!A1:E100",
    )).get("values", [])
    settings = {cell(row, 0): cell(row, 1) for row in config_rows if len(row) > 1}
    timezone = ZoneInfo(settings.get("timezone", "America/Los_Angeles"))
    active_rows = call(sheets.values().get(
        spreadsheetId=spreadsheet_id, range=f"'{ACTIVE_TAB}'!A1:L10000",
    )).get("values", [])
    if not active_rows or active_rows[0] != ACTIVE_HEADER:
        raise RuntimeError("Active Track columns changed; refusing closure processing")
    active_by_thread = {cell(row, 0): row for row in active_rows[1:] if cell(row, 0)}
    ensure_tab(sheets, spreadsheet_id)
    closed_rows = call(sheets.values().get(
        spreadsheetId=spreadsheet_id, range=f"'{TAB}'!A1:I10000",
    )).get("values", [])
    row_by_thread = {cell(row, 0): number for number, row in enumerate(closed_rows[1:], 2) if cell(row, 0)}

    gmail_credentials = Credentials(
        None, refresh_token=os.environ["GMAIL_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GMAIL_CLIENT_ID"], client_secret=os.environ["GMAIL_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/gmail.modify"],
    )
    gmail_credentials.refresh(Request())
    gmail = build("gmail", "v1", credentials=gmail_credentials, cache_discovery=False).users()
    labels = call(gmail.labels().list(userId="me")).get("labels", [])
    label_name_by_id = {item["id"]: item["name"] for item in labels}
    active = resolve_active_labels(labels, config_rows)
    closed = resolve_closed_labels(labels, config_rows)
    closure_names = closed_label_names(config_rows)
    threads_by_outcome = {outcome: list_thread_ids(gmail, label_id)
                          for outcome, label_id in closed.items()}
    candidates = set().union(*threads_by_outcome.values())
    state_store = SheetState(sheets, spreadsheet_id)
    processed = state_store.load("closed_threads", {})
    next_processed = {thread_id: value for thread_id, value in processed.items()
                      if thread_id in candidates}
    internal = {settings.get("gmail_mailbox", "").casefold(), "partnerships@bluevua.com", "pr@bluevua.com"}
    internal.update(address.strip().casefold()
                    for address in settings.get("brand_team_emails", "").split(",") if address.strip())
    tracking_ids = set(active["active_ids"]) | {active["review_id"]}
    closed_count = 0
    conflicts = 0
    for thread_id in sorted(candidates):
        outcomes = [outcome for outcome, ids in threads_by_outcome.items() if thread_id in ids]
        if len(outcomes) != 1:
            conflicts += 1
            print(f"Closure label conflict: {thread_id}: {', '.join(outcomes)}; skipped", flush=True)
            continue
        outcome = outcomes[0]
        if processed.get(thread_id, {}).get("outcome") == outcome and thread_id not in active_by_thread:
            continue
        thread = call(gmail.threads().get(
            userId="me", id=thread_id, format="metadata",
            metadataHeaders=["From", "To", "Cc", "Subject"],
        ))
        current_label_ids = {label_id for message in thread["messages"]
                             for label_id in message.get("labelIds", [])}
        previous_ids = current_label_ids & tracking_ids
        active_row = active_by_thread.get(thread_id, [])
        name, email, subject = counterpart(thread, internal)
        row = [
            thread_id,
            datetime.now(timezone).strftime("%Y-%m-%d %H:%M:%S %Z"),
            outcome,
            cell(active_row, 4) or name,
            cell(active_row, 5) or email,
            cell(active_row, 3) or subject,
            cell(active_row, 8),
            f"https://mail.google.com/mail/u/0/#all/{thread_id}",
            ", ".join(sorted(label_name_by_id[label_id] for label_id in previous_ids)),
        ]
        row_number = row_by_thread.get(thread_id)
        if row_number:
            old = closed_rows[row_number - 1]
            if cell(old, 2) == outcome:
                row[1] = cell(old, 1) or row[1]
                # Preserve the original closure snapshot on later scans. The
                # Gmail thread remains under the closure label indefinitely.
                for index in range(3, len(HEADER)):
                    row[index] = cell(old, index) or row[index]
            call(sheets.values().update(
                spreadsheetId=spreadsheet_id, range=f"'{TAB}'!A{row_number}:I{row_number}",
                valueInputOption="RAW", body={"values": [row]},
            ))
        else:
            call(sheets.values().append(
                spreadsheetId=spreadsheet_id, range=f"'{TAB}'!A:I",
                valueInputOption="RAW", insertDataOption="INSERT_ROWS", body={"values": [row]},
            ))
            row_by_thread[thread_id] = len(row_by_thread) + 2
        if previous_ids:
            call(gmail.threads().modify(
                userId="me", id=thread_id, body={"removeLabelIds": sorted(previous_ids)},
            ))
        clear_notification_state(state_store, thread_id)
        next_processed[thread_id] = {
            "outcome": outcome,
            "closed_at": row[1],
        }
        closed_count += 1
        print(f"Closed: {thread_id}: {outcome}; kept {closure_names[outcome]}", flush=True)
    if next_processed != processed:
        state_store.save("closed_threads", next_processed)
    print(f"Closed label threads: {len(candidates)}; processed: {closed_count}; conflicts: {conflicts}", flush=True)


if __name__ == "__main__":
    main()
