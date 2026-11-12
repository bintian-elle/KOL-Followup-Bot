"""Deduplicated Slack health alerts for the long-running bot."""

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_STATE_PATH = ROOT / ".runtime-alert-state.json"


class RuntimeAlerter:
    def __init__(self, token, recipient, send_text, state_path=DEFAULT_STATE_PATH):
        self.token = token
        self.recipient = recipient
        self.send_text = send_text
        self.state_path = Path(state_path)

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
        fingerprint = hashlib.sha256(f"{key}\n{detail}".encode()).hexdigest()
        state = self._load()
        if state.get("active_fingerprint") == fingerprint:
            return False
        self.send_text(
            "🚨 KOL Follow-up Bot issue\n"
            f"Area: {key}\n"
            f"Details: {detail}\n"
            "The service will keep retrying automatically."
        )
        self._save({
            "active_fingerprint": fingerprint,
            "failed_at": datetime.now(timezone.utc).isoformat(),
            "key": key,
        })
        return True

    def recovered(self):
        state = self._load()
        if not state.get("active_fingerprint"):
            return False
        self.send_text(
            "✅ KOL Follow-up Bot recovered\n"
            "Gmail monitoring and task synchronization are running normally again."
        )
        self._save({"recovered_at": datetime.now(timezone.utc).isoformat()})
        return True
