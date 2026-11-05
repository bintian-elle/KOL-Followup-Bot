import unittest
from unittest.mock import patch

import run_bot


class SchedulerTests(unittest.TestCase):
    def test_intervals_are_two_and_seven_minutes(self):
        self.assertEqual(120, run_bot.FAST_INTERVAL_SECONDS)
        self.assertEqual(420, run_bot.FULL_INTERVAL_SECONDS)

    def test_advance_deadline_skips_missed_intervals(self):
        self.assertEqual(480, run_bot.advance_deadline(120, 120, 370))
        self.assertEqual(420, run_bot.advance_deadline(0, 420, 1))

    @patch("run_bot.run_script", return_value=0)
    def test_fast_cycle_runs_incremental_workers_sequentially(self, run_script):
        self.assertTrue(run_bot.cycle(full_scan=False))
        self.assertEqual([
            "monitor_needs_review.py",
            "route_reach_out_replies.py",
            "sync_reach_out.py",
            "sync_needs_review.py",
        ], [call.args[0] for call in run_script.call_args_list])

    @patch("run_bot.run_script", return_value=0)
    def test_full_cycle_includes_fast_work_before_active_assignment(self, run_script):
        self.assertTrue(run_bot.cycle(full_scan=True))
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


if __name__ == "__main__":
    unittest.main()
