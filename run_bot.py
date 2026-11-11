"""Run the event-driven Gmail synchronization service."""

import argparse
import fcntl
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from assign_active import load_env
from gmail_event_sync import (ClientRegistry, EventRuntime, PubSubInbox, TriggerQueue,
                              config_fingerprint, subscriber_needs_restart)
from event_sync_metrics import CountingHttp, RuntimeMetrics
from runtime_config import load_config_rows
from sheet_state import SheetState


ROOT = Path(__file__).resolve().parent
LOCK = ROOT / ".bot-runner.lock"
HISTORY_CATCHUP_SECONDS = 30 * 60
FULL_RECONCILIATION_SECONDS = 24 * 60 * 60
CONFIG_REFRESH_SECONDS = 2 * 60 * 60
WATCH_RENEWAL_SECONDS = 24 * 60 * 60
MAINTENANCE_SECONDS = 5 * 60
RUNTIME_METRICS = None


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

    @classmethod
    def restored_at(cls, monotonic_now, wall_now, runtime_state):
        schedule = cls.started_at(monotonic_now)
        last_full = runtime_state.get("last_full_reconciliation_epoch")
        if last_full is not None:
            remaining = max(0.0, FULL_RECONCILIATION_SECONDS - (wall_now - float(last_full)))
            schedule.next_full_reconciliation = monotonic_now + remaining
        return schedule

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

    def retry(self, trigger, now, delay=5 * 60):
        field = {
            "full_reconciliation": "next_full_reconciliation",
            "history_catchup": "next_history_catchup",
            "config_refresh": "next_config_refresh",
            "watch_renewal": "next_watch_renewal",
            "maintenance": "next_maintenance",
        }[trigger]
        setattr(self, field, now + delay)


def run_script(name):
    if RUNTIME_METRICS is not None:
        RUNTIME_METRICS.record_worker(name)
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


def build_clients(metrics):
    registry = ClientRegistry()
    service_file = ROOT / os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"]

    def sheets_builder():
        credentials = service_account.Credentials.from_service_account_file(
            str(service_file), scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        resource = build("sheets", "v4", credentials=credentials, cache_discovery=False).spreadsheets()
        resource._http = CountingHttp(resource._http, metrics, "sheets")
        return resource

    def gmail_builder():
        credentials = Credentials(
            None,
            refresh_token=os.environ["GMAIL_REFRESH_TOKEN"],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=os.environ["GMAIL_CLIENT_ID"],
            client_secret=os.environ["GMAIL_CLIENT_SECRET"],
            scopes=["https://www.googleapis.com/auth/gmail.modify"],
        )
        credentials.refresh(Request())
        resource = build("gmail", "v1", credentials=credentials, cache_discovery=False).users()
        resource._http = CountingHttp(resource._http, metrics, "gmail")
        return resource

    def subscriber_builder():
        from google.cloud import pubsub_v1

        credentials = service_account.Credentials.from_service_account_file(
            str(service_file), scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        return pubsub_v1.SubscriberClient(credentials=credentials)

    return (
        registry.get("sheets", sheets_builder),
        registry.get("gmail", gmail_builder),
        registry.get("subscriber", subscriber_builder),
    )


def refresh_config(sheets, sheet_id, state_store):
    rows = load_config_rows(sheets, sheet_id, force=True)
    fingerprint = config_fingerprint(rows)
    runtime_state = state_store.load("gmail_event_runtime", {}) or {}
    changed = bool(runtime_state.get("config_fingerprint")
                   and runtime_state["config_fingerprint"] != fingerprint)
    runtime_state.update({
        "config_fingerprint": fingerprint,
        "config_refreshed_at": datetime.now(timezone.utc).isoformat(),
        "config_refreshed_epoch": time.time(),
    })
    state_store.save("gmail_event_runtime", runtime_state)
    return rows, changed


def mark_full_reconciliation(state_store):
    runtime_state = state_store.load("gmail_event_runtime", {}) or {}
    runtime_state.update({
        "last_full_reconciliation_at": datetime.now(timezone.utc).isoformat(),
        "last_full_reconciliation_epoch": time.time(),
    })
    state_store.save("gmail_event_runtime", runtime_state)


def _settings(rows):
    return {row[0]: row[1] for row in rows if len(row) > 1}


def _start_subscription(subscriber, subscription_name, inbox):
    return subscriber.subscribe(subscription_name, callback=inbox.callback)


def _start_slack_socket():
    return subprocess.Popen([sys.executable, str(ROOT / "slack_socket.py")], cwd=ROOT)


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

        load_env()
        required = ("GMAIL_PUBSUB_TOPIC", "GMAIL_PUBSUB_SUBSCRIPTION")
        missing = [name for name in required if not os.environ.get(name)]
        if missing:
            raise RuntimeError("Missing event-sync environment variables: " + ", ".join(missing))

        global RUNTIME_METRICS
        RUNTIME_METRICS = RuntimeMetrics()
        sheets, gmail, subscriber = build_clients(RUNTIME_METRICS)
        sheet_id = os.environ["GOOGLE_SHEET_ID"]
        state_store = SheetState(sheets, sheet_id)
        config_rows, _ = refresh_config(sheets, sheet_id, state_store)
        settings = _settings(config_rows)
        expected_email = settings["gmail_mailbox"].strip()
        topic_name = os.environ["GMAIL_PUBSUB_TOPIC"]
        subscription_name = os.environ["GMAIL_PUBSUB_SUBSCRIPTION"]

        trigger_queue = TriggerQueue()
        wake_event = threading.Event()
        inbox = PubSubInbox(wake_event)
        runtime = EventRuntime(
            gmail, state_store, compatibility_pipeline,
            on_full_reconcile=lambda: mark_full_reconciliation(state_store),
        )
        runtime_state = state_store.load("gmail_event_runtime", {}) or {}
        schedule = RuntimeSchedule.restored_at(time.monotonic(), time.time(), runtime_state)
        socket_process = None
        subscriber_future = None
        try:
            run_script("publish_slack_home.py")
            socket_process = _start_slack_socket()
            subscriber_future = _start_subscription(subscriber, subscription_name, inbox)
            runtime.renew_watch(topic_name)
            trigger_queue.enqueue("startup")
            print(f"{datetime.now(timezone.utc).isoformat()} event runtime started", flush=True)

            while True:
                inbox.drain(state_store, trigger_queue, expected_email)
                now = time.monotonic()
                for trigger in schedule.take_due(now):
                    trigger_queue.enqueue(trigger)

                if subscriber_needs_restart(subscriber_future):
                    if subscriber_future is not None:
                        try:
                            subscriber_future.result(timeout=0)
                        except Exception as exc:
                            print(f"Pub/Sub listener disconnected: {exc}", flush=True)
                    subscriber_future = _start_subscription(subscriber, subscription_name, inbox)
                    print("Pub/Sub listener restarted", flush=True)

                if socket_process.poll() is not None:
                    socket_process = _start_slack_socket()
                    print("Slack Socket listener restarted", flush=True)

                triggers = trigger_queue.begin()
                if triggers:
                    started = time.monotonic()
                    success = True
                    try:
                        if "config_refresh" in triggers:
                            config_rows, config_changed = refresh_config(sheets, sheet_id, state_store)
                            settings = _settings(config_rows)
                            expected_email = settings["gmail_mailbox"].strip()
                            if config_changed:
                                triggers.add("full_reconciliation")
                        if "watch_renewal" in triggers:
                            runtime.renew_watch(topic_name)
                        if "full_reconciliation" in triggers:
                            full_success = runtime.full_reconcile("scheduled_or_config_full")
                            success = full_success and success
                            if not full_success:
                                schedule.retry("full_reconciliation", time.monotonic())
                        if triggers & {"startup", "notification", "history_catchup"}:
                            success = runtime.sync_history("+".join(sorted(triggers))) and success
                        if "maintenance" in triggers and "full_reconciliation" not in triggers:
                            success = time_maintenance() and success
                    finally:
                        trigger_queue.finish()
                    print(
                        f"{datetime.now(timezone.utc).isoformat()} triggers={','.join(sorted(triggers))} "
                        f"status={'ok' if success else 'failed'} duration={time.monotonic() - started:.1f}s",
                        flush=True,
                    )
                    RUNTIME_METRICS.record_trigger(
                        "+".join(sorted(triggers)),
                        duration=time.monotonic() - started,
                        success=success,
                        changed_threads=runtime.last_changed_threads,
                    )
                    print("event_metrics=" + json.dumps(RUNTIME_METRICS.snapshot(), sort_keys=True), flush=True)

                wake_event.wait(timeout=min(60.0, schedule.seconds_until_next(time.monotonic())))
        finally:
            if subscriber_future is not None:
                subscriber_future.cancel()
            subscriber.close()
            if socket_process is not None and socket_process.poll() is None:
                socket_process.terminate()
                socket_process.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
