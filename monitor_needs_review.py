"""Record removals or moves from Needs Review and prevent relabeling.

This module reads Gmail labels only. It never sends messages or changes labels.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from sync_needs_review import ROOT, call, load_env
from sheet_state import SheetState
from gmail_active_labels import resolve_active_labels


EVENTS = ROOT / ".needs-review-events.jsonl"


def classify_changes(previous, current_review, current_active):
    departed = set(previous) - set(current_review)
    moved = departed & set(current_active)
    removed = departed - moved
    return moved, removed


def list_thread_ids_for_label(gmail, label_id):
    ids = set()
    token = None
    while True:
        page = call(gmail.threads().list(userId="me", labelIds=[label_id], maxResults=500, pageToken=token))
        ids.update(item["id"] for item in page.get("threads", []))
        token = page.get("nextPageToken")
        if not token:
            return ids


def main():
    load_env()
    from google.oauth2 import service_account

    sheet_credentials = service_account.Credentials.from_service_account_file(
        str(ROOT / os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    sheets = build("sheets", "v4", credentials=sheet_credentials, cache_discovery=False).spreadsheets()
    state_store = SheetState(sheets, os.environ["GOOGLE_SHEET_ID"])
    rows = call(sheets.values().get(spreadsheetId=os.environ["GOOGLE_SHEET_ID"], range="'Config'!A1:E100")).get("values", [])
    config = {row[0]: row[1] for row in rows if len(row) > 1}

    credentials = Credentials(
        None, refresh_token=os.environ["GMAIL_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=os.environ["GMAIL_CLIENT_ID"], client_secret=os.environ["GMAIL_CLIENT_SECRET"],
        scopes=["https://www.googleapis.com/auth/gmail.modify"],
    )
    credentials.refresh(Request())
    gmail = build("gmail", "v1", credentials=credentials, cache_discovery=False).users()
    labels = call(gmail.labels().list(userId="me")).get("labels", [])
    active = resolve_active_labels(labels, rows)
    current_review = list_thread_ids_for_label(gmail, active["review_id"])
    current_active = set().union(*(list_thread_ids_for_label(gmail, label_id)
                                   for label_id in active["active_ids"]))
    state = state_store.load("needs_review_monitor", {})
    previous = state.get("review_thread_ids", [])
    moved, removed = classify_changes(previous, current_review, current_active)
    suppressed = set(state.get("removed_thread_ids", [])) | removed | moved
    # If a person explicitly applies Needs Review again, accept that decision.
    suppressed -= current_review
    now = datetime.now(timezone.utc).isoformat()
    if moved or removed:
        with EVENTS.open("a") as log:
            for thread_id in sorted(moved):
                log.write(json.dumps({"at": now, "thread_id": thread_id, "change": "moved_to_active"}) + "\n")
            for thread_id in sorted(removed):
                log.write(json.dumps({"at": now, "thread_id": thread_id, "change": "review_label_removed"}) + "\n")
    next_state = {
        "review_thread_ids": sorted(current_review),
        "removed_thread_ids": sorted(suppressed),
        "last_scan_at": now,
    }
    state_store.save("needs_review_monitor", next_state)
    print(f"Needs Review: {len(current_review)}; moved to Active: {len(moved)}; label removed: {len(removed)}", flush=True)


if __name__ == "__main__":
    main()
