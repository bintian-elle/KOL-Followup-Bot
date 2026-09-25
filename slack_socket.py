"""Handle Slack DM commands immediately over Socket Mode."""

import asyncio
import json
import os
import urllib.request
import uuid
from datetime import datetime, timezone

import websockets
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from assign_active import HEADER, ROOT, TAB, cell, load_env, slack_call, team_from_config, truth
from slack_commands import list_label_threads, remove_all_review_labels, summary_text, task_text
from sync_needs_review import call
from sync_reach_out import HEADER as REACH_HEADER, TAB as REACH_TAB
from gmail_active_labels import resolve_active_labels
from rm_audit import append_batch, ensure_tab, mark_batch_removed


def socket_url(app_token):
    request = urllib.request.Request(
        "https://slack.com/api/apps.connections.open",
        data=b"{}",
        headers={"Authorization": "Bearer " + app_token, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        result = json.load(response)
    if not result.get("ok"):
        raise RuntimeError("Slack apps.connections.open: " + result.get("error", "unknown error"))
    return result["url"]


class CommandHandler:
    def __init__(self):
        load_env()
        self.sheet_id = os.environ["GOOGLE_SHEET_ID"]
        credentials = service_account.Credentials.from_service_account_file(
            str(ROOT / os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"]),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        self.sheets = build("sheets", "v4", credentials=credentials, cache_discovery=False).spreadsheets()
        config = self.refresh_authorized()
        settings = {cell(row, 0): cell(row, 1) for row in config if len(row) > 1}
        gmail_credentials = Credentials(
            None, refresh_token=os.environ["GMAIL_REFRESH_TOKEN"], token_uri="https://oauth2.googleapis.com/token",
            client_id=os.environ["GMAIL_CLIENT_ID"], client_secret=os.environ["GMAIL_CLIENT_SECRET"],
            scopes=["https://www.googleapis.com/auth/gmail.modify"],
        )
        gmail_credentials.refresh(Request())
        self.gmail = build("gmail", "v1", credentials=gmail_credentials, cache_discovery=False).users()
        labels = call(self.gmail.labels().list(userId="me")).get("labels", [])
        active = resolve_active_labels(labels, config)
        self.review_id = active["review_id"]
        self.active_family_label_ids = active["active_ids"] | {self.review_id}
        self.label_name_by_id = {item["id"]: item["name"] for item in labels}
        ensure_tab(self.sheets, self.sheet_id)
        self.bot_token = os.environ["SLACK_BOT_TOKEN"]

    def refresh_authorized(self):
        config = call(self.sheets.values().get(spreadsheetId=self.sheet_id, range="'Config'!A1:E100")).get("values", [])
        members = team_from_config(config)
        self.member_by_slack_id = {member["slack_id"]: member for member in members if member["slack_id"]}
        shanshan = next((member for member in members if member["name"].casefold() == "shanshan"), None)
        settings = {cell(row, 0): cell(row, 1) for row in config if len(row) > 1}
        self.rm_authorized = {settings.get("pilot_recipient_slack_id", "").strip()}
        if shanshan:
            self.rm_authorized.add(shanshan["slack_id"])
        self.rm_authorized.discard("")
        developer = next((row for row in config if cell(row, 0) == "Testing developer"), None)
        if not developer:
            raise RuntimeError("Testing developer Config row missing")
        developer_mode = truth(cell(developer, 2))
        self.authorized = ({cell(developer, 3)} if developer_mode
                           else {member["slack_id"] for member in members if member["active"]})
        self.authorized.discard("")
        return config

    def handle(self, event):
        if event.get("channel_type") != "im" or event.get("subtype") or event.get("bot_id"):
            return
        user_id = event.get("user", "")
        command = event.get("text", "").strip().casefold()
        self.refresh_authorized()
        if command not in ("rm", "summary", "task"):
            return
        if command == "rm" and user_id not in self.rm_authorized:
            return
        if command != "rm" and user_id not in self.authorized:
            return
        if command == "rm":
            thread_ids = list_label_threads(self.gmail, self.review_id)
            snapshots = []
            for thread_id in thread_ids:
                thread = call(self.gmail.threads().get(
                    userId="me", id=thread_id, format="metadata", metadataHeaders=["Subject"],
                ))
                labels = {label_id for message in thread["messages"] for label_id in message.get("labelIds", [])}
                subject = ""
                for message in thread["messages"]:
                    subject = next((header["value"] for header in message["payload"].get("headers", [])
                                    if header["name"].casefold() == "subject"), subject)
                    if subject:
                        break
                snapshots.append({"thread_id": thread_id, "subject": subject,
                                  "labels": sorted(self.label_name_by_id[label_id]
                                                   for label_id in labels & self.active_family_label_ids)})
            batch_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
            append_batch(self.sheets, self.sheet_id, batch_id, user_id, snapshots)
            removed = remove_all_review_labels(self.gmail, self.review_id, self.active_family_label_ids, thread_ids)
            marked = mark_batch_removed(self.sheets, self.sheet_id, batch_id)
            if marked != removed:
                raise RuntimeError(f"RM_Audit batch {batch_id} recorded {marked} of {removed} removed threads")
            response = (f"Removed all Bluevua KOL Active labels from {removed} Needs Review email threads. "
                        f"Audit batch: `{batch_id}`. The emails are no longer in Needs Review or Active Track. "
                        "No emails were deleted.")
        elif command == "summary":
            active_rows = call(self.sheets.values().get(spreadsheetId=self.sheet_id, range=f"'{TAB}'!A1:L10000")).get("values", [])
            reach_rows = call(self.sheets.values().get(spreadsheetId=self.sheet_id, range=f"'{REACH_TAB}'!A1:H10000")).get("values", [])
            if not active_rows or active_rows[0] != HEADER or not reach_rows or reach_rows[0] != REACH_HEADER:
                raise RuntimeError("Track columns changed; refusing summary")
            response = summary_text(active_rows, reach_rows, len(list_label_threads(self.gmail, self.review_id)), self.sheet_id)
        else:
            active_rows = call(self.sheets.values().get(spreadsheetId=self.sheet_id, range=f"'{TAB}'!A1:L10000")).get("values", [])
            if not active_rows or active_rows[0] != HEADER:
                raise RuntimeError("Active Track columns changed; refusing task lookup")
            member = self.member_by_slack_id.get(user_id)
            response = (task_text(active_rows, member["name"]) if member else
                        "Your Slack account is not associated with a Team rotation member in Config.")
        slack_call(self.bot_token, "chat.postMessage", {
            "channel": event["channel"], "text": response, "unfurl_links": False, "unfurl_media": False,
        })
        print(f"Handled immediate Slack command {command!r} from {user_id}", flush=True)


async def listen_once(handler, app_token):
    async with websockets.connect(socket_url(app_token), ping_interval=20, ping_timeout=20) as socket:
        print("Slack Socket Mode connected", flush=True)
        async for raw in socket:
            envelope = json.loads(raw)
            envelope_id = envelope.get("envelope_id")
            if envelope_id:
                await socket.send(json.dumps({"envelope_id": envelope_id}))
            if envelope.get("type") != "events_api":
                continue
            event = envelope.get("payload", {}).get("event", {})
            try:
                await asyncio.to_thread(handler.handle, event)
            except Exception as exc:
                print(f"Slack command failed: {exc}", flush=True)


async def main():
    load_env()
    app_token = os.environ.get("SLACK_APP_TOKEN", "")
    if not app_token.startswith("xapp-"):
        raise RuntimeError("SLACK_APP_TOKEN missing; create an xapp token with connections:write")
    handler = await asyncio.to_thread(CommandHandler)
    while True:
        try:
            await listen_once(handler, app_token)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"Slack Socket Mode disconnected: {exc}; reconnecting in 5 seconds", flush=True)
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
