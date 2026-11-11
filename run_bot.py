"""Run the event-driven Gmail synchronization service."""

import argparse
import fcntl
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
LOCK = ROOT / ".bot-runner.lock"
HISTORY_CATCHUP_SECONDS = 30 * 60
FULL_RECONCILIATION_SECONDS = 24 * 60 * 60
CONFIG_REFRESH_SECONDS = 2 * 60 * 60
WATCH_RENEWAL_SECONDS = 24 * 60 * 60
MAINTENANCE_SECONDS = 5 * 60


def _advance(deadline, interval, now):
    while deadline <= now:
        deadline += interval
    return deadline


@dataclass
class RuntimeSchedule:
    next_history_catchup: float
    next_full_reconciliation: float
    next_config_refresh: float
    next_watch_renewal: float
    next_maintenance: float

    @classmethod
    def started_at(cls, now):
        return cls(
            next_history_catchup=now + HISTORY_CATCHUP_SECONDS,
            next_full_reconciliation=now + FULL_RECONCILIATION_SECONDS,
            next_config_refresh=now + CONFIG_REFRESH_SECONDS,
            next_watch_renewal=now + WATCH_RENEWAL_SECONDS,
            next_maintenance=now + MAINTENANCE_SECONDS,
        )

    def take_due(self, now):
        due = set()
        fields = (
            ("history_catchup", "next_history_catchup", HISTORY_CATCHUP_SECONDS),
            ("full_reconciliation", "next_full_reconciliation", FULL_RECONCILIATION_SECONDS),
            ("config_refresh", "next_config_refresh", CONFIG_REFRESH_SECONDS),
            ("watch_renewal", "next_watch_renewal", WATCH_RENEWAL_SECONDS),
            ("maintenance", "next_maintenance", MAINTENANCE_SECONDS),
        )
        for trigger, field, interval in fields:
            deadline = getattr(self, field)
            if now >= deadline:
                due.add(trigger)
                setattr(self, field, _advance(deadline, interval, now))
        return due

    def seconds_until_next(self, now):
        return max(0.0, min(
            self.next_history_catchup,
            self.next_full_reconciliation,
            self.next_config_refresh,
            self.next_watch_renewal,
            self.next_maintenance,
        ) - now)


def run_script(name):
    process = subprocess.Popen(
        [sys.executable, str(ROOT / name)], cwd=ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    for line in process.stdout:
        stamp = datetime.now(timezone.utc).isoformat()
        print(f"{stamp} {name}: {line.rstrip()}", flush=True)
    return_code = process.wait()
    if return_code:
        print(f"{datetime.now(timezone.utc).isoformat()} {name}: FAILED (exit {return_code})", flush=True)
    return return_code


def compatibility_pipeline():
    """Run the established business workers once, in their required order."""
    closure_result = run_script("process_closed.py")
    if closure_result:
        return False
    monitor_result = run_script("monitor_needs_review.py")
    route_result = run_script("route_reach_out_replies.py")
    reach_result = run_script("sync_reach_out.py") if not route_result else 1
    needs_result = run_script("sync_needs_review.py") if not monitor_result else 1
    active_result = run_script("sync_active_label.py")
    if active_result == 0:
        assignment_result = run_script("assign_active.py")
        reminder_result = run_script("remind_overdue.py")
    else:
        assignment_result = reminder_result = 1
    if not any((monitor_result, route_result, reach_result, needs_result, active_result)):
        digest_result = run_script("daily_digest.py")
    else:
        digest_result = 1
    return not any((monitor_result, route_result, reach_result, needs_result, active_result,
                    assignment_result, reminder_result, digest_result))


def time_maintenance():
    """Keep time-based reminders and the Daily Digest independent of mail events."""
    reminder_result = run_script("remind_overdue.py")
    digest_result = run_script("daily_digest.py")
    return not any((reminder_result, digest_result))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run one compatibility reconciliation and exit")
    args = parser.parse_args()
    with LOCK.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another bot runner is already active", flush=True)
            return 1
        if args.once:
            return 0 if compatibility_pipeline() else 1
        raise RuntimeError("Pub/Sub event runtime is not initialized")


if __name__ == "__main__":
    raise SystemExit(main())
