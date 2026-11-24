import unittest

from sync_active_label import merge_track_rows


class MergeTrackRowsTests(unittest.TestCase):
    def test_incremental_update_preserves_other_threads_and_sorts_by_reply(self):
        existing = [
            ["t-old", "", "2026-11-20 10:00:00 PST"],
            ["t-change", "", "2026-11-21 10:00:00 PST"],
        ]
        changed = ["t-change", "", "2026-11-24 09:00:00 PST"]

        rows = merge_track_rows(existing, {"t-change": changed}, set())

        self.assertEqual(["t-change", "t-old"], [row[0] for row in rows])
        self.assertEqual(changed, rows[0])

    def test_incremental_removal_deletes_only_changed_thread(self):
        existing = [
            ["t-keep", "", "2026-11-20 10:00:00 PST"],
            ["t-remove", "", "2026-11-21 10:00:00 PST"],
        ]

        rows = merge_track_rows(existing, {}, {"t-remove"})

        self.assertEqual([["t-keep", "", "2026-11-20 10:00:00 PST"]], rows)


if __name__ == "__main__":
    unittest.main()
