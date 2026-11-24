import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from runtime_alerts import RuntimeAlerter


class RuntimeAlerterTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "alerts.json"
        self.messages = []
        self.now = datetime(2026, 11, 18, 15, tzinfo=timezone.utc)
        self.alerter = RuntimeAlerter(
            "token", "U123", self.messages.append, state_path=self.path, clock=lambda: self.now,
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_repeated_failure_is_sent_once(self):
        self.alerter.failure("gmail_sync", "history.list failed")
        self.now += timedelta(minutes=4, seconds=59)
        self.alerter.failure("gmail_sync", "history.list failed")
        self.assertEqual([], self.messages)
        self.now += timedelta(seconds=1)
        self.alerter.failure("gmail_sync", "history.list failed")
        self.alerter.failure("gmail_sync", "history.list failed")

        self.assertEqual(1, len(self.messages))
        self.assertIn("KOL Follow-up Bot issue", self.messages[0])
        self.assertIn("history.list failed", self.messages[0])

    def test_changed_error_details_do_not_reset_or_duplicate_the_incident(self):
        self.alerter.failure("gmail_sync", "history.list failed")
        self.now += timedelta(minutes=5)
        self.alerter.failure("config", "Config could not be read")
        self.alerter.failure("other", "Another error")
        self.assertEqual(1, len(self.messages))
        self.assertIn("Config could not be read", self.messages[0])

    def test_recovery_before_deadline_is_silent(self):
        self.alerter.failure("gmail_sync", "history.list failed")
        self.now += timedelta(minutes=4)
        self.alerter.recovered()
        self.now += timedelta(minutes=2)
        self.alerter.failure("gmail_sync", "history.list failed")
        self.assertEqual([], self.messages)

    def test_recovery_is_sent_once_and_clears_failure(self):
        self.alerter.failure("gmail_sync", "history.list failed")
        self.now += timedelta(minutes=5)
        self.alerter.failure("gmail_sync", "history.list failed")
        self.alerter.recovered()
        self.alerter.recovered()

        self.assertEqual(2, len(self.messages))
        self.assertIn("recovered", self.messages[1].lower())

    def test_state_survives_process_restart(self):
        self.alerter.failure("gmail_sync", "history.list failed")
        restarted = RuntimeAlerter(
            "token", "U123", self.messages.append, state_path=self.path, clock=lambda: self.now,
        )
        self.now += timedelta(minutes=5)
        restarted.failure("gmail_sync", "history.list failed")

        self.assertEqual(1, len(self.messages))


if __name__ == "__main__":
    unittest.main()
