"""Deduplicated Slack health alerts for the long-running bot."""

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_STATE_PATH = ROOT / ".runtime-alert-state.json"
ALERT_DELAY = timedelta(minutes=5)


class RuntimeAlerter:
    def __init__(self, token, recipient, send_text, state_path=DEFAULT_STATE_PATH, clock=None):
        self.token = token
        self.recipient = recipient
        self.send_text = send_text
        self.state_path = Path(state_path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _load(self):
        try:
            return json.loads(self.state_path.read_text())
        except (FileNotFoundError, ValueError, OSError):
            return {}

    def _save(self, value):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=self.state_path.name + ".", dir=str(self.state_path.parent))
        try:
            with os.fdopen(handle, "w") as stream:
                json.dump(value, stream, sort_keys=True)
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.state_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def failure(self, key, detail):
        detail = str(detail).strip()[-1500:]
        state = self._load()
        if state.get("alerted_at") or state.get("active_fingerprint"):
            return False
        now = self.clock()
        if not state.get("failed_at"):
            self._save({"failed_at": now.isoformat()})
            return False
        if now - datetime.fromisoformat(state["failed_at"]) < ALERT_DELAY:
            return False
        self.send_text(
            "🚨 KOL Follow-up Bot issue\n"
            f"Area: {key}\n"
            f"Details: {detail}\n"
            "The service will keep retrying automatically."
        )
        self._save({**state, "alerted_at": now.isoformat()})
        return True

    def recovered(self):
        state = self._load()
        if not (state.get("alerted_at") or state.get("active_fingerprint")):
            if state.get("failed_at"):
                self._save({})
            return False
        self.send_text(
            "✅ KOL Follow-up Bot recovered\n"
            "Gmail monitoring and task synchronization are running normally again."
        )
        self._save({"recovered_at": self.clock().isoformat()})
        return True
