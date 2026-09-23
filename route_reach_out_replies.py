"""Move replied Reach Out threads to Needs Review and route Slack by Config."""

import argparse
import json
import os
from datetime import datetime, timezone
from email.utils import parseaddr

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from assign_active import ROOT, cell, load_env, slack_call, truth
from sheet_state import SheetState
from gmail_active_labels import resolve_active_labels
from sync_reach_out import HEADER, TAB, call, first_human_reply, route_complete


def message_text(event, reminder_to_id, test_example=False):
    row = event["row"]
    destination = "This thread is already in Active Track." if event.get("destination") == "active" else "Please review it in Needs Review."
    prefix = "[Test example] " if test_example else ""
    return (f"{prefix}<@{reminder_to_id}> An Upfluence reach out received its first reply. {destination}\n"
            f"KOL: {cell(row, 4) or 'Unknown KOL'} <{cell(row, 5)}>\n"
            f"Subject: {cell(row, 2)}\n"
            f"Gmail: {cell(row, 3)}")


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
    developer_rows = [row for row in config if cell(row, 0) == "Testing developer"]
    if len(developer_rows) != 1:
        raise RuntimeError("Expected exactly one Testing developer row")
    developer_mode = truth(cell(developer_rows[0], 2))
    developer_id = cell(developer_rows[0], 3)
    if developer_mode and not developer_id:
        raise RuntimeError("Testing developer is active but Slack ID is empty")
    channel_rows = [row for row in config if cell(row, 0) == "Testing Channel"]
    if not developer_mode and (len(channel_rows) != 1 or not truth(cell(channel_rows[0], 2)) or not cell(channel_rows[0], 3)):
        raise RuntimeError("Testing Channel must be active and have a Slack channel ID when developer mode is off")
    channel_id = cell(channel_rows[0], 3) if channel_rows else ""
    reminder_to_id = settings.get("New_firstreachout_reminder_to", "").strip()
    if not reminder_to_id:
        raise RuntimeError("New_firstreachout_reminder_to must contain a Slack member ID")
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
    active_ids, review_id = active["active_ids"], active["review_id"]
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
                            "route": "developer" if developer_mode else "team",
                            "developer_ts": "", "channel_ts": "", "dm_ts": "",
                            "created_at": datetime.now(timezone.utc).isoformat()}
        sheet_state.save("reach_out_replies", state)

    if args.dry_run:
        print(f"Pending transitions: {sum(not route_complete(e) for e in state.values())}")
        return

    pending = [(thread_id, event) for thread_id, event in state.items() if not route_complete(event)]
    if not pending:
        print("No pending Reach Out reply transitions", flush=True)
        return
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("SLACK_BOT_TOKEN missing; reply transitions remain pending")
    for thread_id, event in pending:
        if not event["labeled"]:
            thread = call(gmail.threads().get(userId="me", id=thread_id, format="minimal"))
            current_labels = {label for message in thread["messages"] for label in message.get("labelIds", [])}
            already_active = bool(active_ids & current_labels)
            if not already_active and review_id not in current_labels:
                call(gmail.threads().modify(userId="me", id=thread_id, body={"addLabelIds": [review_id]}))
            # If a teammate already moved it to Active, avoid returning it to
            # Needs Review. The notification still describes the reply event.
            event["destination"] = "active" if already_active else "needs_review"
            event["labeled"] = True
            sheet_state.save("reach_out_replies", state)
        text = message_text(event, reminder_to_id)
        event["route"] = "developer" if developer_mode else "team"
        sheet_state.save("reach_out_replies", state)
        if developer_mode:
            if not event.get("developer_ts"):
                dm = slack_call(token, "conversations.open", {"users": developer_id})["channel"]["id"]
                sent = slack_call(token, "chat.postMessage", {"channel": dm, "text": text,
                                                               "unfurl_links": False, "unfurl_media": False})
                event["developer_ts"] = sent["ts"]
                sheet_state.save("reach_out_replies", state)
                print(f"Testing developer notified for {thread_id}", flush=True)
            continue
        if not event["channel_ts"]:
            sent = slack_call(token, "chat.postMessage", {"channel": channel_id, "text": text,
                                                           "unfurl_links": False, "unfurl_media": False})
            event["channel_ts"] = sent["ts"]
            sheet_state.save("reach_out_replies", state)
            print(f"Testing Channel notified for {thread_id}", flush=True)
        if not event["dm_ts"]:
            dm = slack_call(token, "conversations.open", {"users": reminder_to_id})["channel"]["id"]
            sent = slack_call(token, "chat.postMessage", {"channel": dm, "text": text,
                                                           "unfurl_links": False, "unfurl_media": False})
            event["dm_ts"] = sent["ts"]
            sheet_state.save("reach_out_replies", state)
            print(f"First-reply recipient DM notified for {thread_id}", flush=True)


if __name__ == "__main__":
    main()
