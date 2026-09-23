"""Label new, unanswered Bluevua creator inquiries for human review.

The Gmail OAuth refresh token needs the gmail.modify scope. This script only
changes Gmail labels; it does not write Sheets or send Slack messages.
"""

import argparse
import fcntl
import json
import os
import time
from datetime import datetime
from email.utils import getaddresses, parseaddr
from pathlib import Path
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from sheet_state import SheetState
from gmail_active_labels import resolve_active_labels


ROOT = Path(__file__).resolve().parent
LOCK = ROOT / ".needs-review.lock"


def load_env():
    for line in (ROOT / ".env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"'))


def call(request):
    for attempt in range(9):
        try:
            return request.execute()
        except HttpError as exc:
            if exc.resp.status not in (403, 429, 500, 502, 503) or attempt == 8:
                raise
            time.sleep(min(2 ** attempt, 60))


def list_thread_ids(gmail, query):
    ids = set()
    token = None
    while True:
        page = call(gmail.threads().list(userId="me", q=query, maxResults=500, pageToken=token))
        ids.update(item["id"] for item in page.get("threads", []))
        token = page.get("nextPageToken")
        if not token:
            return ids


def settings(sheets, spreadsheet_id):
    rows = call(sheets.values().get(spreadsheetId=spreadsheet_id, range="'Config'!A1:E100")).get("values", [])
    return rows, {row[0]: row[1] for row in rows if len(row) > 1}


def words(value):
    return [word.strip().casefold() for word in value.split(",") if word.strip()]


def first_headers(message):
    return {header["name"].lower(): header["value"] for header in message["payload"].get("headers", [])}


def creator_inquiry(thread, config, active_labels, review_label, scan_start_ms):
    messages = sorted(thread["messages"], key=lambda message: int(message["internalDate"]))
    if int(messages[0]["internalDate"]) < scan_start_ms:
        return False, "older conversation"
    labels = {label for message in messages for label in message.get("labelIds", [])}
    if labels & set(active_labels) or review_label in labels:
        return False, "already labeled"

    headers = [first_headers(message) for message in messages]
    senders = [parseaddr(header.get("from", ""))[1].casefold() for header in headers]
    mailbox = config["gmail_mailbox"].strip().casefold()
    if any(sender.endswith("@elle-media.com") for sender in senders):
        return False, "team already replied"
    first_sender = senders[0]
    brand_team = set(words(config.get("brand_team_emails", "")))
    forwarding = config.get("forwarding_mailbox", "").strip().casefold()
    handoff = first_sender in brand_team or first_sender == forwarding
    first = messages[0]
    subject = headers[0].get("subject", "")
    text = (subject + " " + first.get("snippet", "")).casefold()

    if first_sender.startswith(("no-reply@", "noreply@", "mailer-daemon@", "postmaster@", "notifications@upfluence.", "notice@qiye.")):
        return False, "automatic sender"
    if headers[0].get("auto-submitted") or headers[0].get("list-unsubscribe"):
        return False, "automated or bulk mail"
    if any(word in text for word in words(config.get("other_brand_keywords", ""))):
        return False, "other brand"
    if any(word in text for word in words(config.get("completed_collab_keywords", ""))):
        return False, "completed collaboration"

    explicit_brand = config.get("campaign_name_contains", "Bluevua").strip().casefold() in text
    generic_subject = any(word in subject.casefold() for word in words(config.get("generic_collab_keywords", "")))
    if not explicit_brand and not generic_subject:
        return False, "no creator or brand context"
    if "certifications" in subject.casefold() and not generic_subject:
        return False, "product support question"

    if handoff:
        recipients = {address.casefold() for _, address in getaddresses([headers[0].get("to", ""), headers[0].get("cc", "")])}
        if mailbox not in recipients and forwarding not in recipients:
            return False, "handoff not sent to partnerships"
        handoff_signals = ("forwarded message", "thanks for reaching out", "thanks so much for reaching out",
                           "looping us in", "included our partnerships team", "including our partnerships team")
        if not any(signal in text for signal in handoff_signals):
            return False, "no original inquiry in brand handoff"
    else:
        if headers[0].get("in-reply-to"):
            return False, "reply to earlier outreach"
        if subject.casefold().startswith("re:") and "thanks so much for getting back" in text:
            return False, "continuation of earlier brand conversation"
        # Config excludes unsolicited UGC pitches unless a prior Bluevua
        # campaign is established. A newly initiated thread has no such proof.
        if "ugc" in text or "ugc" in first_sender.split("@")[0]:
            return False, "unsolicited UGC pitch"
        vendor_signals = ("affiliate recruitment", "affiliate growth", "improving affiliate", "affiliate support")
        vendor_program = "affiliate program" in text and not generic_subject
        if vendor_program or any(signal in text for signal in vendor_signals):
            return False, "affiliate service pitch"
    return True, "brand handoff" if handoff else "creator inquiry"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="scan without changing Gmail labels or checkpoint")
    parser.add_argument("--full-scan", action="store_true", help="ignore the incremental checkpoint")
    args = parser.parse_args()
    load_env()

    with LOCK.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another Needs Review scan is running; skipped")
            return

        sheet_credentials = service_account.Credentials.from_service_account_file(
            str(ROOT / os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"]),
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        sheets = build("sheets", "v4", credentials=sheet_credentials, cache_discovery=False).spreadsheets()
        state_store = SheetState(sheets, os.environ["GOOGLE_SHEET_ID"])
        config_rows, config = settings(sheets, os.environ["GOOGLE_SHEET_ID"])
        tz = ZoneInfo(config.get("timezone", "America/Los_Angeles"))
        config_start = int(datetime.strptime(config["gmail_scan_start_at"].strip(), "%Y-%m-%d").replace(tzinfo=tz).timestamp())
        scan_started = int(time.time())
        checkpoint = state_store.load("needs_review_scan", {})
        since = config_start
        if not args.full_scan and checkpoint.get("config_start") == config_start:
            since = max(config_start, int(checkpoint["last_successful_scan_started"]) - 300)

        gmail_credentials = Credentials(
            None, refresh_token=os.environ["GMAIL_REFRESH_TOKEN"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=os.environ["GMAIL_CLIENT_ID"], client_secret=os.environ["GMAIL_CLIENT_SECRET"],
            scopes=["https://www.googleapis.com/auth/gmail.modify"],
        )
        gmail_credentials.refresh(Request())
        gmail = build("gmail", "v1", credentials=gmail_credentials, cache_discovery=False).users()
        labels = call(gmail.labels().list(userId="me")).get("labels", [])
        active = resolve_active_labels(labels, config_rows)
        active_labels = active["active_ids"]
        review_label = active["review_id"]

        generic = words(config.get("generic_collab_keywords", ""))
        subject_terms = " ".join('subject:"' + term + '"' for term in generic)
        brand = config.get("campaign_name_contains", "Bluevua").strip()
        date = f"after:{since}"
        handoff_sources = words(config.get("brand_team_emails", "") + "," + config.get("forwarding_mailbox", ""))
        queries = [
            "in:anywhere " + date + " -in:sent {" + brand + " " + subject_terms + "}",
            f'in:anywhere {date} {{' + " ".join(f'from:{address}' for address in handoff_sources) + "}",
            f'in:anywhere {date} to:{config.get("forwarding_mailbox", "").strip()}',
        ]
        candidates = set().union(*(list_thread_ids(gmail, query) for query in queries))
        monitor_state = state_store.load("needs_review_monitor", {})
        removed_from_review = set(monitor_state.get("removed_thread_ids", []))
        count = {"scanned": 0, "labeled": 0, "eligible": 0}
        scan_start_ms = config_start * 1000
        for thread_id in sorted(candidates):
            if thread_id in removed_from_review:
                continue
            thread = call(gmail.threads().get(
                userId="me", id=thread_id, format="metadata",
                metadataHeaders=["From", "To", "Cc", "Subject", "In-Reply-To", "Auto-Submitted", "List-Unsubscribe"],
            ))
            count["scanned"] += 1
            eligible, reason = creator_inquiry(thread, config, active_labels, review_label, scan_start_ms)
            if eligible:
                count["eligible"] += 1
                if args.dry_run:
                    print("WOULD_LABEL", thread_id, reason)
                else:
                    call(gmail.threads().modify(userId="me", id=thread_id, body={"addLabelIds": [review_label]}))
                    count["labeled"] += 1
            time.sleep(float(os.getenv("GMAIL_THREAD_INTERVAL_SECONDS", "2.5")))

        if not args.dry_run:
            state_store.save("needs_review_scan", {"config_start": config_start,
                                                   "last_successful_scan_started": scan_started})
        print(count)


if __name__ == "__main__":
    main()
