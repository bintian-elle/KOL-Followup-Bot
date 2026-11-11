import json
import tempfile
import unittest
from pathlib import Path

from runtime_config import ConfigCache, apply_config_updates


class FakeValues:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0

    def get(self, **kwargs):
        self.calls += 1
        rows = self.rows

        class Request:
            def execute(self):
                return {"values": rows}

        return Request()


class FakeSheets:
    def __init__(self, rows):
        self.api = FakeValues(rows)

    def values(self):
        return self.api


class ConfigCacheTests(unittest.TestCase):
    def test_reuses_config_for_two_hours_without_a_sheet_read(self):
        with tempfile.TemporaryDirectory() as directory:
            sheets = FakeSheets([["key", "value"]])
            clock = iter([100.0, 200.0])
            cache = ConfigCache(Path(directory) / "config.json", clock=lambda: next(clock))

            self.assertEqual([["key", "value"]], cache.load(sheets, "sheet"))
            self.assertEqual([["key", "value"]], cache.load(sheets, "sheet"))
            self.assertEqual(1, sheets.api.calls)

    def test_force_refresh_replaces_cached_config(self):
        with tempfile.TemporaryDirectory() as directory:
            sheets = FakeSheets([["key", "old"]])
            cache = ConfigCache(Path(directory) / "config.json", clock=lambda: 100.0)
            cache.load(sheets, "sheet")
            sheets.api.rows = [["key", "new"]]

            self.assertEqual([["key", "new"]], cache.load(sheets, "sheet", force=True))
            self.assertEqual(2, sheets.api.calls)

    def test_programmatic_rotation_update_is_visible_without_refresh(self):
        rows = [
            ["Team rotation"],
            ["Member", "Email", "Slack", "Active", "last_round_robin_owner"],
            ["Dan", "", "", "TRUE", "TRUE"],
            ["Ivan", "", "", "TRUE", "FALSE"],
        ]

        updated = apply_config_updates(rows, [(3, 4, "FALSE"), (4, 4, "TRUE")])

        self.assertEqual("FALSE", updated[2][4])
        self.assertEqual("TRUE", updated[3][4])
        self.assertEqual("TRUE", rows[2][4])


if __name__ == "__main__":
    unittest.main()
