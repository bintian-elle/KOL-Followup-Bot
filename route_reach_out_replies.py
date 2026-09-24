"""Move replied Upfluence Reach Out threads directly into Active tracking."""

import argparse
import os
from datetime import datetime, timezone
from email.utils import parseaddr

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from assign_active import ROOT, cell, load_env
from sheet_state import SheetState
from gmail_active_labels import resolve_active_labels
from sync_reach_out import HEADER, TAB, call, first_human_reply, route_complete


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="preview transitions without Gmail or Slack writes")
    args = parser.parse_args()
    load_env()
    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    sheet_credentials = service_account.Credentials.from_service_account_file(
        str(ROOT / os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    sheets = build("sheets", "v4", credentials=sheet_credentials, cache_discovery=False).spreadsheets()
    sheet_state = SheetState(sheets, sheet_id)
    config = call(sheets.values().get(spreadsheetId=sheet_id, range="'Config'!A1:E100")).get("values", [])
    settings = {cell(row, 0): cell(row, 1) for row in config if len(row) > 1}
    rows = call(sheets.values().get(spreadsheetId=sheet_id, range=f"'{TAB}'!A1:H10000")).get("values", [])
    if not rows or rows[0] != HEADER:
        raise RuntimeError("Reach Out Track columns changed; refusing transition")
    state = sheet_state.load("reach_out_replies", {})

    credentials = Credentials(
        None, refresh_token=os.environ["GMAIL_REFRESH_TOKEN"], token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GMAIL_CLIENT_ID"], client_secret=os.environ["GMAIL_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/gmail.modify"],
    )
    credentials.refresh(Request())
    gmail = build("gmail", "v1", credentials=credentials, cache_discovery=False).users()
    labels = call(gmail.labels().list(userId="me")).get("labels", [])
    active = resolve_active_labels(labels, config)
    parent_id, review_id = active["parent_id"], active["review_id"]
    internal = {settings["gmail_mailbox"].strip().lower(), "partnerships@bluevua.com", "pr@bluevua.com"}
    internal.update(address.strip().lower() for address in settings.get("brand_team_emails", "").split(","))

    for row in rows[1:]:
        thread_id = cell(row, 0)
        if not thread_id or thread_id in state:
            continue
        thread = call(gmail.threads().get(userId="me", id=thread_id, format="metadata", metadataHeaders=["From", "Subject"]))
        conversation = sorted(thread["messages"], key=lambda item: int(item["internalDate"]))
        reply = first_human_reply(conversation, internal)
        if not reply:
            continue
        sender = parseaddr(next((h["value"] for h in reply["payload"].get("headers", []) if h["name"].lower() == "from"), ""))[1]
        print(f"Replied Reach Out: {thread_id} from {sender}", flush=True)
        if args.dry_run:
            continue
        state[thread_id] = {"row": row, "reply_id": reply["id"], "labeled": False,
                            "created_at": datetime.now(timezone.utc).isoformat()}
        sheet_state.save("reach_out_replies", state)

    if args.dry_run:
        print(f"Pending transitions: {sum(not route_complete(e) for e in state.values())}")
        return

    pending = [(thread_id, event) for thread_id, event in state.items() if not route_complete(event)]
    if not pending:
        print("No pending Reach Out reply transitions", flush=True)
        return
    for thread_id, event in pending:
        if not event["labeled"]:
            thread = call(gmail.threads().get(userId="me", id=thread_id, format="minimal"))
            current_labels = {label for message in thread["messages"] for label in message.get("labelIds", [])}
            body = {"addLabelIds": [] if parent_id in current_labels else [parent_id],
                    "removeLabelIds": [review_id] if review_id in current_labels else []}
            if body["addLabelIds"] or body["removeLabelIds"]:
                call(gmail.threads().modify(userId="me", id=thread_id, body=body))
            event["labeled"] = True
            event["destination"] = "active"
            event["completed_at"] = datetime.now(timezone.utc).isoformat()
            sheet_state.save("reach_out_replies", state)
            print(f"Moved replied Reach Out directly to Active: {thread_id}", flush=True)


if __name__ == "__main__":
    main()
