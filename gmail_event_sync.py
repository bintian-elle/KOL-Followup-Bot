"""Reliable Gmail Watch, Pub/Sub, and History API synchronization primitives."""

import base64
import hashlib
import json
import queue
import threading
import time
from copy import deepcopy
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, List, Tuple

from googleapiclient.errors import HttpError


class HistoryCursorExpired(RuntimeError):
    """The stored Gmail history cursor can no longer be incrementally resumed."""


def retry_execute(request_factory, attempts=6, sleeper=time.sleep):
    """Execute a Google request with bounded exponential backoff."""
    for attempt in range(attempts):
        try:
            return request_factory().execute()
        except HttpError as exc:
            if exc.resp.status not in (403, 429, 500, 502, 503, 504) or attempt == attempts - 1:
                raise
            sleeper(min(2 ** attempt, 30))


class ClientRegistry:
    """Build each long-lived API client once per process."""

    def __init__(self):
        self._clients = {}
        self._lock = threading.Lock()

    def get(self, name, builder):
        with self._lock:
            if name not in self._clients:
                self._clients[name] = builder()
            return self._clients[name]


class TriggerQueue:
    """Coalesce trigger types while allowing only one processing batch."""

    def __init__(self):
        self._pending = set()
        self._running = False
        self._lock = threading.Lock()

    def enqueue(self, trigger):
        with self._lock:
            self._pending.add(trigger)

    def begin(self):
        with self._lock:
            if self._running or not self._pending:
                return None
            self._running = True
            triggers = set(self._pending)
            self._pending.clear()
            return triggers

    def finish(self):
        with self._lock:
            self._running = False


class PubSubInbox:
    """Move StreamingPull callbacks onto the serialized main worker thread."""

    def __init__(self, wake_event=None):
        self.messages = queue.Queue()
        self.wake_event = wake_event or threading.Event()

    def callback(self, message):
        self.messages.put(message)
        self.wake_event.set()

    def drain(self, state_store, trigger_queue, expected_email):
        self.wake_event.clear()
        pending = []
        while True:
            try:
                pending.append(self.messages.get_nowait())
            except queue.Empty:
                break
        if not pending:
            return 0

        watch_state = state_store.load("gmail_watch", {}) or {}
        receipts = list(watch_state.get("notification_ids", []))
        valid = []
        accepted = 0
        latest_history_id = watch_state.get("latest_notification_history_id")
        for message in pending:
            try:
                payload = decode_pubsub_data(message.data)
                if (not payload.get("historyId")
                        or payload.get("emailAddress", "").casefold() != expected_email.casefold()):
                    message.ack()
                    continue
            except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
                message.ack()
                continue
            is_new, receipts = dedupe_notification_ids(receipts, message.message_id)
            accepted += int(is_new)
            candidate = str(payload["historyId"])
            if latest_history_id is None or int(candidate) > int(latest_history_id):
                latest_history_id = candidate
            valid.append(message)

        if valid:
            if accepted:
                watch_state.update({
                    "notification_ids": receipts,
                    "latest_notification_history_id": latest_history_id,
                    "last_notification_at": datetime.now(timezone.utc).isoformat(),
                })
                state_store.save("gmail_watch", watch_state)
            trigger_queue.enqueue("notification")
            for message in valid:
                message.ack()
        return accepted


def config_fingerprint(rows):
    normalized = deepcopy(rows)
    header = next((row for row in normalized if "last_round_robin_owner" in row), None)
    if header:
        column = header.index("last_round_robin_owner")
        for row in normalized:
            if (row is not header and len(row) > max(3, column)
                    and str(row[3]).strip().casefold() in ("true", "false")):
                row[column] = "<runtime>"
    payload = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True)
class HistoryBatch:
    start_history_id: str
    end_history_id: str
    message_ids: FrozenSet[str]
    thread_ids: FrozenSet[str]
    labels_added: Dict[str, FrozenSet[str]]
    labels_removed: Dict[str, FrozenSet[str]]

    @property
    def changed(self):
        return bool(self.message_ids or self.labels_added or self.labels_removed)


def _remember_message(message, message_ids, thread_ids):
    message_id = message.get("id")
    thread_id = message.get("threadId")
    if message_id:
        message_ids.add(message_id)
    if thread_id:
        thread_ids.add(thread_id)


def collect_history(gmail, start_history_id):
    """Drain every Gmail history page after start_history_id into one batch."""
    message_ids = set()
    thread_ids = set()
    labels_added = {}
    labels_removed = {}
    page_token = None
    end_history_id = str(start_history_id)
    while True:
        try:
            page = retry_execute(lambda: gmail.history().list(
                userId="me",
                startHistoryId=str(start_history_id),
                maxResults=500,
                pageToken=page_token,
            ))
        except HttpError as exc:
            if exc.resp.status == 404:
                raise HistoryCursorExpired(str(start_history_id)) from exc
            raise
        end_history_id = str(page.get("historyId", end_history_id))
        for record in page.get("history", []):
            for item in record.get("messagesAdded", []):
                _remember_message(item.get("message", {}), message_ids, thread_ids)
            for item in record.get("messagesDeleted", []):
                _remember_message(item.get("message", {}), message_ids, thread_ids)
            for field, destination in (("labelsAdded", labels_added), ("labelsRemoved", labels_removed)):
                for item in record.get(field, []):
                    message = item.get("message", {})
                    _remember_message(message, message_ids, thread_ids)
                    message_id = message.get("id")
                    if message_id:
                        destination.setdefault(message_id, set()).update(item.get("labelIds", []))
        page_token = page.get("nextPageToken")
        if not page_token:
            break
    return HistoryBatch(
        start_history_id=str(start_history_id),
        end_history_id=end_history_id,
        message_ids=frozenset(message_ids),
        thread_ids=frozenset(thread_ids),
        labels_added={key: frozenset(value) for key, value in labels_added.items()},
        labels_removed={key: frozenset(value) for key, value in labels_removed.items()},
    )


def dedupe_notification_ids(existing: Iterable[str], message_id: str, limit=1000) -> Tuple[bool, List[str]]:
    """Return whether a Pub/Sub receipt is new and a bounded receipt history."""
    receipts = list(existing)
    if message_id in receipts:
        return False, receipts
    receipts.append(message_id)
    return True, receipts[-limit:]


def decode_notification(encoded_data):
    """Decode the Gmail payload stored in Pub/Sub message.data."""
    padded = encoded_data + "=" * (-len(encoded_data) % 4)
    return json.loads(base64.urlsafe_b64decode(padded.encode()).decode())


def decode_pubsub_data(data):
    if isinstance(data, bytes):
        return json.loads(data.decode())
    return decode_notification(data)


def handle_pubsub_message(message, state_store, trigger_queue, expected_email):
    """Persist a receipt, enqueue work, then acknowledge a StreamingPull message."""
    try:
        payload = decode_pubsub_data(message.data)
        if (not payload.get("historyId")
                or payload.get("emailAddress", "").casefold() != expected_email.casefold()):
            message.ack()
            return False
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        message.ack()
        return False

    watch_state = state_store.load("gmail_watch", {}) or {}
    accepted, receipts = dedupe_notification_ids(
        watch_state.get("notification_ids", []), message.message_id,
    )
    if not accepted:
        trigger_queue.enqueue("notification")
        message.ack()
        return False
    watch_state.update({
        "notification_ids": receipts,
        "latest_notification_history_id": str(payload["historyId"]),
        "last_notification_at": datetime.now(timezone.utc).isoformat(),
    })
    state_store.save("gmail_watch", watch_state)
    trigger_queue.enqueue("notification")
    message.ack()
    return True


def subscriber_needs_restart(future):
    return future is None or future.done()


class EventRuntime:
    """Own the durable Gmail cursor and serialize business pipeline commits."""

    def __init__(self, gmail, state_store, pipeline, on_full_reconcile=None):
        self.gmail = gmail
        self.state_store = state_store
        self.pipeline = pipeline
        self.on_full_reconcile = on_full_reconcile or (lambda: None)
        self.last_changed_threads = 0

    def _commit_cursor(self, history_id, trigger, changed):
        self.state_store.save("gmail_event_cursor", {
            "history_id": str(history_id),
            "last_successful_sync_at": datetime.now(timezone.utc).isoformat(),
            "last_trigger": trigger,
            "last_batch_changed": bool(changed),
        })

    def _full_recovery(self, trigger):
        self.last_changed_threads = 0
        baseline = retry_execute(lambda: self.gmail.getProfile(userId="me"))["historyId"]
        if not self.pipeline():
            return False
        self.on_full_reconcile()
        self._commit_cursor(baseline, trigger, True)
        return True

    def sync_history(self, trigger):
        cursor = self.state_store.load("gmail_event_cursor", {}) or {}
        history_id = cursor.get("history_id")
        if not history_id:
            return self._full_recovery("bootstrap")
        try:
            batch = collect_history(self.gmail, history_id)
        except HistoryCursorExpired:
            return self._full_recovery("history_cursor_expired")
        self.last_changed_threads = len(batch.thread_ids)
        if batch.changed and not self.pipeline():
            return False
        self._commit_cursor(batch.end_history_id, trigger, batch.changed)
        return True

    def full_reconcile(self, trigger="scheduled_full"):
        return self._full_recovery(trigger)

    def renew_watch(self, topic_name):
        response = retry_execute(lambda: self.gmail.watch(
            userId="me",
            body={"topicName": topic_name},
        ))
        watch_state = self.state_store.load("gmail_watch", {}) or {}
        watch_state.update({
            "watch_history_id": str(response["historyId"]),
            "expiration": str(response["expiration"]),
            "renewed_at": datetime.now(timezone.utc).isoformat(),
        })
        self.state_store.save("gmail_watch", watch_state)
        return response
