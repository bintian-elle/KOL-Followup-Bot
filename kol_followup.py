"""KOL first-reply -> Google Sheets -> round-robin owner -> Slack.

Run periodically from cron/systemd.  One process at a time is expected.
"""

from __future__ import annotations

import argparse
import base64
import calendar
from contextlib import contextmanager
import fcntl
import html
import json
import logging
import os
import re
import signal
import threading
import time as time_module
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


LOG = logging.getLogger("kol_followup")
PROJECT_DIR = Path(__file__).resolve().parent
GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
HEADERS = [
    "processed_at",
    "reply_message_id",
    "gmail_thread_id",
    "kol_name",
    "kol_email",
    "subject",
    "reply_date",
    "owner",
    "slack_user_id",
    "slack_status",
]
QUEUE_HEADERS = [
    "Assignment ID", "Reply Received At", "Campaign / Context",
    "KOL / Creator Name", "Email", "Primary Platform", "Primary Account",
    "Assigned Owner", "Assignment Status", "Assigned At",
    "Reply Classification", "Review Reason", "Upfluence Reply ID",
    "Upfluence Thread ID", "Upfluence KOL ID", "Campaign ID", "Dedup Key",
    "Previous Owner", "Reassigned At", "Reassignment Reason",
    "Slack Destination", "Slack Message TS", "Notification Status",
    "Retry Count", "Last Error", "Source", "Agency", "Agency Contact",
    "Gmail Message ID", "Gmail Thread ID", "Gmail Thread Link",
]
AUDIT_HEADERS = [
    "Timestamp", "Event", "Assignment ID", "Source", "Source Thread ID",
    "KOL / Creator Name", "Old Owner", "New Owner", "Actor", "Reason",
    "Result", "Details",
]


@dataclass(frozen=True)
class Owner:
    name: str
    slack_user_id: str
    slack_channel_id: str = ""


@dataclass(frozen=True)
class Reply:
    message_id: str
    thread_id: str
    kol_name: str
    kol_email: str
    subject: str
    reply_date: str
    outreach_message_id: str = ""
    body_text: str = ""
    is_spam: bool = False
    threading_status: str = "verified_first_email"


@dataclass(frozen=True)
class PlannedItem:
    reply: Reply
    assigned: bool
    classification: str
    reason: str
    owner: Optional[Owner]
    assignment_id: str
    event: str
    previous_owner: str = ""


def env_required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def project_path(value: str) -> str:
    """Resolve relative configuration paths from the project, not the process cwd."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_DIR / path
    return str(path.resolve())


def load_owners(raw: str) -> List[Owner]:
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("KOL_OWNERS_JSON must be valid JSON") from exc
    if not isinstance(values, list) or len(values) != 3:
        raise ValueError("KOL_OWNERS_JSON must contain exactly 3 owners")
    owners = [
        Owner(
            name=str(item.get("name", "")).strip(),
            slack_user_id=str(item.get("slack_user_id", "")).strip(),
            slack_channel_id=str(item.get("slack_channel_id", "")).strip(),
        )
        for item in values
    ]
    if any(not owner.name or not owner.slack_user_id for owner in owners):
        raise ValueError("Every owner needs name and slack_user_id")
    if len({owner.name for owner in owners}) != 3:
        raise ValueError("Owner names must be unique")
    return owners


def header_map(message: Mapping[str, Any]) -> Dict[str, str]:
    return {
        h.get("name", "").lower(): h.get("value", "")
        for h in message.get("payload", {}).get("headers", [])
    }


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: List[str] = []
        self.hidden_depth = 0

    def handle_starttag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
        if tag in {"script", "style", "head"}:
            self.hidden_depth += 1
        if tag == "a":
            href = dict(attrs).get("href")
            if href and href.startswith(("http://", "https://")):
                self.parts.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "head"} and self.hidden_depth:
            self.hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self.hidden_depth and data.strip():
            self.parts.append(data.strip())


def message_text(message: Mapping[str, Any]) -> str:
    """Extract plain text plus safe HTML text/links; attachments stay ignored."""
    plain_parts: List[str] = []
    html_parts: List[str] = []

    def visit(part: Mapping[str, Any]) -> None:
        mime = part.get("mimeType", "")
        body = part.get("body", {})
        data = body.get("data")
        if mime in {"text/plain", "text/html"} and data and not body.get("attachmentId"):
            try:
                decoded = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
                if mime == "text/plain":
                    plain_parts.append(decoded)
                else:
                    parser = _HTMLTextExtractor()
                    parser.feed(decoded)
                    html_parts.append("\n".join(parser.parts))
            except (ValueError, TypeError):
                pass
        for child in part.get("parts", []) or []:
            visit(child)

    visit(message.get("payload", {}))
    # Plain text is usually cleaner; HTML adds links/details that plain parts omit.
    return "\n".join(plain_parts + html_parts)[:30000]


def addresses(value: str) -> List[tuple[str, str]]:
    return [(name, email.lower()) for name, email in getaddresses([value]) if email]


def timestamp(message: Mapping[str, Any]) -> int:
    try:
        return int(message.get("internalDate", 0))
    except (TypeError, ValueError):
        return 0


def iso_date(headers: Mapping[str, str], internal_ms: int) -> str:
    try:
        parsed = parsedate_to_datetime(headers.get("date", ""))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        return datetime.fromtimestamp(internal_ms / 1000, tz=timezone.utc).isoformat()


def message_id_tokens(value: str) -> List[str]:
    """Normalize RFC Message-ID values from Message-ID/References headers."""
    return [token.strip().lower() for token in re.findall(r"<[^<>]+>", value or "")]


def is_automated_message(headers: Mapping[str, str], sender_email: str) -> bool:
    subject = headers.get("subject", "").lower()
    auto_submitted = headers.get("auto-submitted", "").lower()
    precedence = headers.get("precedence", "").lower()
    sender = sender_email.lower()
    if auto_submitted and auto_submitted != "no":
        return True
    if precedence in {"bulk", "list", "junk"}:
        return True
    automated_senders = (
        "mailer-daemon", "postmaster", "notifications@upfluence",
        "mail.instagram.com", "facebookmail.com",
    )
    local_part = sender.split("@", 1)[0]
    if any(part in sender for part in automated_senders) or local_part in {"noreply", "no-reply", "notifications"}:
        return True
    automated_subjects = (
        "out of office", "automatic reply", "auto reply", "autoreply",
        "undeliverable", "delivery status notification", "delivery failure",
    )
    return any(marker in subject for marker in automated_subjects)


def has_profile_and_audience_evidence(reply: Reply) -> bool:
    """Require both a social-profile URL and an explicit audience size."""
    content = f"{reply.subject}\n{reply.body_text}"
    profile_url = re.search(
        r"https?://[^\s<>\"']*(?:instagram\.com|tiktok\.com|youtube\.com|youtu\.be|"
        r"facebook\.com|x\.com|twitter\.com|twitch\.tv)/[^\s<>\"']+",
        content,
        re.IGNORECASE,
    )
    audience = re.search(
        r"(?:\b\d[\d,.]*\s*[kmb]?\s*(?:followers?|subscribers?|fans?)\b|"
        r"(?:followers?|subscribers?|fans?)\s*[:=-]?\s*\d[\d,.]*\s*[kmb]?)",
        content,
        re.IGNORECASE,
    )
    return bool(profile_url and audience)


def _reply_from_message(
    message: Mapping[str, Any], thread_id: str, outreach_message_id: str,
    threading_status: str,
) -> Reply:
    headers = header_map(message)
    name, email = addresses(headers.get("from", ""))[0]
    return Reply(
        message_id=str(message["id"]), thread_id=thread_id,
        kol_name=name, kol_email=email, subject=headers.get("subject", ""),
        reply_date=iso_date(headers, timestamp(message)),
        outreach_message_id=outreach_message_id,
        body_text=message_text(message),
        is_spam="SPAM" in message.get("labelIds", []),
        threading_status=threading_status,
    )


def eligible_gmail_events(
    thread: Mapping[str, Any], mailbox: str, first_email_only: bool = True
) -> List[Reply]:
    """Return the initial qualifying event plus later reply-after-team events."""
    mailbox = mailbox.lower()
    messages = sorted(thread.get("messages", []), key=timestamp)
    external: Dict[int, tuple[str, str]] = {}
    sent_indexes: List[int] = []
    for index, message in enumerate(messages):
        headers = header_map(message)
        if "SENT" in message.get("labelIds", []):
            sent_indexes.append(index)
            continue
        senders = addresses(headers.get("from", ""))
        if not senders:
            continue
        name, email = senders[0]
        if email == mailbox or is_automated_message(headers, email):
            continue
        external[index] = (name, email)
    if not external:
        return []

    first_external_index = min(external)
    first_sent_index = sent_indexes[0] if sent_indexes else None
    events: List[Reply] = []
    base_index: Optional[int] = None
    outreach_id = ""

    if first_sent_index is None or first_external_index < first_sent_index:
        base_index = first_external_index
        events.append(_reply_from_message(
            messages[base_index], str(thread["id"]), "", "inbound_initiated"
        ))
    else:
        initial = messages[first_sent_index]
        outreach_id = str(initial["id"])
        initial_ids = message_id_tokens(header_map(initial).get("message-id", ""))
        initial_rfc_id = initial_ids[0] if initial_ids else ""
        for index in sorted(i for i in external if i > first_sent_index):
            candidate = messages[index]
            parent_ids = message_id_tokens(header_map(candidate).get("in-reply-to", ""))
            if not first_email_only:
                status = "verified_first_email" if initial_rfc_id and parent_ids == [initial_rfc_id] else "unrestricted_reply"
                base_index = index
            elif not initial_rfc_id or not parent_ids:
                status = "missing_threading_headers"
                base_index = index
            elif parent_ids == [initial_rfc_id]:
                status = "verified_first_email"
                base_index = index
            else:
                # A reply to a campaign follow-up is not the first-email event.
                continue
            events.append(_reply_from_message(
                candidate, str(thread["id"]), outreach_id, status
            ))
            break

    if base_index is None:
        return events

    # After the initial event, each team SENT message can arm one reactivation.
    waiting_for_external = False
    for index in range(base_index + 1, len(messages)):
        message = messages[index]
        if "SENT" in message.get("labelIds", []):
            waiting_for_external = True
            continue
        if index in external and waiting_for_external:
            events.append(_reply_from_message(
                message, str(thread["id"]), outreach_id, "reactivated"
            ))
            waiting_for_external = False
    return events


def first_external_reply(
    thread: Mapping[str, Any], mailbox: str, first_email_only: bool = True
) -> Optional[Reply]:
    """Return the first human reply directly tied to the first sent outreach.

    With first_email_only enabled, In-Reply-To must identify the RFC Message-ID
    of the thread's earliest SENT message. References alone are insufficient:
    a reply to a later follow-up normally references the initial message too.
    """
    events = eligible_gmail_events(thread, mailbox, first_email_only)
    return events[0] if events and events[0].outreach_message_id else None


def first_eligible_gmail_message(
    thread: Mapping[str, Any], mailbox: str, first_email_only: bool = True
) -> Optional[Reply]:
    """Apply the Sheet's source-specific entry rule.

    An externally initiated Gmail conversation is eligible for later Bluevua
    relevance classification without an outbound anchor. If our message starts
    the conversation, treat it as outreach and apply strict first-email reply
    validation.
    """
    events = eligible_gmail_events(thread, mailbox, first_email_only)
    return events[0] if events else None


def next_owner(owners: Sequence[Owner], rows: Sequence[Sequence[str]]) -> Owner:
    """Continue after the last recognized owner; start at owners[0]."""
    owner_col = HEADERS.index("owner")
    names = [owner.name for owner in owners]
    for row in reversed(rows):
        if len(row) > owner_col and row[owner_col] in names:
            return owners[(names.index(row[owner_col]) + 1) % len(owners)]
    return owners[0]


class GmailReader:
    def __init__(self) -> None:
        creds = Credentials(
            token=None,
            refresh_token=env_required("GMAIL_REFRESH_TOKEN"),
            token_uri="https://oauth2.googleapis.com/token",
            client_id=env_required("GMAIL_CLIENT_ID"),
            client_secret=env_required("GMAIL_CLIENT_SECRET"),
            scopes=[GMAIL_SCOPE],
        )
        self.api = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self.mailbox = self._execute(self.api.users().getProfile(userId="me"))["emailAddress"]

    @staticmethod
    def _execute(request: Any) -> Mapping[str, Any]:
        delays = (1, 2, 4, 8, 16)
        for attempt in range(len(delays) + 1):
            try:
                return request.execute()
            except HttpError as exc:
                content = exc.content.decode("utf-8", "replace") if isinstance(exc.content, bytes) else str(exc.content)
                if exc.resp.status != 403 or "rateLimitExceeded" not in content or attempt == len(delays):
                    raise
                delay = delays[attempt]
                LOG.warning("Gmail rate limit reached; retrying in %d second(s)", delay)
                time_module.sleep(delay)
        raise RuntimeError("Gmail request retry loop ended unexpectedly")

    def replies(self, query: str, first_email_only: bool = True) -> Iterable[Reply]:
        page_token: Optional[str] = None
        seen_threads = set()
        interval = max(0.0, float(os.getenv("GMAIL_THREAD_INTERVAL_SECONDS", "2.5")))
        while True:
            result = self._execute(self.api.users().messages().list(
                userId="me", q=query, maxResults=100, pageToken=page_token
            ))
            for item in result.get("messages", []):
                thread_id = item["threadId"]
                if thread_id in seen_threads:
                    continue
                seen_threads.add(thread_id)
                thread = self._execute(self.api.users().threads().get(
                    userId="me", id=thread_id, format="full"
                ))
                for reply in eligible_gmail_events(thread, self.mailbox, first_email_only):
                    yield reply
                if interval:
                    time_module.sleep(interval)
            page_token = result.get("nextPageToken")
            if not page_token:
                break


class SheetStore:
    def __init__(self, sheet_id: str, tab: str, credential_file: str) -> None:
        creds = service_account.Credentials.from_service_account_file(
            project_path(credential_file), scopes=[SHEETS_SCOPE]
        )
        self.api = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self.sheet_id = sheet_id
        self.tab = tab

    @property
    def range_all(self) -> str:
        return f"'{self.escaped_tab}'!A:J"

    @property
    def escaped_tab(self) -> str:
        return self.tab.replace("'", "''")

    def rows(self) -> List[List[str]]:
        result = self.api.spreadsheets().values().get(
            spreadsheetId=self.sheet_id, range=self.range_all
        ).execute()
        values = result.get("values", [])
        if not values:
            self.api.spreadsheets().values().update(
                spreadsheetId=self.sheet_id,
                range=f"'{self.escaped_tab}'!A1:J1",
                valueInputOption="RAW",
                body={"values": [HEADERS]},
            ).execute()
            return []
        if values[0] != HEADERS:
            raise ValueError(f"Sheet header must exactly match: {HEADERS}")
        return values[1:]

    def append(self, values: Sequence[str]) -> int:
        result = self.api.spreadsheets().values().append(
            spreadsheetId=self.sheet_id,
            range=self.range_all,
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [list(values)]},
        ).execute()
        updated_range = result["updates"]["updatedRange"]
        match = re.search(r"![A-Z]+(\d+):", updated_range)
        if not match:
            raise RuntimeError(f"Could not parse appended row: {updated_range}")
        return int(match.group(1))

    def set_slack_status(self, row_number: int, status: str) -> None:
        self.api.spreadsheets().values().update(
            spreadsheetId=self.sheet_id,
            range=f"'{self.escaped_tab}'!J{row_number}",
            valueInputOption="RAW",
            body={"values": [[status]]},
        ).execute()


class SlackNotifier:
    def __init__(
        self, bot_token: str = "", webhook_url: str = "",
        override_user_id: str = "",
        testing_channel_id: str = "",
    ) -> None:
        if not bot_token and not webhook_url:
            raise ValueError("Set SLACK_BOT_TOKEN or SLACK_WEBHOOK_URL")
        self.bot_token = bot_token
        self.webhook_url = webhook_url
        self.override_user_id = override_user_id
        self.testing_channel_id = testing_channel_id
        if testing_channel_id and not bot_token:
            raise ValueError("Testing channel mirroring requires SLACK_BOT_TOKEN")

    def _mirror(self, primary_channel: str, text: str, key: str) -> None:
        if not self.testing_channel_id or self.testing_channel_id == primary_channel:
            return
        payload = {"channel": self.testing_channel_id, "text": text, "unfurl_links": False}
        if key:
            payload["client_msg_id"] = self._client_msg_id(f"{key}:mirror:{self.testing_channel_id}")
        response = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {self.bot_token}"},
            json=payload, timeout=20,
        )
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(f"Slack testing channel mirror error: {result.get('error', 'unknown')}")

    def _dm_channel(self, slack_user_id: str) -> str:
        opened = requests.post(
            "https://slack.com/api/conversations.open",
            headers={"Authorization": f"Bearer {self.bot_token}"},
            json={"users": slack_user_id}, timeout=20,
        )
        opened.raise_for_status()
        payload = opened.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Slack conversations.open error: {payload.get('error', 'unknown')}")
        channel = payload.get("channel", {}).get("id", "")
        if not channel:
            raise RuntimeError("Slack conversations.open returned no DM channel")
        return channel

    @staticmethod
    def _client_msg_id(key: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"kol-followup:{key}"))

    def send(self, owner: Owner, reply: Reply, idempotency_key: str = "") -> str:
        spam_warning = "\n:warning: 此邮件位于 Gmail Spam，请先核查发件人和链接。" if reply.is_spam else ""
        gmail_folder = "spam" if reply.is_spam else "all"
        text = (
            f"<@{owner.slack_user_id}> 新的 KOL 邮件回复任务\n"
            f"KOL: {reply.kol_name or 'Unknown'} <{reply.kol_email}>\n"
            f"Subject: {html.escape(reply.subject)}\n"
            f"Gmail: https://mail.google.com/mail/u/0/#{gmail_folder}/{reply.message_id}"
            f"{spam_warning}"
        )
        if self.bot_token:
            channel = ""
            if self.testing_channel_id:
                channel = self.testing_channel_id
            elif self.override_user_id:
                channel = self._dm_channel(self.override_user_id)
            elif owner.slack_channel_id:
                channel = owner.slack_channel_id
            if not channel:
                channel = self._dm_channel(owner.slack_user_id)
            payload = {"channel": channel, "text": text, "unfurl_links": False}
            if idempotency_key:
                payload["client_msg_id"] = self._client_msg_id(idempotency_key)
            response = requests.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {self.bot_token}"},
                json=payload,
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json()
            if not payload.get("ok"):
                raise RuntimeError(f"Slack API error: {payload.get('error', 'unknown')}")
            self._mirror(channel, text, idempotency_key)
            return str(payload.get("ts", ""))
        response = requests.post(self.webhook_url, json={"text": text}, timeout=20)
        response.raise_for_status()
        return ""

    def send_digest(
        self, destination: str, text: str, idempotency_key: str, direct_message: bool
    ) -> str:
        if not self.bot_token:
            response = requests.post(self.webhook_url, json={"text": text}, timeout=20)
            response.raise_for_status()
            return ""
        if self.testing_channel_id:
            channel = self.testing_channel_id
        elif self.override_user_id:
            channel = self._dm_channel(self.override_user_id)
        else:
            channel = self._dm_channel(destination) if direct_message else destination
        response = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {self.bot_token}"},
            json={
                "channel": channel, "text": text, "unfurl_links": False,
                "client_msg_id": self._client_msg_id(idempotency_key),
            }, timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(f"Slack API error: {payload.get('error', 'unknown')}")
        self._mirror(channel, text, idempotency_key)
        return str(payload.get("ts", ""))


def row_values(reply: Reply, owner: Owner, status: str) -> List[str]:
    return [
        datetime.now(timezone.utc).isoformat(),
        reply.message_id,
        reply.thread_id,
        reply.kol_name,
        reply.kol_email,
        reply.subject,
        reply.reply_date,
        owner.name,
        owner.slack_user_id,
        status,
    ]


def retry_unsent(rows: Sequence[Sequence[str]], owners: Sequence[Owner], store: SheetStore,
                 notifier: SlackNotifier) -> int:
    """Retry rows that were durably recorded before a Slack failure."""
    by_name = {owner.name: owner for owner in owners}
    sent = 0
    for row_number, row in enumerate(rows, start=2):
        padded = list(row) + [""] * (len(HEADERS) - len(row))
        status = padded[HEADERS.index("slack_status")]
        if status == "SENT" or not (status == "PENDING" or status.startswith("ERROR:")):
            continue
        owner = by_name.get(padded[HEADERS.index("owner")])
        if not owner:
            LOG.error("Cannot retry row %d: owner is not in KOL_OWNERS_JSON", row_number)
            continue
        reply = Reply(
            message_id=padded[HEADERS.index("reply_message_id")],
            thread_id=padded[HEADERS.index("gmail_thread_id")],
            kol_name=padded[HEADERS.index("kol_name")],
            kol_email=padded[HEADERS.index("kol_email")],
            subject=padded[HEADERS.index("subject")],
            reply_date=padded[HEADERS.index("reply_date")],
        )
        try:
            notifier.send(owner, reply)
        except Exception as exc:
            store.set_slack_status(row_number, f"ERROR: {str(exc)[:160]}")
            LOG.exception("Slack retry failed for sheet row %d", row_number)
            continue
        store.set_slack_status(row_number, "SENT")
        row_list = rows[row_number - 2]
        if isinstance(row_list, list):
            row_list.extend([""] * (len(HEADERS) - len(row_list)))
            row_list[HEADERS.index("slack_status")] = "SENT"
        sent += 1
        LOG.info("Slack retry succeeded for sheet row %d", row_number)
    return sent


def run_legacy(dry_run: bool) -> int:
    query = env_required("GMAIL_UPFLUENCE_QUERY")
    owners = load_owners(env_required("KOL_OWNERS_JSON"))
    gmail = GmailReader()
    replies = sorted(gmail.replies(query), key=lambda item: item.reply_date)
    LOG.info("Found %d Upfluence thread(s) with an external reply", len(replies))

    if dry_run:
        for reply in replies:
            LOG.info("DRY RUN reply=%s <%s> subject=%r", reply.kol_name, reply.kol_email, reply.subject)
        return len(replies)

    store = SheetStore(
        env_required("GOOGLE_SHEET_ID"),
        os.getenv("GOOGLE_SHEET_TAB", "KOL Follow-up"),
        env_required("GOOGLE_SERVICE_ACCOUNT_FILE"),
    )
    notifier = SlackNotifier(os.getenv("SLACK_BOT_TOKEN", ""), os.getenv("SLACK_WEBHOOK_URL", ""))
    rows = store.rows()
    retried = retry_unsent(rows, owners, store, notifier)
    id_col = HEADERS.index("reply_message_id")
    processed = {row[id_col] for row in rows if len(row) > id_col}
    created = 0
    for reply in replies:
        if reply.message_id in processed:
            continue
        owner = next_owner(owners, rows)
        row_number = store.append(row_values(reply, owner, "PENDING"))
        rows.append(row_values(reply, owner, "PENDING"))
        processed.add(reply.message_id)
        try:
            notifier.send(owner, reply)
        except Exception as exc:
            store.set_slack_status(row_number, f"ERROR: {str(exc)[:160]}")
            LOG.exception("Sheet row %d saved, but Slack failed", row_number)
            continue
        store.set_slack_status(row_number, "SENT")
        created += 1
        LOG.info("Assigned %s to %s (sheet row %d)", reply.kol_email, owner.name, row_number)
    return created + retried


class OperationalSheetStore:
    """Adapter for the existing three-tab operational workbook."""

    def __init__(self, sheet_id: str, credential_file: str) -> None:
        creds = service_account.Credentials.from_service_account_file(
            project_path(credential_file), scopes=[SHEETS_SCOPE]
        )
        self.api = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self.sheet_id = sheet_id
        self._last_write_at = 0.0

    def _write(self, request: Any) -> Mapping[str, Any]:
        """Execute a Sheets write below the per-user quota, retrying throttles."""
        delays = (10, 20, 40, 60)
        for attempt in range(len(delays) + 1):
            elapsed = time_module.monotonic() - self._last_write_at
            if elapsed < 1.25:
                time_module.sleep(1.25 - elapsed)
            try:
                result = request.execute()
                self._last_write_at = time_module.monotonic()
                return result
            except HttpError as exc:
                content = str(exc)
                throttled = exc.resp.status == 429 or (
                    exc.resp.status == 403 and "rateLimitExceeded" in content
                )
                if not throttled or attempt == len(delays):
                    raise
                delay = delays[attempt]
                LOG.warning("Google Sheets write quota reached; retrying in %ds", delay)
                time_module.sleep(delay)
        raise RuntimeError("Google Sheets write retry loop exited unexpectedly")

    def values(self, range_name: str) -> List[List[str]]:
        return self.api.spreadsheets().values().get(
            spreadsheetId=self.sheet_id, range=range_name
        ).execute().get("values", [])

    def config(self) -> tuple[List[Owner], Dict[str, str], Dict[str, int]]:
        rows = self.values("'KOL Followup Config'!A1:F100")
        owners: List[tuple[int, Owner]] = []
        settings: Dict[str, str] = {}
        setting_rows: Dict[str, int] = {}
        for number, row in enumerate(rows, start=1):
            padded = row + [""] * (6 - len(row))
            if padded[0].strip().lower() == "testing channel":
                settings["testing_channel_active"] = padded[2].strip()
                settings["testing_channel_id"] = padded[3].strip()
            if number >= 7 and padded[0] and padded[1].isdigit():
                if padded[2].strip().upper() == "TRUE":
                    owners.append((int(padded[1]), Owner(padded[0].strip(), padded[3].strip())))
            if number >= 14 and padded[0]:
                settings[padded[0].strip()] = padded[1].strip()
                setting_rows[padded[0].strip()] = number
        owners.sort(key=lambda item: item[0])
        result = [owner for _, owner in owners]
        if len(result) != 3 or any(not item.slack_user_id for item in result):
            raise ValueError("KOL Followup Config must contain 3 active owners with Slack IDs")
        return result, settings, setting_rows

    def queue_rows(self) -> List[List[str]]:
        rows = self.values("'KOL Followup Queue'!A5:AE")
        if not rows or rows[0] != QUEUE_HEADERS:
            raise ValueError("KOL Followup Queue row 5 does not match the required 31-column schema")
        return rows[1:]

    def audit_rows(self) -> List[List[str]]:
        rows = self.values("'KOL Followup Audit Log'!A5:L")
        if not rows or rows[0] != AUDIT_HEADERS:
            raise ValueError("KOL Followup Audit Log row 5 does not match the required schema")
        return rows[1:]

    def append(self, range_name: str, values: Sequence[str]) -> int:
        result = self._write(self.api.spreadsheets().values().append(
            spreadsheetId=self.sheet_id,
            range=range_name,
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": [list(values)]},
        ))
        updated_range = result.get("updates", {}).get("updatedRange", "")
        match = re.search(r"![A-Z]+(\d+):", updated_range)
        return int(match.group(1)) if match else 0

    def append_queue(self, values: Sequence[str]) -> int:
        return self.append("'KOL Followup Queue'!A:AE", values)

    def append_audit(self, values: Sequence[str]) -> None:
        self.append("'KOL Followup Audit Log'!A:L", values)

    def sort_newest_first(self) -> None:
        metadata = self.api.spreadsheets().get(
            spreadsheetId=self.sheet_id, fields="sheets(properties)"
        ).execute()
        requests_to_sort = []
        for sheet in metadata["sheets"]:
            props = sheet["properties"]
            title = props["title"]
            if title not in {"KOL Followup Queue", "KOL Followup Audit Log"}:
                continue
            rows = self.queue_rows() if title == "KOL Followup Queue" else self.audit_rows()
            date_col = 1 if title == "KOL Followup Queue" else 0
            def date_key(row):
                value = row[date_col] if len(row) > date_col else ""
                try:
                    return datetime.strptime(value, "%m/%d/%Y %H:%M")
                except ValueError:
                    return datetime.min
            ordered = sorted(range(len(rows)), key=lambda i: date_key(rows[i]), reverse=True)
            current = list(range(len(rows)))
            for dest, original in enumerate(ordered):
                source = current.index(original)
                if source != dest:
                    requests_to_sort.append({"moveDimension": {
                        "source": {"sheetId": props["sheetId"], "dimension": "ROWS",
                                   "startIndex": source + 5, "endIndex": source + 6},
                        "destinationIndex": dest + 5,
                    }})
                    current.insert(dest, current.pop(source))
            if rows:
                requests_to_sort.append({"updateBorders": {
                    "range": {"sheetId": props["sheetId"], "startRowIndex": 5,
                              "endRowIndex": 5 + len(rows), "startColumnIndex": 0,
                              "endColumnIndex": 31 if date_col == 1 else 12},
                    "top": {"style": "NONE"}, "bottom": {"style": "NONE"},
                    "innerHorizontal": {"style": "NONE"},
                }})
            prior_day = None
            for dest, original in enumerate(ordered):
                day = date_key(rows[original]).date()
                if prior_day is not None and day != prior_day:
                    requests_to_sort.append({"updateBorders": {
                        "range": {"sheetId": props["sheetId"], "startRowIndex": dest + 5,
                                  "endRowIndex": dest + 6, "startColumnIndex": 0,
                                  "endColumnIndex": 31 if date_col == 1 else 12},
                        "top": {"style": "SOLID_MEDIUM", "color": {
                            "red": 0.12, "green": 0.55, "blue": 0.72}},
                    }})
                prior_day = day
        self._write(self.api.spreadsheets().batchUpdate(
            spreadsheetId=self.sheet_id, body={"requests": requests_to_sort}
        ))

    def update_notification(
        self, row_number: int, message_ts: str, status: str, retry_count: str, error: str
    ) -> None:
        if not row_number:
            raise RuntimeError("Could not determine appended Queue row number")
        self._write(self.api.spreadsheets().values().update(
            spreadsheetId=self.sheet_id,
            range=f"'KOL Followup Queue'!V{row_number}:Y{row_number}",
            valueInputOption="RAW",
            body={"values": [[message_ts, status, retry_count, error]]},
        ))

    def update_setting(self, setting_rows: Mapping[str, int], name: str, value: str) -> None:
        if name not in setting_rows:
            raise ValueError(f"Missing Config setting: {name}")
        self._write(self.api.spreadsheets().values().update(
            spreadsheetId=self.sheet_id,
            range=f"'KOL Followup Config'!B{setting_rows[name]}",
            valueInputOption="RAW",
            body={"values": [[value]]},
        ))


def config_bool(settings: Mapping[str, str], name: str, default: bool = False) -> bool:
    value = settings.get(name)
    return default if value is None else value.strip().upper() == "TRUE"


def config_list(settings: Mapping[str, str], name: str, defaults: Sequence[str]) -> List[str]:
    raw = settings.get(name, "").strip()
    if not raw:
        return [item.lower() for item in defaults]
    return [item.strip().lower() for item in re.split(r"[,\n]", raw) if item.strip()]


def classify_reply(reply: Reply, settings: Mapping[str, str]) -> tuple[bool, str, str]:
    """Return assigned?, classification, review reason from Config rules."""
    subject = reply.subject.lower()
    content = f"{reply.subject}\n{reply.body_text}".lower()
    brand = settings.get("campaign_name_contains", "Bluevua").lower()
    closure_markers = config_list(settings, "completed_collab_keywords", (
        "invoice", "payment request", "payment confirmation", "w-9", "w9",
        "tax form", "collaboration completed", "future collaboration",
    ))
    if (
        any(item in subject for item in closure_markers)
        and settings.get("gmail_completed_collab_action", "Ignore").lower() == "ignore"
    ):
        return False, "", "Collaboration completed"
    if reply.is_spam and has_profile_and_audience_evidence(reply):
        return True, "Needs Review", "Spam: profile link and follower scale; manual verification required"
    if reply.threading_status == "missing_threading_headers":
        if settings.get("missing_thread_headers_action", "Needs Review").lower() == "ignore":
            return False, "", "Missing Message-ID/In-Reply-To"
        return True, "Needs Review", "Missing Message-ID/In-Reply-To; manual threading verification required"
    if brand and brand in content:
        if reply.is_spam:
            reason = "Spam: manual verification required"
        elif reply.threading_status == "reactivated":
            reason = "New external reply after prior internal answer"
        else:
            reason = ""
        return True, "Human Reply", reason

    other_brands = config_list(settings, "other_brand_keywords", (
        "biceek", "ringconn", "memomind", "furrytail", "senson", "sleepal",
    ))
    if any(item in subject for item in other_brands):
        return False, "", "Other brand"

    if reply.threading_status == "reactivated":
        reason = "Spam: manual verification required" if reply.is_spam else "New external reply after prior internal answer"
        return True, "Human Reply", reason

    if not reply.outreach_message_id:
        if "ugc" in content and settings.get("gmail_ugc_outreach_action", "Ignore").lower() == "ignore":
            return False, "", "UGC creator outreach"
        plausible = config_list(settings, "generic_collab_keywords", (
            "creator", "influencer", "collab", "partnership", "agency", "talent management",
        ))
        if any(item in subject for item in plausible):
            return True, "Needs Review", "No explicit Bluevua reference"
    return False, "", "Unrelated"


def next_config_owner(owners: Sequence[Owner], last_owner: str) -> Owner:
    names = [owner.name for owner in owners]
    if last_owner in names:
        return owners[(names.index(last_owner) + 1) % len(owners)]
    return owners[0]


def next_assignment_id(rows: Sequence[Sequence[str]], assigned: bool, now: datetime) -> str:
    prefix = "BV" if assigned else "IGN"
    date_part = now.strftime("%Y%m%d")
    pattern = re.compile(rf"^{prefix}-{date_part}-(\d+)$")
    highest = 0
    for row in rows:
        if row:
            match = pattern.match(row[0])
            if match:
                highest = max(highest, int(match.group(1)))
    return f"{prefix}-{date_part}-{highest + 1:04d}"


def reply_after_cutoff(reply: Reply, settings: Mapping[str, str], tz: ZoneInfo) -> bool:
    try:
        cutoff = datetime.strptime(settings.get("reply_cutoff_date", "08/20/2026"), "%m/%d/%Y")
        cutoff = cutoff.replace(tzinfo=tz)
        received = datetime.fromisoformat(reply.reply_date)
        if received.tzinfo is None:
            received = received.replace(tzinfo=timezone.utc)
        return received.astimezone(tz) >= cutoff
    except ValueError:
        raise ValueError("reply_cutoff_date must use MM/DD/YYYY")


def operational_row(
    assignment_id: str, reply: Reply, owner: Optional[Owner], assigned: bool,
    classification: str, reason: str, now: datetime, mailbox: str,
    event: str = "ASSIGNED", previous_owner: str = "",
) -> List[str]:
    formatted_now = now.strftime("%m/%d/%Y %H:%M")
    try:
        received = datetime.fromisoformat(reply.reply_date).astimezone(now.tzinfo).strftime("%m/%d/%Y %H:%M")
    except ValueError:
        received = reply.reply_date
    values = {header: "" for header in QUEUE_HEADERS}
    values.update({
        "Assignment ID": assignment_id,
        "Reply Received At": received,
        "Campaign / Context": reply.subject or "Gmail conversation",
        "KOL / Creator Name": reply.kol_name,
        "Email": reply.kol_email,
        "Assigned Owner": owner.name if owner else "",
        "Assignment Status": "Reactivated" if event == "REACTIVATED" else ("Assigned" if assigned else "Ignored"),
        "Assigned At": formatted_now if assigned else "",
        "Reply Classification": classification,
        "Review Reason": reason,
        "Dedup Key": f"gmail_message:{reply.message_id}",
        "Previous Owner": previous_owner,
        "Reassigned At": formatted_now if event == "REACTIVATED" else "",
        "Reassignment Reason": "New external reply after prior internal answer" if event == "REACTIVATED" else "",
        "Slack Destination": owner.slack_user_id if owner else "",
        "Notification Status": "PENDING" if assigned else "",
        "Retry Count": "0",
        "Source": "Gmail",
        "Agency Contact": reply.kol_email,
        "Gmail Message ID": reply.message_id,
        "Gmail Thread ID": reply.thread_id,
        "Gmail Thread Link": (
            "https://mail.google.com/mail/u/?authuser="
            f"{mailbox.replace('@', '%40')}#{'spam' if reply.is_spam else 'all'}/{reply.message_id}"
        ),
    })
    return [values[header] for header in QUEUE_HEADERS]


def operational_audit(
    now: datetime, event: str, assignment_id: str, reply: Reply,
    owner: Optional[Owner], reason: str, details: str, result: str = "Success",
    previous_owner: str = "",
) -> List[str]:
    return [
        now.strftime("%m/%d/%Y %H:%M"), event, assignment_id, "Gmail",
        f"gmail_thread:{reply.thread_id}", reply.kol_name, previous_owner,
        owner.name if owner else "", "KOL Follow-up Automation", reason,
        result, f"{details}; gmail_message:{reply.message_id}",
    ]


def gmail_query(settings: Mapping[str, str]) -> str:
    explicit = os.getenv("GMAIL_UPFLUENCE_QUERY", "").strip()
    if explicit:
        return explicit
    checkpoint = settings.get("last_gmail_successful_run_at", "").strip()
    tz = ZoneInfo(settings.get("timezone", "America/Los_Angeles"))
    try:
        since = datetime.strptime(checkpoint, "%m/%d/%Y %H:%M").replace(tzinfo=tz)
        # Small overlap is intentional; the Queue dedup key makes it safe.
        return f"in:anywhere after:{int(since.timestamp()) - 300}"
    except ValueError:
        cutoff = settings.get("reply_cutoff_date", "08/20/2026")
        parsed = datetime.strptime(cutoff, "%m/%d/%Y")
        return f"in:anywhere after:{parsed.strftime('%Y/%m/%d')}"


def gmail_scan_start(settings: Mapping[str, str], tz: ZoneInfo) -> datetime:
    checkpoint = settings.get("last_gmail_successful_run_at", "").strip()
    try:
        return datetime.strptime(checkpoint, "%m/%d/%Y %H:%M").replace(tzinfo=tz) - timedelta(minutes=5)
    except ValueError:
        cutoff = datetime.strptime(settings.get("reply_cutoff_date", "08/20/2026"), "%m/%d/%Y")
        return cutoff.replace(tzinfo=tz)


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (occurrence - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year, month, calendar.monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def us_federal_holidays(year: int) -> set[date]:
    fixed = [date(year, 1, 1), date(year, 6, 19), date(year, 7, 4),
             date(year, 11, 11), date(year, 12, 25)]
    result = {_observed(day) for day in fixed}
    result.update({
        _nth_weekday(year, 1, 0, 3),   # MLK Day
        _nth_weekday(year, 2, 0, 3),   # Washington's Birthday
        _last_weekday(year, 5, 0),     # Memorial Day
        _nth_weekday(year, 9, 0, 1),   # Labor Day
        _nth_weekday(year, 10, 0, 2),  # Columbus Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
    })
    # New Year's observed can fall in the neighboring calendar year.
    result.add(_observed(date(year + 1, 1, 1)))
    return result


def is_business_day(day: date, settings: Mapping[str, str]) -> bool:
    if day.weekday() >= 5:
        return False
    if "federal" in settings.get("holiday_calendar", "").lower():
        holidays = us_federal_holidays(day.year) | us_federal_holidays(day.year - 1)
        if day in holidays:
            return False
    return True


def previous_business_day(day: date, settings: Mapping[str, str]) -> date:
    candidate = day - timedelta(days=1)
    while not is_business_day(candidate, settings):
        candidate -= timedelta(days=1)
    return candidate


def digest_window(now: datetime, settings: Mapping[str, str]) -> Optional[tuple[datetime, datetime]]:
    if not is_business_day(now.date(), settings):
        return None
    try:
        send_at = datetime.strptime(settings.get("daily_send_time", "09:00"), "%H:%M").time()
    except ValueError:
        raise ValueError("daily_send_time must use HH:MM")
    if now.time() < send_at:
        return None
    start_day = previous_business_day(now.date(), settings)
    return datetime.combine(start_day, time.min, now.tzinfo), datetime.combine(now.date(), time.min, now.tzinfo)


def _queue_datetime(value: str, tz: ZoneInfo) -> Optional[datetime]:
    try:
        return datetime.strptime(value, "%m/%d/%Y %H:%M").replace(tzinfo=tz)
    except ValueError:
        return None


def maybe_send_daily_digest(
    store: OperationalSheetStore, queue: Sequence[Sequence[str]], settings: Mapping[str, str],
    setting_rows: Mapping[str, int], notifier: SlackNotifier, now: datetime,
    force: bool = False,
) -> bool:
    window = digest_window(now, settings)
    if force and not window:
        start_day = previous_business_day(now.date(), settings)
        window = (
            datetime.combine(start_day, time.min, now.tzinfo),
            datetime.combine(now.date(), time.min, now.tzinfo),
        )
    if not window or (not force and settings.get("last_daily_digest_date") == now.date().isoformat()):
        return False
    start, end = window
    status_i = QUEUE_HEADERS.index("Assignment Status")
    assigned_i = QUEUE_HEADERS.index("Assigned At")
    owner_i = QUEUE_HEADERS.index("Assigned Owner")
    name_i = QUEUE_HEADERS.index("KOL / Creator Name")
    subject_i = QUEUE_HEADERS.index("Campaign / Context")
    id_i = QUEUE_HEADERS.index("Assignment ID")
    selected: List[List[str]] = []
    for raw in queue:
        row = list(raw) + [""] * (len(QUEUE_HEADERS) - len(raw))
        assigned_at = _queue_datetime(row[assigned_i], now.tzinfo)
        if row[status_i] in {"Assigned", "Reassigned", "Reactivated"} and assigned_at and start <= assigned_at < end:
            selected.append(row)

    # An explicit manual send must produce a visible digest even when the
    # scheduled policy normally suppresses empty summaries.
    skip_empty = config_bool(settings, "skip_empty_digest", True) and not force
    digest_key = (
        f"daily-digest-manual:{now.date().isoformat()}:{notifier.override_user_id}"
        if force else f"daily-digest:{now.date().isoformat()}"
    )
    digest_sent = False
    if selected or not skip_empty:
        lines = [
            f"KOL Follow-up Daily Digest ({start.strftime('%m/%d')}–{(end - timedelta(days=1)).strftime('%m/%d')})",
            f"Total assigned: {len(selected)}",
        ]
        for row in selected[:50]:
            lines.append(f"• {row[id_i]} | {row[owner_i]} | {row[name_i]} | {row[subject_i]}")
        if len(selected) > 50:
            lines.append(f"…and {len(selected) - 50} more")
        pilot = config_bool(settings, "pilot_mode", True)
        destination = settings.get("pilot_recipient_slack_id" if pilot else "production_channel_id", "")
        if not destination:
            raise ValueError("Daily digest Slack destination is missing in Config")
        notifier.send_digest(destination, "\n".join(lines), digest_key, direct_message=pilot)
        digest_sent = True
        result, details = "Success", f"Digest sent with {len(selected)} assignment(s)"
    else:
        result, details = "Success", "Empty digest skipped by configuration"
    store.append_audit([
        now.strftime("%m/%d/%Y %H:%M"),
        "DAILY_DIGEST_SENT" if digest_sent else "DAILY_DIGEST_SKIPPED",
        f"DIGEST-{now.strftime('%Y%m%d')}", "System", "", "", "", "",
        "KOL Follow-up Automation", "Daily digest", result, details,
    ])
    store.update_setting(setting_rows, "last_daily_digest_date", now.date().isoformat())
    return True


def run(dry_run: bool, force_digest: bool = False) -> int:
    store = OperationalSheetStore(
        env_required("GOOGLE_SHEET_ID"), env_required("GOOGLE_SERVICE_ACCOUNT_FILE")
    )
    owners, settings, setting_rows = store.config()
    queue = store.queue_rows()
    existing_queue = list(queue)
    audit = store.audit_rows()  # Validate before any possible write.
    tz = ZoneInfo(settings.get("timezone", "America/Los_Angeles"))
    now = datetime.now(tz)
    scan_start = gmail_scan_start(settings, tz)
    gmail = GmailReader()
    replies: List[Reply] = []
    if config_bool(settings, "gmail_scan_enabled", True):
        query = gmail_query(settings)
        replies = sorted(
            gmail.replies(query, config_bool(settings, "first_email_only", True)),
            key=lambda item: item.reply_date,
        )
    message_i = QUEUE_HEADERS.index("Gmail Message ID")
    thread_i = QUEUE_HEADERS.index("Gmail Thread ID")
    owner_i = QUEUE_HEADERS.index("Assigned Owner")
    status_i = QUEUE_HEADERS.index("Assignment Status")
    processed_messages = {row[message_i] for row in queue if len(row) > message_i and row[message_i]}
    for row in audit:
        if len(row) > 11:
            processed_messages.update(re.findall(r"gmail_message:([A-Za-z0-9_-]+)", row[11]))
    latest_by_thread: Dict[str, List[str]] = {}
    for raw in sorted(queue, key=lambda row: row[1] if len(row) > 1 else ""):
        row = list(raw) + [""] * (len(QUEUE_HEADERS) - len(raw))
        if row[thread_i]:
            latest_by_thread[row[thread_i]] = row
    last_owner = settings.get("last_round_robin_owner", "")
    active_owner_names = {owner.name for owner in owners}
    planned: List[PlannedItem] = []

    for reply in replies:
        try:
            received = datetime.fromisoformat(reply.reply_date)
            if received.tzinfo is None:
                received = received.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if (
            reply.message_id in processed_messages
            or not reply_after_cutoff(reply, settings, tz)
            or received.astimezone(tz) < scan_start
        ):
            continue
        assigned, classification, reason = classify_reply(reply, settings)
        prior = latest_by_thread.get(reply.thread_id)
        event = (
            "REACTIVATED"
            if assigned and reply.threading_status == "reactivated" and prior
            else ("ASSIGNED" if assigned else "IGNORED")
        )
        previous_owner = prior[owner_i] if prior else ""
        if (
            assigned and event == "REACTIVATED" and previous_owner in active_owner_names
            and config_bool(settings, "reactivation_keep_owner", True)
        ):
            owner = next(item for item in owners if item.name == previous_owner)
        else:
            owner = next_config_owner(owners, last_owner) if assigned else None
        if owner and event != "REACTIVATED":
            last_owner = owner.name
        assignment_id = next_assignment_id(
            queue + [[row[2]] for row in audit if len(row) > 2], assigned, now
        )
        new_row = operational_row(
            assignment_id, reply, owner, assigned, classification, reason, now,
            gmail.mailbox, event, previous_owner,
        )
        queue.append(new_row)
        latest_by_thread[reply.thread_id] = new_row
        processed_messages.add(reply.message_id)
        planned.append(PlannedItem(
            reply, assigned, classification, reason, owner, assignment_id, event, previous_owner
        ))

    if dry_run:
        for item in planned:
            LOG.info(
                "DRY RUN %s %s owner=%s classification=%s reason=%s subject=%r",
                item.assignment_id, item.event,
                item.owner.name if item.owner else "", item.classification,
                item.reason, item.reply.subject,
            )
        LOG.info("Dry run planned %d new Queue row(s)", len(planned))
        return len(planned)

    notification_col = QUEUE_HEADERS.index("Notification Status")
    assignment_status_col = QUEUE_HEADERS.index("Assignment Status")
    pending_existing = [
        row for row in existing_queue
        if len(row) > notification_col
        and row[assignment_status_col] in {"Assigned", "Reassigned", "Reactivated"}
        and row[notification_col] in {"PENDING", "Pending", "Failed"}
    ]
    digest_due = force_digest or (
        digest_window(now, settings) is not None
        and settings.get("last_daily_digest_date") != now.date().isoformat()
    )
    needs_slack = bool(pending_existing) or any(item.assigned for item in planned) or digest_due
    if needs_slack and not (
        os.getenv("SLACK_BOT_TOKEN", "") or os.getenv("SLACK_WEBHOOK_URL", "")
    ):
        raise ValueError("Slack credential is required before writing assigned Queue rows")
    notifier = (
        SlackNotifier(
            os.getenv("SLACK_BOT_TOKEN", ""),
            os.getenv("SLACK_WEBHOOK_URL", ""),
            "",  # Developer DM overrides are no longer used.
            settings.get("testing_channel_id", "")
            if config_bool(settings, "testing_channel_active") else "",
        )
        if needs_slack else None
    )

    owners_by_name = {owner.name: owner for owner in owners}
    for row_number, raw_row in enumerate(existing_queue, start=6):
        row = list(raw_row) + [""] * (len(QUEUE_HEADERS) - len(raw_row))
        if row[assignment_status_col] not in {"Assigned", "Reassigned", "Reactivated"}:
            continue
        if row[notification_col] not in {"PENDING", "Pending", "Failed"}:
            continue
        owner = owners_by_name.get(row[QUEUE_HEADERS.index("Assigned Owner")])
        if not owner:
            continue
        retry_reply = Reply(
            message_id=row[QUEUE_HEADERS.index("Gmail Message ID")],
            thread_id=row[QUEUE_HEADERS.index("Gmail Thread ID")],
            kol_name=row[QUEUE_HEADERS.index("KOL / Creator Name")],
            kol_email=row[QUEUE_HEADERS.index("Email")],
            subject=row[QUEUE_HEADERS.index("Campaign / Context")],
            reply_date=row[QUEUE_HEADERS.index("Reply Received At")],
        )
        retry_count_text = row[QUEUE_HEADERS.index("Retry Count")] or "0"
        try:
            retry_count = int(retry_count_text) + 1
        except ValueError:
            retry_count = 1
        try:
            assignment_id = row[QUEUE_HEADERS.index("Assignment ID")]
            message_ts = notifier.send(owner, retry_reply, f"owner-task:{assignment_id}") if notifier else ""
            store.update_notification(row_number, message_ts, "Sent", str(retry_count), "")
            result, details = "Success", "Owner Slack notification retry succeeded"
        except Exception as exc:
            details = str(exc)[:160]
            store.update_notification(row_number, "", "Failed", str(retry_count), details)
            result = "Failure"
        store.append_audit(operational_audit(
            now, "NOTIFICATION_RETRIED", row[QUEUE_HEADERS.index("Assignment ID")],
            retry_reply, owner, "Retry pending/failed owner notification", details, result,
        ))
    for item in planned:
        reply, assigned, classification, reason = item.reply, item.assigned, item.classification, item.reason
        owner, assignment_id = item.owner, item.assignment_id
        row = operational_row(
            assignment_id, reply, owner, assigned, classification, reason, now,
            gmail.mailbox, item.event, item.previous_owner,
        )
        row_number = store.append_queue(row) if assigned else 0
        audit_result = "Success"
        audit_details = classification or "Recorded for deduplication"
        if assigned and owner:
            try:
                message_ts = notifier.send(owner, reply, f"owner-task:{assignment_id}") if notifier else ""
                store.update_notification(row_number, message_ts, "Sent", "0", "")
            except Exception as exc:
                error = str(exc)[:160]
                store.update_notification(row_number, "", "Failed", "1", error)
                audit_result = "Failure"
                audit_details = error
        event = item.event
        store.append_audit(operational_audit(
            now, event, assignment_id, reply, owner,
            "New external reply after prior internal answer" if event == "REACTIVATED" else ("Unified round robin" if assigned else reason),
            audit_details, audit_result, item.previous_owner,
        ))
        audit.append(["", "", assignment_id])
    if planned:
        store.update_setting(setting_rows, "last_round_robin_owner", last_owner)
    store.update_setting(
        setting_rows, "last_gmail_successful_run_at", now.strftime("%m/%d/%Y %H:%M")
    )
    # Re-read Queue so the digest includes rows written during this run.
    if notifier:
        maybe_send_daily_digest(
            store, store.queue_rows(), settings, setting_rows, notifier, now,
            force=force_digest,
        )
    store.sort_newest_first()
    return len(planned)


@contextmanager
def single_instance_lock() -> Iterable[None]:
    lock_path = project_path(os.getenv("WORKER_LOCK_FILE", ".kol-followup.lock"))
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another KOL follow-up worker instance is already running") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def monitor(dry_run: bool, interval_seconds: int, stop: threading.Event) -> None:
    """Run sequential scans until shutdown, retaining the process lock outside."""
    while not stop.is_set():
        try:
            count = run(dry_run)
            LOG.info("Scan completed; %d new event(s)", count)
        except Exception:
            LOG.exception("Scan failed; will retry after %d seconds", interval_seconds)
        if not stop.is_set():
            LOG.info("Next scan in %d seconds", interval_seconds)
            stop.wait(interval_seconds)


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Never write Sheet or Slack")
    parser.add_argument("--monitor", action="store_true", help="Run continuously")
    parser.add_argument("--interval-seconds", type=int, default=300,
                        help="Wait between completed scans in monitor mode (default 300)")
    parser.add_argument(
        "--force-digest", action="store_true",
        help="Send today's daily digest even if it was already recorded as sent",
    )
    args = parser.parse_args()
    if args.interval_seconds <= 0:
        parser.error("--interval-seconds must be positive")
    if args.monitor and args.force_digest:
        parser.error("--force-digest is only supported for a single run")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    env_dry_run = os.getenv("DRY_RUN", "true").lower() in {"1", "true", "yes"}
    if args.monitor:
        stop = threading.Event()
        def shutdown(signum, frame):
            LOG.info("Shutdown requested; finish the current scan before exiting")
            stop.set()
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        with single_instance_lock():
            monitor(args.dry_run or env_dry_run, args.interval_seconds, stop)
        LOG.info("Monitor stopped")
        return
    with single_instance_lock():
        count = run(args.dry_run or env_dry_run, force_digest=args.force_digest)
    LOG.info("Done; %d item(s) %s", count, "inspected" if args.dry_run or env_dry_run else "notified")


if __name__ == "__main__":
    main()
