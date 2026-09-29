"""Publish command help to authorized users' Slack App Home tabs."""

import os

from google.oauth2 import service_account
from googleapiclient.discovery import build

from assign_active import ROOT, cell, load_env, slack_call, team_from_config, truth


def home_view(sheet_id):
    sheet_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
    return {
        "type": "home",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "Bluevua KOL Follow-up Bot", "emoji": True}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "Send one of these commands in the app's *Messages* tab:"}},
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": "*`task` — View your current tasks*\nLists your assigned KOLs, reply status, and Gmail links."}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "*`summary` — View the current summary*\n• Active Track and Needs Reply totals\n• Needs Review total\n• Task count by team member\n• Reach Out awaiting reply total\n• Google Sheet link"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "*`rm` — Remove Needs Review threads from Bluevua tracking Label (Shanshan and Candice only)*\nThe emails will leave Active Track after the next scan. It does not delete emails. This runs immediately."}},
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": "*Gmail labels*\n• `01_Needs Review` — New inbound KOL emails waiting for review\n• `02_Upfluence Replied` — Upfluence outreach that received its first reply\n• Member name — Manually assign the task to that member\n• `03_Completed` — Collaboration or communication completed\n• `04_Not Moving Forward` — We decided not to proceed\n\nMove finished tasks to `03_Completed` or `04_Not Moving Forward`. The bot removes them from Active Track and records them in `Closed_Track`. Emails are never deleted."}},
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"<{sheet_url}|Open the Bluevua KOL Follow-up Bot working sheet>"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "• *Bluevua_KOL_Active_Track* — Ongoing task tracking\n• *Reach_Out_Track* — Reach outs sent through Upfluence that are still awaiting replies\n• *Closed_Track* — Completed and Not Moving Forward history\n• *RM_Audit* — Recoverable history of labels removed by the `rm` command\n• *Config* — Other bot settings"}},
        ],
    }


def home_users(config):
    """Return everyone who needs task help or administrative instructions."""
    members = team_from_config(config)
    developer = next((row for row in config if cell(row, 0) == "Testing developer"), None)
    if not developer:
        raise RuntimeError("Testing developer Config row missing")
    developer_id = cell(developer, 3)
    if truth(cell(developer, 2)):
        return [developer_id] if developer_id else []
    settings = {cell(row, 0): cell(row, 1) for row in config if len(row) > 1}
    users = [member["slack_id"] for member in members if member["active"]]
    users.extend([settings.get("pilot_recipient_slack_id", "").strip(), developer_id])
    return [user for user in dict.fromkeys(users) if user]


def main():
    load_env()
    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    credentials = service_account.Credentials.from_service_account_file(
        str(ROOT / os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    sheets = build("sheets", "v4", credentials=credentials, cache_discovery=False).spreadsheets()
    values = sheets.values()
    config = values.get(spreadsheetId=sheet_id, range="'Config'!A1:E100").execute().get("values", [])
    users = home_users(config)
    view = home_view(sheet_id)
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("SLACK_BOT_TOKEN missing")
    changed = 0
    for user_id in users:
        slack_call(token, "views.publish", {"user_id": user_id, "view": view})
        changed += 1
        print(f"Published Slack Home for {user_id}", flush=True)
    print(f"Slack Home users: {len(users)}; updated: {changed}", flush=True)


if __name__ == "__main__":
    main()
