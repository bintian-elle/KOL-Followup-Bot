"""Send one Slack daily digest at the configured local time."""

import argparse
import os
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from assign_active import HEADER, ROOT, TAB, cell, load_env, slack_call, truth
from sheet_state import SheetState
from slack_commands import list_label_threads
from sync_needs_review import call
from sync_reach_out import HEADER as REACH_HEADER, TAB as REACH_TAB


def snapshot(active_rows, reach_rows, review_ids):
    active = [row for row in active_rows[1:] if cell(row, 0)]
    reach = [row for row in reach_rows[1:] if cell(row, 0) and cell(row, 6).casefold() == "reach out sent"]
    return {
        "active_ids": sorted(cell(row, 0) for row in active),
        "needs_reply_ids": sorted(cell(row, 0) for row in active if cell(row, 10).casefold() == "needs reply"),
        "needs_review_ids": sorted(review_ids),
        "reach_out_ids": sorted(cell(row, 0) for row in reach),
        "owners": dict(sorted(Counter(cell(row, 8) or "Unassigned" for row in active).items())),
    }


def change(current, previous, key):
    if previous is None:
        return 0, 0
    current_ids = set(current[key])
    previous_ids = set(previous.get(key, []))
    return len(current_ids - previous_ids), len(previous_ids - current_ids)


def digest_text(current, previous, local_date, sheet_id):
    active_added, active_removed = change(current, previous, "active_ids")
    reply_added, reply_removed = change(current, previous, "needs_reply_ids")
    review_added, review_removed = change(current, previous, "needs_review_ids")
    reach_added, reach_removed = change(current, previous, "reach_out_ids")
    previous_owners = (previous or {}).get("owners", {})
    owner_lines = []
    for owner in sorted(set(current["owners"]) | set(previous_owners)):
        total = current["owners"].get(owner, 0)
        delta = total - previous_owners.get(owner, 0)
        delta_text = f" ({delta:+d})" if previous is not None and delta else ""
        owner_lines.append(f"• {owner}: {total}{delta_text}")
    owners = "\n".join(owner_lines) or "• None"
    baseline_note = "\n_First digest baseline: changes start tracking from this report._" if previous is None else ""
    link = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
    return (f"*Bluevua KOL Daily Digest — {local_date}*\n"
            "*Changes since the previous digest*\n"
            f"• Active Track: +{active_added} added / -{active_removed} removed\n"
            f"• Needs Reply: +{reply_added} entered / -{reply_removed} cleared\n"
            f"• Needs Review: +{review_added} added / -{review_removed} removed\n"
            f"• Reach Out awaiting replies: +{reach_added} added / -{reach_removed} removed\n\n"
            "*Current totals*\n"
            f"• Active Track: {len(current['active_ids'])}\n"
            f"• Needs Reply: {len(current['needs_reply_ids'])}\n"
            f"• Needs Review: {len(current['needs_review_ids'])}\n"
            f"• Reach Out awaiting replies: {len(current['reach_out_ids'])}\n"
            "*Tasks by team member*\n"
            f"{owners}"
            f"{baseline_note}\n\n"
            f"View full details: <{link}|Bluevua KOL Follow-up Bot working sheet>")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="print today's digest without sending or saving state")
    parser.add_argument("--initialize", action="store_true", help="save a baseline snapshot without sending")
    parser.add_argument("--force", action="store_true", help="ignore the configured time and today's receipt")
    args = parser.parse_args()
    load_env()
    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    credentials = service_account.Credentials.from_service_account_file(
        str(ROOT / os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    sheets = build("sheets", "v4", credentials=credentials, cache_discovery=False).spreadsheets()
    state_store = SheetState(sheets, sheet_id)
    config_rows = call(sheets.values().get(spreadsheetId=sheet_id, range="'Config'!A1:E100")).get("values", [])
    settings = {cell(row, 0): cell(row, 1) for row in config_rows if len(row) > 1}
    local_tz = ZoneInfo(settings["timezone"].strip())
    now = datetime.now(local_tz)
    send_time = datetime.strptime(settings["daily_send_time"].strip(), "%H:%M").time()
    state = state_store.load("daily_digest", {})
    today = now.date().isoformat()
    if not (args.dry_run or args.initialize or args.force):
        if now.time().replace(second=0, microsecond=0) < send_time:
            print(f"Daily Digest not due until {send_time.strftime('%H:%M')} {local_tz.key}", flush=True)
            return
        if state.get("last_sent_date") == today:
            print(f"Daily Digest already sent for {today}", flush=True)
            return

    active_rows = call(sheets.values().get(spreadsheetId=sheet_id, range=f"'{TAB}'!A1:L10000")).get("values", [])
    reach_rows = call(sheets.values().get(spreadsheetId=sheet_id, range=f"'{REACH_TAB}'!A1:H10000")).get("values", [])
    if not active_rows or active_rows[0] != HEADER or not reach_rows or reach_rows[0] != REACH_HEADER:
        raise RuntimeError("Track columns changed; refusing Daily Digest")

    gmail_credentials = Credentials(
        None, refresh_token=os.environ["GMAIL_REFRESH_TOKEN"], token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GMAIL_CLIENT_ID"], client_secret=os.environ["GMAIL_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/gmail.modify"],
    )
    gmail_credentials.refresh(Request())
    gmail = build("gmail", "v1", credentials=gmail_credentials, cache_discovery=False).users()
    labels = {label["name"].casefold(): label["id"] for label in call(gmail.labels().list(userId="me")).get("labels", [])}
    review_name = settings["gmail_active_label"].strip() + "/" + settings["gmail_needs_review_label"].strip()
    current = snapshot(active_rows, reach_rows, list_label_threads(gmail, labels[review_name.casefold()]))
    previous = state.get("snapshot")
    if state.get("pending_date") == today and state.get("pending_snapshot") and state.get("pending_text"):
        current = state["pending_snapshot"]
        text = state["pending_text"]
    else:
        text = digest_text(current, previous, today, sheet_id)
    if args.dry_run:
        print(text)
        return
    if args.initialize:
        state_store.save("daily_digest", {"last_sent_date": state.get("last_sent_date", ""), "snapshot": current})
        print("Daily Digest baseline initialized", flush=True)
        return

    developer = next((row for row in config_rows if cell(row, 0) == "Testing developer"), None)
    if not developer:
        raise RuntimeError("Testing developer Config row missing")
    developer_mode = truth(cell(developer, 2))
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("SLACK_BOT_TOKEN missing")
    pilot_mode = truth(settings.get("pilot_mode", "FALSE"))
    if developer_mode:
        primary_recipient = cell(developer, 3)
        primary_channel = slack_call(token, "conversations.open", {"users": primary_recipient})["channel"]["id"]
        primary_route = "testing_developer"
    elif pilot_mode:
        primary_recipient = settings["pilot_recipient_slack_id"].strip()
        primary_channel = slack_call(token, "conversations.open", {"users": primary_recipient})["channel"]["id"]
        primary_route = "pilot_recipient"
    else:
        primary_channel = settings["production_channel_id"].strip()
        primary_recipient = primary_channel
        primary_route = "production_channel"
    if not primary_recipient:
        raise RuntimeError(f"Daily Digest {primary_route} is empty")

    targets = [(primary_route, primary_recipient, primary_channel)]
    testing_channel = next((row for row in config_rows if cell(row, 0) == "Testing Channel"), None)
    if testing_channel and truth(cell(testing_channel, 2)):
        testing_channel_id = cell(testing_channel, 3)
        if not testing_channel_id:
            raise RuntimeError("Testing Channel is active but its Slack channel ID is empty")
        if testing_channel_id not in {target[2] for target in targets}:
            targets.append(("testing_channel", testing_channel_id, testing_channel_id))

    deliveries = state.get("deliveries", {}) if state.get("pending_date") == today else {}
    pending_state = {"last_sent_date": state.get("last_sent_date", ""), "pending_date": today,
                     "snapshot": previous, "pending_snapshot": current, "pending_text": text,
                     "deliveries": deliveries}
    state_store.save("daily_digest", pending_state)
    for route, recipient, channel in targets:
        if route in deliveries:
            continue
        sent = slack_call(token, "chat.postMessage", {
            "channel": channel, "text": text, "unfurl_links": False, "unfurl_media": False,
        })
        deliveries[route] = {"recipient": recipient, "ts": sent["ts"]}
        pending_state["deliveries"] = deliveries
        state_store.save("daily_digest", pending_state)
        print(f"Daily Digest delivered via {route}", flush=True)
    pending_state["last_sent_date"] = today
    pending_state["snapshot"] = current
    pending_state.pop("pending_date", None)
    pending_state.pop("pending_snapshot", None)
    pending_state.pop("pending_text", None)
    state_store.save("daily_digest", pending_state)
    print(f"Daily Digest sent for {today} to {len(targets)} destination(s)", flush=True)


if __name__ == "__main__":
    main()
