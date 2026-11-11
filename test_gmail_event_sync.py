import json
import unittest

from googleapiclient.errors import HttpError

from gmail_event_sync import (
    EventRuntime,
    HistoryCursorExpired,
    TriggerQueue,
    collect_history,
    decode_notification,
    dedupe_notification_ids,
    handle_pubsub_message,
    retry_execute,
    subscriber_needs_restart,
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


class FakeState:
    def __init__(self, values=None, events=None):
        self.values = dict(values or {})
        self.events = events if events is not None else []

    def load(self, namespace, default=None):
        return self.values.get(namespace, default)

    def save(self, namespace, value):
        self.events.append(("save", namespace))
        self.values[namespace] = value


class FakeMessage:
    def __init__(self, message_id, data, events=None):
        self.message_id = message_id
        self.data = data
        self.events = events if events is not None else []

    def ack(self):
        self.events.append(("ack", self.message_id))

    def nack(self):
        self.events.append(("nack", self.message_id))


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

    def test_valid_notification_is_persisted_before_ack_and_queued_once(self):
        events = []
        state = FakeState(events=events)
        queue = TriggerQueue()
        message = FakeMessage("pub-1", b'{"emailAddress":"kol@elle-media.com","historyId":"44"}', events)

        self.assertTrue(handle_pubsub_message(message, state, queue, "kol@elle-media.com"))

        self.assertEqual([("save", "gmail_watch"), ("ack", "pub-1")], events)
        self.assertEqual({"notification"}, queue.begin())
        self.assertEqual("44", state.values["gmail_watch"]["latest_notification_history_id"])

    def test_duplicate_notification_is_acked_without_queueing(self):
        events = []
        state = FakeState({"gmail_watch": {"notification_ids": ["pub-1"]}}, events)
        queue = TriggerQueue()
        message = FakeMessage("pub-1", b'{"emailAddress":"kol@elle-media.com","historyId":"44"}', events)

        self.assertFalse(handle_pubsub_message(message, state, queue, "kol@elle-media.com"))

        self.assertEqual([("ack", "pub-1")], events)
        self.assertIsNone(queue.begin())

    def test_invalid_notification_is_acked_as_poison_message(self):
        events = []
        message = FakeMessage("bad", b'not-json', events)

        self.assertFalse(handle_pubsub_message(message, FakeState(), TriggerQueue(), "kol@elle-media.com"))

        self.assertEqual([("ack", "bad")], events)


class FakeFuture:
    def __init__(self, done):
        self._done = done

    def done(self):
        return self._done


class RuntimeRecoveryTests(unittest.TestCase):
    def test_first_run_full_sync_uses_pre_sync_profile_as_replay_baseline(self):
        class FirstRunGmail:
            def getProfile(self, **kwargs):
                events.append(("profile", "read"))
                return FakeRequest({"historyId": "70"})

        events = []
        state = FakeState(events=events)
        runtime = EventRuntime(
            FirstRunGmail(),
            state,
            lambda: events.append(("pipeline", "full")) or True,
        )

        self.assertTrue(runtime.sync_history("startup"))

        self.assertEqual([
            ("profile", "read"),
            ("pipeline", "full"),
            ("save", "gmail_event_cursor"),
        ], events)
        self.assertEqual("70", state.values["gmail_event_cursor"]["history_id"])

    def test_transient_google_error_is_retried_with_a_bound(self):
        response = type("Response", (), {"status": 503, "reason": "Unavailable"})()
        attempts = [HttpError(response, b'{}'), HttpError(response, b'{}'), {"ok": True}]
        sleeps = []

        result = retry_execute(
            lambda: FakeRequest(result=attempts.pop(0)) if isinstance(attempts[0], dict)
            else FakeRequest(error=attempts.pop(0)),
            attempts=3,
            sleeper=lambda seconds: sleeps.append(seconds),
        )

        self.assertEqual({"ok": True}, result)
        self.assertEqual([1, 2], sleeps)

    def test_subscriber_restarts_only_after_disconnect(self):
        self.assertFalse(subscriber_needs_restart(FakeFuture(False)))
        self.assertTrue(subscriber_needs_restart(FakeFuture(True)))
        self.assertTrue(subscriber_needs_restart(None))

    def test_restart_resumes_from_stored_cursor_and_commits_after_pipeline(self):
        events = []
        gmail = FakeGmail({None: {
            "history": [{"id": "11", "messagesAdded": [{"message": {"id": "m1", "threadId": "t1"}}]}],
            "historyId": "11",
        }})
        state = FakeState({"gmail_event_cursor": {"history_id": "10"}}, events)

        runtime = EventRuntime(gmail, state, lambda: events.append(("pipeline", "event")) or True)
        self.assertTrue(runtime.sync_history("notification"))

        self.assertEqual([("pipeline", "event"), ("save", "gmail_event_cursor")], events)
        self.assertEqual("11", state.values["gmail_event_cursor"]["history_id"])
        self.assertEqual(1, runtime.last_changed_threads)

    def test_pipeline_failure_does_not_advance_cursor(self):
        gmail = FakeGmail({None: {
            "history": [{"id": "11", "messagesAdded": [{"message": {"id": "m1", "threadId": "t1"}}]}],
            "historyId": "11",
        }})
        state = FakeState({"gmail_event_cursor": {"history_id": "10"}})

        runtime = EventRuntime(gmail, state, lambda: False)
        self.assertFalse(runtime.sync_history("notification"))

        self.assertEqual("10", state.values["gmail_event_cursor"]["history_id"])

    def test_expired_cursor_runs_full_reconciliation_and_restarts_from_profile(self):
        response = type("Response", (), {"status": 404, "reason": "Not Found"})()
        error = HttpError(response, b'{"error":{"message":"old"}}')

        class ExpiredGmail(FakeGmail):
            def getProfile(self, **kwargs):
                return FakeRequest({"historyId": "90"})

        events = []
        gmail = ExpiredGmail({None: FakeRequest(error=error)})
        state = FakeState({"gmail_event_cursor": {"history_id": "1"}}, events)
        runtime = EventRuntime(
            gmail,
            state,
            lambda: events.append(("pipeline", "full")) or True,
            on_full_reconcile=lambda: events.append(("marked", "full")),
        )

        self.assertTrue(runtime.sync_history("catchup"))

        self.assertEqual([
            ("pipeline", "full"),
            ("marked", "full"),
            ("save", "gmail_event_cursor"),
        ], events)
        self.assertEqual("90", state.values["gmail_event_cursor"]["history_id"])

    def test_watch_renewal_persists_expiration_without_advancing_event_cursor(self):
        class WatchGmail:
            def watch(self, **kwargs):
                self.body = kwargs["body"]
                return FakeRequest({"historyId": "500", "expiration": "999000"})

        gmail = WatchGmail()
        state = FakeState({"gmail_event_cursor": {"history_id": "400"}})
        runtime = EventRuntime(gmail, state, lambda: True)

        runtime.renew_watch("projects/p/topics/gmail")

        self.assertEqual("projects/p/topics/gmail", gmail.body["topicName"])
        self.assertEqual("400", state.values["gmail_event_cursor"]["history_id"])
        self.assertEqual("999000", state.values["gmail_watch"]["expiration"])


if __name__ == "__main__":
    unittest.main()
