"""One-time import of legacy local JSON state into Bot_State."""

import json
import os

from google.oauth2 import service_account
from googleapiclient.discovery import build

from assign_active import ROOT, load_env
from sheet_state import SheetState


FILES = {
    "assignment_slack_sent": ".assignment-slack-sent.json",
    "reply_reminders_sent": ".reply-reminders-sent.json",
    "reach_out_replies": ".reach-out-replies.json",
    "needs_review_monitor": ".needs-review-monitor.json",
    "needs_review_scan": ".needs-review-state.json",
}


def main():
    load_env()
    credentials = service_account.Credentials.from_service_account_file(
        str(ROOT / os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    sheets = build("sheets", "v4", credentials=credentials, cache_discovery=False).spreadsheets()
    state = SheetState(sheets, os.environ["GOOGLE_SHEET_ID"])
    for namespace, filename in FILES.items():
        existing = state.load(namespace, None)
        path = ROOT / filename
        if existing is not None:
            print(f"kept existing {namespace}")
            continue
        value = json.loads(path.read_text()) if path.exists() else {}
        state.save(namespace, value)
        print(f"migrated {namespace}: {len(value) if hasattr(value, '__len__') else 1}")


if __name__ == "__main__":
    main()
