import json
import unittest

from googleapiclient.errors import HttpError

from gmail_event_sync import (
    HistoryCursorExpired,
    collect_history,
    decode_notification,
    dedupe_notification_ids,
)


class FakeRequest:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def execute(self):
        if self.error:
            raise self.error
        return self.result


class FakeHistory:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        page = self.pages[kwargs.get("pageToken")]
        return page if isinstance(page, FakeRequest) else FakeRequest(page)


class FakeGmail:
    def __init__(self, pages):
        self.history_api = FakeHistory(pages)

    def history(self):
        return self.history_api


class HistoryCollectionTests(unittest.TestCase):
    def test_collects_specific_message_and_label_changes_across_pages(self):
        gmail = FakeGmail({
            None: {
                "history": [
                    {"id": "11", "messagesAdded": [{"message": {"id": "m1", "threadId": "t1"}}]},
                    {"id": "12", "labelsAdded": [{"message": {"id": "m2", "threadId": "t2"}, "labelIds": ["L1"]}]},
                ],
                "nextPageToken": "p2",
                "historyId": "12",
            },
            "p2": {
                "history": [
                    {"id": "13", "labelsRemoved": [{"message": {"id": "m1", "threadId": "t1"}, "labelIds": ["L2"]}]},
                ],
                "historyId": "13",
            },
        })

        batch = collect_history(gmail, "10")

        self.assertEqual("10", batch.start_history_id)
        self.assertEqual("13", batch.end_history_id)
        self.assertEqual(frozenset({"m1", "m2"}), batch.message_ids)
        self.assertEqual(frozenset({"t1", "t2"}), batch.thread_ids)
        self.assertEqual({"m2": frozenset({"L1"})}, batch.labels_added)
        self.assertEqual({"m1": frozenset({"L2"})}, batch.labels_removed)
        self.assertEqual([None, "p2"], [call.get("pageToken") for call in gmail.history_api.calls])

    def test_duplicate_history_records_do_not_duplicate_changes(self):
        repeated = {"id": "11", "messagesAdded": [{"message": {"id": "m1", "threadId": "t1"}}]}
        gmail = FakeGmail({None: {"history": [repeated, repeated], "historyId": "11"}})

        batch = collect_history(gmail, "10")

        self.assertEqual(frozenset({"m1"}), batch.message_ids)
        self.assertEqual(frozenset({"t1"}), batch.thread_ids)

    def test_outdated_history_cursor_is_a_recoverable_domain_error(self):
        response = type("Response", (), {"status": 404, "reason": "Not Found"})()
        error = HttpError(response, b'{"error":{"message":"startHistoryId too old"}}')
        gmail = FakeGmail({None: FakeRequest(error=error)})

        with self.assertRaises(HistoryCursorExpired):
            collect_history(gmail, "1")


class NotificationTests(unittest.TestCase):
    def test_duplicate_pubsub_message_id_is_ignored_with_bounded_receipts(self):
        accepted, receipts = dedupe_notification_ids(["old", "same"], "same", limit=3)
        self.assertFalse(accepted)
        self.assertEqual(["old", "same"], receipts)

        accepted, receipts = dedupe_notification_ids(["a", "b", "c"], "new", limit=3)
        self.assertTrue(accepted)
        self.assertEqual(["b", "c", "new"], receipts)

    def test_decodes_gmail_notification_payload(self):
        import base64

        raw = json.dumps({"emailAddress": "kol@elle-media.com", "historyId": "321"}).encode()
        encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")

        self.assertEqual(
            {"emailAddress": "kol@elle-media.com", "historyId": "321"},
            decode_notification(encoded),
        )


if __name__ == "__main__":
    unittest.main()
