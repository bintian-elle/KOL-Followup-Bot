"""Reliable Gmail Watch, Pub/Sub, and History API synchronization primitives."""

import base64
import json
from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, List, Tuple

from googleapiclient.errors import HttpError


class HistoryCursorExpired(RuntimeError):
    """The stored Gmail history cursor can no longer be incrementally resumed."""


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
        request = gmail.history().list(
            userId="me",
            startHistoryId=str(start_history_id),
            maxResults=500,
            pageToken=page_token,
        )
        try:
            page = request.execute()
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
