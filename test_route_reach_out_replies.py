import unittest

from route_reach_out_replies import candidate_thread_ids, reply_scan_start


class ReplyScanCheckpointTests(unittest.TestCase):
    def test_first_scan_checks_every_tracked_thread(self):
        since, full_scan = reply_scan_start({}, 1_000)
        self.assertEqual(0, since)
        self.assertTrue(full_scan)
        self.assertEqual(
            {"thread-a", "thread-b"},
            candidate_thread_ids({"thread-a", "thread-b"}, [], full_scan),
        )

    def test_incremental_scan_uses_five_minute_overlap(self):
        since, full_scan = reply_scan_start({"last_successful_scan_started": 900}, 1_000)
        self.assertEqual(600, since)
        self.assertFalse(full_scan)

    def test_incremental_scan_only_checks_recent_tracked_threads(self):
        recent = [
            {"threadId": "thread-b"},
            {"threadId": "untracked"},
            {"threadId": "thread-b"},
        ]
        self.assertEqual(
            {"thread-b"},
            candidate_thread_ids({"thread-a", "thread-b"}, recent, False),
        )


if __name__ == "__main__":
    unittest.main()
