import tempfile
import unittest
from pathlib import Path

from runtime_alerts import RuntimeAlerter


class RuntimeAlerterTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "alerts.json"
        self.messages = []
        self.alerter = RuntimeAlerter(
            "token", "U123", self.messages.append, state_path=self.path,
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_repeated_failure_is_sent_once(self):
        self.alerter.failure("gmail_sync", "history.list failed")
        self.alerter.failure("gmail_sync", "history.list failed")

        self.assertEqual(1, len(self.messages))
        self.assertIn("KOL Follow-up Bot issue", self.messages[0])
        self.assertIn("history.list failed", self.messages[0])

    def test_changed_failure_is_sent_again(self):
        self.alerter.failure("gmail_sync", "history.list failed")
        self.alerter.failure("config", "Config could not be read")

        self.assertEqual(2, len(self.messages))

    def test_recovery_is_sent_once_and_clears_failure(self):
        self.alerter.failure("gmail_sync", "history.list failed")
        self.alerter.recovered()
        self.alerter.recovered()

        self.assertEqual(2, len(self.messages))
        self.assertIn("recovered", self.messages[1].lower())

    def test_state_survives_process_restart(self):
        self.alerter.failure("gmail_sync", "history.list failed")
        restarted = RuntimeAlerter(
            "token", "U123", self.messages.append, state_path=self.path,
        )
        restarted.failure("gmail_sync", "history.list failed")

        self.assertEqual(1, len(self.messages))


if __name__ == "__main__":
    unittest.main()
