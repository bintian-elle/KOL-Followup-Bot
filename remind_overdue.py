"""Send one reminder per unanswered KOL reply after 24 hours.

The active Testing developer recipient in Config overrides member delivery.
"""

import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build

from assign_active import HEADER, ROOT, TAB, cell, load_env, slack_call, team_from_config, truth
from sheet_state import SheetState


def reply_instant(value, local_tz):
    """Sheet timestamps contain a timezone suffix; Config defines its zone."""
    local = datetime.strptime(value[:16], "%Y-%m-%d %H:%M").replace(tzinfo=local_tz)
    return local.astimezone(timezone.utc)


def due_reply(row, now, local_tz, sent):
    thread_id, received, owner, classification = (cell(row, i) for i in (0, 2, 8, 10))
    if not thread_id or not received or not owner or classification.casefold() != "needs reply":
        return False
    # The Track message ID can change after an internal forward without a new
    # creator reply. Reply Received At identifies the reply being reminded.
    prior_keys = sent.get(thread_id, {})
    if received in prior_keys or any(key.startswith(received + "|") for key in prior_keys):
        return False
    # Ignore seconds on both sides of the 24-hour comparison.
    minute_now = now.astimezone(timezone.utc).replace(second=0, microsecond=0)
    return minute_now - reply_instant(received, local_tz) >= timedelta(hours=24)


def reminder_text(row, owner):
    return (f"<@{owner['slack_id']}> KOL reply reminder: more than 24 hours have passed\n"
            f"KOL: {cell(row, 4) or 'Unknown KOL'} <{cell(row, 5)}>\n"
            f"Subject: {cell(row, 3)}\n"
            f"Reply Received At: {cell(row, 2)}\n"
            f"Gmail: {cell(row, 6)}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="show due replies without sending Slack")
    args = parser.parse_args()
    load_env()
    spreadsheet_id = os.environ["GOOGLE_SHEET_ID"]
    credentials = service_account.Credentials.from_service_account_file(
        str(ROOT / os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    sheets = build("sheets", "v4", credentials=credentials, cache_discovery=False).spreadsheets()
    state = SheetState(sheets, spreadsheet_id)
    config = sheets.values().get(spreadsheetId=spreadsheet_id, range="'Config'!A1:E100").execute().get("values", [])
    members = {member["name"]: member for member in team_from_config(config)}
    developer_rows = [row for row in config if cell(row, 0) == "Testing developer"]
    if len(developer_rows) != 1:
        raise RuntimeError("Expected exactly one Testing developer row")
    developer_mode = truth(cell(developer_rows[0], 2))
    developer_id = cell(developer_rows[0], 3)
    if developer_mode and not developer_id:
        raise RuntimeError("Testing developer is active but Slack ID is empty")
    settings = {cell(row, 0): cell(row, 1) for row in config if len(row) > 1}
    local_tz = ZoneInfo(settings.get("timezone", "America/Los_Angeles"))
    rows = sheets.values().get(spreadsheetId=spreadsheet_id, range=f"'{TAB}'!A1:L10000").execute().get("values", [])
    if not rows or rows[0] != HEADER:
        raise RuntimeError("Active Track columns changed; refusing reminders")
    sent = state.load("reply_reminders_sent", {})
    now = datetime.now(timezone.utc)
    due = [row for row in rows[1:] if due_reply(row, now, local_tz, sent)]
    for row in due:
        print(f"Due: {cell(row, 0)} -> {cell(row, 8)} at {cell(row, 2)}", flush=True)
    if args.dry_run or not due:
        print(f"Due reminders: {len(due)}", flush=True)
        return
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("SLACK_BOT_TOKEN missing")
    for row in due:
        owner = members.get(cell(row, 8))
        if not owner or not owner["slack_id"]:
            raise RuntimeError(f"Assigned owner {cell(row, 8)!r} has no Slack ID")
        recipient = developer_id if developer_mode else owner["slack_id"]
        dm = slack_call(token, "conversations.open", {"users": recipient})["channel"]["id"]
        result = slack_call(token, "chat.postMessage", {
            "channel": dm, "text": reminder_text(row, owner), "unfurl_links": False, "unfurl_media": False,
        })
        thread_id = cell(row, 0)
        reply_key = cell(row, 2)
        sent.setdefault(thread_id, {})[reply_key] = {"recipient": recipient, "ts": result["ts"]}
        state.save("reply_reminders_sent", sent)
        print(f"Reminded {recipient} for {thread_id}", flush=True)
        time.sleep(1.1)


if __name__ == "__main__":
    main()
