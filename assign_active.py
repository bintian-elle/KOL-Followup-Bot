"""Assign new Active Track rows in Config order and notify via Slack DM.

When the Config Testing developer row is active, every notification goes only
to that developer. The Testing Channel is never used by this worker.
"""

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from gmail_active_labels import resolve_active_labels
from sheet_state import SheetState


ROOT = Path(__file__).resolve().parent
TAB = "Bluevua_KOL_Active_Track"
HEADER = ["Gmail Thread ID", "Reach Out Sent At", "Reply Received At", "Campaign / Context", "KOL  Name", "Email", "Gmail Thread Link", "Added_At", "Assigned Owner", "Assigned At", "Reply Classification", "Gmail Message ID"]
COLORS = [
    (0.83, 0.92, 1.00),  # Shanshan: blue
    (0.85, 0.95, 0.84),  # Dan: green
    (1.00, 0.91, 0.75),  # Ivan: orange
    (0.93, 0.86, 0.98),  # Tina: purple
    (1.00, 0.86, 0.89),  # Jason: pink
    (1.00, 0.96, 0.72),  # Jen: yellow
]


def load_env():
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"'))


def truth(value):
    return str(value).strip().casefold() in ("true", "yes", "1")


def cell(row, index):
    return row[index].strip() if len(row) > index else ""


def team_from_config(rows):
    if len(rows) < 7 or rows[1][:5] != ["Member", "Round Robin Order", "Active", "Slack Member ID", "last_round_robin_owner"]:
        raise RuntimeError("Config team rotation columns changed")
    members = []
    for row_number, row in enumerate(rows[2:], 3):
        if not cell(row, 0):
            break
        members.append({"name": cell(row, 0), "order": int(cell(row, 1)), "active": truth(cell(row, 2)),
                        "slack_id": cell(row, 3), "last": truth(cell(row, 4)), "row": row_number})
    if len({m["name"] for m in members}) != len(members) or len({m["order"] for m in members}) != len(members):
        raise RuntimeError("Team rotation contains duplicate names or orders")
    if sum(m["last"] for m in members) != 1:
        raise RuntimeError("Exactly one last_round_robin_owner must be TRUE")
    if not any(m["active"] for m in members):
        raise RuntimeError("No active team member")
    if any(m["active"] and not m["slack_id"] for m in members):
        raise RuntimeError("Active team member missing Slack Member ID")
    return sorted(members, key=lambda m: m["order"])


def next_assignments(members, rows):
    active = [m for m in members if m["active"]]
    last_order = next(m["order"] for m in members if m["last"])
    assignments = []
    for row_number, row in enumerate(rows[1:], 2):
        if not cell(row, 0) or cell(row, 8):
            continue
        owner = next((m for m in active if m["order"] > last_order), active[0])
        assignments.append((row_number, row, owner))
        last_order = owner["order"]
    return assignments


def slack_call(token, method, payload):
    request = urllib.request.Request(
        "https://slack.com/api/" + method,
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                result = json.load(response)
                if not result.get("ok"):
                    raise RuntimeError(f"Slack {method}: {result.get('error', 'unknown error')}")
                return result
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == 3:
                raise
            time.sleep(int(exc.headers.get("Retry-After", "2")))


def slack_text(row, owner):
    mention = "<@" + owner["slack_id"] + ">"
    name = cell(row, 4) or "Unknown KOL"
    email = cell(row, 5)
    subject = cell(row, 3)
    link = cell(row, 6)
    return f"{mention} New KOL task\nKOL: {name} <{email}>\nSubject: {subject}\nGmail: {link}"


def gmail_call(request):
    for attempt in range(8):
        try:
            return request.execute()
        except HttpError as exc:
            if exc.resp.status not in (403, 429, 500, 502, 503) or attempt == 7:
                raise
            time.sleep(min(2 ** attempt, 30))


def label_thread_ids(gmail, label_id):
    thread_ids = set()
    token = None
    while True:
        page = gmail_call(gmail.threads().list(
            userId="me", labelIds=[label_id], maxResults=500, pageToken=token,
        ))
        thread_ids.update(item["id"] for item in page.get("threads", []))
        token = page.get("nextPageToken")
        if not token:
            return thread_ids


def ensure_colors(sheets, spreadsheet_id, tab_id, members, rules):
    existing = {
        rule.get("booleanRule", {}).get("condition", {}).get("values", [{}])[0].get("userEnteredValue")
        for rule in rules if rule.get("booleanRule", {}).get("condition", {}).get("values")
    }
    requests = []
    for member, color in zip(members, COLORS):
        formula = f'=$I2="{member["name"]}"'
        if formula in existing:
            continue
        requests.append({"addConditionalFormatRule": {"index": len(rules) + len(requests), "rule": {
            "ranges": [{"sheetId": tab_id, "startRowIndex": 1, "startColumnIndex": 8, "endColumnIndex": 9}],
            "booleanRule": {"condition": {"type": "CUSTOM_FORMULA", "values": [{"userEnteredValue": formula}]},
                            "format": {"backgroundColor": {"red": color[0], "green": color[1], "blue": color[2]}}},
        }}})
    if requests:
        sheets.batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests}).execute()


def sync_assignment_labels(gmail, gmail_labels, config, rows, state, dry_run=False):
    """Apply each Sheet assignment to Gmail once; later manual edits win."""
    active = resolve_active_labels(gmail_labels, config)
    member_ids = {label_id for label_id, owner in active["owner_by_label_id"].items() if owner}
    label_by_owner = {
        owner.casefold(): label_id for label_id, owner in active["owner_by_label_id"].items() if owner
    }
    label_name = {item["id"]: item["name"] for item in gmail_labels}
    threads_by_member_label = {
        label_id: label_thread_ids(gmail, label_id) for label_id in member_ids
    }
    ledger = state.load("assignment_gmail_labels", {})
    ledger_changed = False
    changed = 0
    skipped_manual = 0
    for row in rows[1:]:
        thread_id, owner = cell(row, 0), cell(row, 8)
        if not thread_id or not owner or thread_id in ledger:
            continue
        target_id = label_by_owner.get(owner.casefold())
        if not target_id:
            print(f"No Gmail member label for assigned owner {owner!r}; skipped {thread_id}", flush=True)
            continue
        current_members = {label_id for label_id, thread_ids in threads_by_member_label.items()
                           if thread_id in thread_ids}
        if target_id in current_members:
            result = "already_present"
        elif current_members:
            result = "manual_override"
            skipped_manual += 1
        else:
            result = "would_add" if dry_run else "added"
            if not dry_run:
                gmail_call(gmail.threads().modify(
                    userId="me", id=thread_id, body={"addLabelIds": [target_id]},
                ))
                threads_by_member_label[target_id].add(thread_id)
            changed += 1
        print(f"Assignment Gmail label: {thread_id}: {owner}: {result}", flush=True)
        if dry_run:
            continue
        ledger[thread_id] = {
            "owner": owner,
            "label": label_name[target_id],
            "result": result,
            "recorded_at": datetime.utcnow().isoformat() + "Z",
        }
        ledger_changed = True
    if ledger_changed:
        state.save("assignment_gmail_labels", ledger)
    print(f"Assignment Gmail labels added: {changed}; manual overrides preserved: {skipped_manual}", flush=True)
    return changed, skipped_manual


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="show assignment plan without writing or sending")
    parser.add_argument("--labels-only", action="store_true", help="sync assigned owners to Gmail labels without assigning or sending Slack")
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
    members = team_from_config(config)
    developer_rows = [r for r in config if cell(r, 0) == "Testing developer"]
    if len(developer_rows) != 1:
        raise RuntimeError("Expected one Testing developer Config row")
    developer_mode = truth(cell(developer_rows[0], 2))
    developer_id = cell(developer_rows[0], 3)
    if developer_mode and not developer_id:
        raise RuntimeError("Testing developer is active but Slack ID is empty")
    settings = {cell(row, 0): cell(row, 1) for row in config if len(row) > 1}
    timezone = ZoneInfo(settings.get("timezone", "America/Los_Angeles"))
    rows = sheets.values().get(spreadsheetId=spreadsheet_id, range=f"'{TAB}'!A1:L10000").execute().get("values", [])
    if not rows or rows[0] != HEADER:
        raise RuntimeError("Active Track columns changed; refusing assignment")
    planned = next_assignments(members, rows)
    for number, row, owner in planned:
        print(f"Row {number}: {cell(row, 0)} -> {owner['name']}", flush=True)
    if args.dry_run and not args.labels_only:
        return
    ledger = state.load("assignment_slack_sent", {})
    if planned and not args.labels_only:
        assigned_at = datetime.now(timezone).strftime("%Y-%m-%d %H:%M:%S %Z")
        last_owner = planned[-1][2]
        updates = [{"range": f"'{TAB}'!I{number}:J{number}", "values": [[owner["name"], assigned_at]]}
                   for number, _, owner in planned]
        updates.extend({"range": f"'Config'!E{member['row']}", "values": [["TRUE" if member is last_owner else "FALSE"]]}
                       for member in members)
        sheets.values().batchUpdate(spreadsheetId=spreadsheet_id,
                                    body={"valueInputOption": "RAW", "data": updates}).execute()
        for number, row, owner in planned:
            while len(row) < 10:
                row.append("")
            row[8:10] = [owner["name"], assigned_at]
    gmail_credentials = Credentials(
        None, refresh_token=os.environ["GMAIL_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GMAIL_CLIENT_ID"], client_secret=os.environ["GMAIL_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/gmail.modify"],
    )
    gmail_credentials.refresh(Request())
    gmail = build("gmail", "v1", credentials=gmail_credentials, cache_discovery=False).users()
    gmail_labels = gmail_call(gmail.labels().list(userId="me")).get("labels", [])
    sync_assignment_labels(gmail, gmail_labels, config, rows, state, dry_run=args.dry_run)
    if args.labels_only or args.dry_run:
        return
    metadata = sheets.get(spreadsheetId=spreadsheet_id,
                          fields="sheets(properties(sheetId,title),conditionalFormats)").execute()
    tab = next(item for item in metadata["sheets"] if item["properties"]["title"] == TAB)
    ensure_colors(sheets, spreadsheet_id, tab["properties"]["sheetId"], members, tab.get("conditionalFormats", []))
    pending = [(row, next(m for m in members if m["name"] == cell(row, 8)))
               for row in rows[1:] if cell(row, 0) and cell(row, 8) and cell(row, 0) not in ledger]
    if not pending:
        print("No unsent assignment notifications", flush=True)
        return
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("SLACK_BOT_TOKEN missing; assignments remain pending for retry")
    for row, owner in pending:
        recipient = developer_id if developer_mode else owner["slack_id"]
        dm = slack_call(token, "conversations.open", {"users": recipient})["channel"]["id"]
        sent = slack_call(token, "chat.postMessage", {"channel": dm, "text": slack_text(row, owner),
                                                       "unfurl_links": False, "unfurl_media": False})
        ledger[cell(row, 0)] = {"owner": owner["name"], "recipient": recipient, "ts": sent["ts"]}
        state.save("assignment_slack_sent", ledger)
        print(f"Notified {recipient} for {cell(row, 0)}", flush=True)
        time.sleep(1.1)


if __name__ == "__main__":
    main()
