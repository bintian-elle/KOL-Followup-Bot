import unittest
from unittest.mock import patch

import run_bot
from gmail_event_sync import ClientRegistry, TriggerQueue, config_fingerprint


class RuntimeScheduleTests(unittest.TestCase):
    def test_deadlines_use_requested_hybrid_intervals(self):
        schedule = run_bot.RuntimeSchedule.started_at(100.0)

        self.assertEqual(1900.0, schedule.next_history_catchup)
        self.assertEqual(86500.0, schedule.next_full_reconciliation)
        self.assertEqual(7300.0, schedule.next_config_refresh)
        self.assertEqual(86500.0, schedule.next_watch_renewal)
        self.assertEqual(400.0, schedule.next_maintenance)

    def test_due_work_advances_without_catchup_loops(self):
        schedule = run_bot.RuntimeSchedule.started_at(0.0)

        due = schedule.take_due(90000.0)

        self.assertEqual(
            {"history_catchup", "full_reconciliation", "config_refresh", "watch_renewal", "maintenance"},
            due,
        )
        self.assertGreater(schedule.next_history_catchup, 90000.0)
        self.assertGreater(schedule.next_full_reconciliation, 90000.0)


class RuntimePrimitiveTests(unittest.TestCase):
    def test_trigger_queue_coalesces_duplicates_and_serializes_new_work(self):
        queue = TriggerQueue()
        queue.enqueue("notification")
        queue.enqueue("notification")

        self.assertEqual({"notification"}, queue.begin())
        self.assertIsNone(queue.begin())
        queue.enqueue("history_catchup")
        queue.finish()
        self.assertEqual({"history_catchup"}, queue.begin())

    def test_client_registry_reuses_built_clients(self):
        registry = ClientRegistry()
        builds = []

        first = registry.get("gmail", lambda: builds.append("gmail") or object())
        second = registry.get("gmail", lambda: builds.append("again") or object())

        self.assertIs(first, second)
        self.assertEqual(["gmail"], builds)

    def test_config_fingerprint_changes_only_with_config_content(self):
        first = config_fingerprint([["a", "1"], ["b", "2"]])
        self.assertEqual(first, config_fingerprint([["a", "1"], ["b", "2"]]))
        self.assertNotEqual(first, config_fingerprint([["a", "1"], ["b", "3"]]))


class PipelineTests(unittest.TestCase):
    @patch("run_bot.run_script", return_value=0)
    def test_event_pipeline_preserves_business_order(self, run_script):
        self.assertTrue(run_bot.compatibility_pipeline())
        self.assertEqual([
            "process_closed.py",
            "monitor_needs_review.py",
            "route_reach_out_replies.py",
            "sync_reach_out.py",
            "sync_needs_review.py",
            "sync_active_label.py",
            "assign_active.py",
            "remind_overdue.py",
            "daily_digest.py",
        ], [call.args[0] for call in run_script.call_args_list])

    @patch("run_bot.run_script", return_value=0)
    def test_time_maintenance_runs_without_full_reconciliation(self, run_script):
        self.assertTrue(run_bot.time_maintenance())
        self.assertEqual(
            ["remind_overdue.py", "daily_digest.py"],
            [call.args[0] for call in run_script.call_args_list],
        )


if __name__ == "__main__":
    unittest.main()
