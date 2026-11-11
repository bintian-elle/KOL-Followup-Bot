"""Two-hour local cache for the Google Sheet Config tab."""

import json
import os
import tempfile
import time
from copy import deepcopy
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_PATH = ROOT / ".runtime-config.json"
DEFAULT_TTL_SECONDS = 2 * 60 * 60


class ConfigCache:
    def __init__(self, path=DEFAULT_PATH, ttl_seconds=DEFAULT_TTL_SECONDS, clock=time.time):
        self.path = Path(path)
        self.ttl_seconds = ttl_seconds
        self.clock = clock

    def _read(self):
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError, OSError):
            return None

    def save(self, rows, fetched_at=None):
        payload = {"fetched_at": self.clock() if fetched_at is None else fetched_at, "rows": rows}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=self.path.name + ".", dir=str(self.path.parent))
        try:
            with os.fdopen(handle, "w") as stream:
                json.dump(payload, stream, ensure_ascii=False)
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def load(self, sheets, spreadsheet_id, force=False):
        now = self.clock()
        cached = self._read()
        if (not force and cached and now - float(cached.get("fetched_at", 0)) < self.ttl_seconds
                and isinstance(cached.get("rows"), list)):
            return cached["rows"]
        rows = sheets.values().get(
            spreadsheetId=spreadsheet_id,
            range="'Config'!A1:E100",
        ).execute().get("values", [])
        self.save(rows, fetched_at=now)
        return rows


def apply_config_updates(rows, updates):
    updated = deepcopy(rows)
    for row_number, column_index, value in updates:
        while len(updated) < row_number:
            updated.append([])
        row = updated[row_number - 1]
        while len(row) <= column_index:
            row.append("")
        row[column_index] = value
    return updated


_CACHE = ConfigCache()


def load_config_rows(sheets, spreadsheet_id, force=False):
    return _CACHE.load(sheets, spreadsheet_id, force=force)


def save_config_rows(rows):
    _CACHE.save(rows)
