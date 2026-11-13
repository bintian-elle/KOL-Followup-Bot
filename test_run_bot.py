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

    def test_restart_preserves_remaining_time_until_daily_reconciliation(self):
        schedule = run_bot.RuntimeSchedule.restored_at(
            monotonic_now=500.0,
            wall_now=1000.0 + 23 * 60 * 60,
            runtime_state={"last_full_reconciliation_epoch": 1000.0},
        )

        self.assertEqual(500.0 + 60 * 60, schedule.next_full_reconciliation)

    def test_failed_full_reconciliation_is_retried_in_five_minutes(self):
        schedule = run_bot.RuntimeSchedule.started_at(0.0)

        schedule.retry("full_reconciliation", now=100.0)

        self.assertEqual(400.0, schedule.next_full_reconciliation)


class RuntimePrimitiveTests(unittest.TestCase):
    def test_health_alerts_use_dedicated_recipient_then_developer_not_pilot(self):
        self.assertEqual("U-ERROR", run_bot.health_alert_recipient({
            "error_notification_slack_id": "U-ERROR",
            "pilot_recipient_slack_id": "U-PILOT",
        }, [["Testing developer", "", "FALSE", "U-DEVELOPER"]]))
        self.assertEqual("U-DEVELOPER", run_bot.health_alert_recipient({
            "pilot_recipient_slack_id": "U-PILOT",
        }, [["Testing developer", "", "FALSE", "U-DEVELOPER"]]))
        self.assertEqual("", run_bot.health_alert_recipient({
            "pilot_recipient_slack_id": "U-PILOT",
        }, []))

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

    def test_round_robin_runtime_flag_does_not_look_like_a_config_change(self):
        before = [
            ["Team rotation"],
            ["Member", "Email", "Slack", "Active", "last_round_robin_owner"],
            ["Dan", "d@example.com", "U1", "TRUE", "TRUE"],
            ["Ivan", "i@example.com", "U2", "TRUE", "FALSE"],
        ]
        after = [row[:] for row in before]
        after[2][4], after[3][4] = "FALSE", "TRUE"

        self.assertEqual(config_fingerprint(before), config_fingerprint(after))


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
