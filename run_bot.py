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

from assign_active import cell, load_env, slack_call, truth
from gmail_active_labels import resolve_active_labels, resolve_closed_labels
from gmail_event_sync import (ClientRegistry, EventRuntime, PubSubInbox, TriggerQueue,
                              config_fingerprint, subscriber_needs_restart)
from event_sync_metrics import CountingHttp, RuntimeMetrics
from runtime_config import load_config_rows
from runtime_alerts import RuntimeAlerter
from ringconn_followup import MAILBOX as RINGCONN_MAILBOX, STATE_PATH as RINGCONN_STATE_PATH, RingConnFollowup
from sheet_state import SheetState


ROOT = Path(__file__).resolve().parent
LOCK = ROOT / ".bot-runner.lock"
HISTORY_CATCHUP_SECONDS = 30 * 60
FULL_RECONCILIATION_SECONDS = 24 * 60 * 60
CONFIG_REFRESH_SECONDS = 2 * 60 * 60
WATCH_RENEWAL_SECONDS = 24 * 60 * 60
MAINTENANCE_SECONDS = 5 * 60
RUNTIME_METRICS = None


@dataclass(frozen=True)
class EventWorkPlan:
    closed: bool
    review: bool
    mail: bool
    active: bool


def event_work_plan(batch, active_label_ids, review_label_id, closed_label_ids):
    changed_labels = set().union(
        *(batch.labels_added.values()), *(batch.labels_removed.values()),
    )
    active_label_ids = set(active_label_ids)
    closed_label_ids = set(closed_label_ids)
    message_change = bool(batch.messages_added or batch.messages_deleted)
    return EventWorkPlan(
        closed=bool(changed_labels & closed_label_ids),
        review=bool(changed_labels & (active_label_ids | {review_label_id})),
        mail=bool(batch.messages_added),
        active=message_change or bool(changed_labels & active_label_ids),
    )


def health_alert_recipient(settings, config_rows=()):
    developer = next((row for row in config_rows if cell(row, 0) == "Testing developer"), None)
    return (settings.get("error_notification_slack_id", "").strip()
            or (cell(developer, 3) if developer else ""))


def build_runtime_alerter(settings, config_rows, *, recipient=None, state_path=None):
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    recipient = recipient or health_alert_recipient(settings, config_rows)
    if not token or not recipient:
        return None

    def send_text(text):
        channel = slack_call(token, "conversations.open", {"users": recipient})["channel"]["id"]
        slack_call(token, "chat.postMessage", {"channel": channel, "text": text})

    if state_path is not None:
        return RuntimeAlerter(token, recipient, send_text, state_path=state_path)
    return RuntimeAlerter(token, recipient, send_text)


def safe_health_alert(alerter, method, *args):
    if alerter is None:
        return
    try:
        getattr(alerter, method)(*args)
    except Exception as exc:
        print(f"Slack health alert failed: {exc}", flush=True)


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


def compatibility_pipeline(plan=None):
    """Run the established business workers once, in their required order."""
    full = plan is None
    closure_result = run_script("process_closed.py") if full or plan.closed else 0
    if closure_result:
        return False
    monitor_result = run_script("monitor_needs_review.py") if full or plan.review else 0
    route_result = run_script("route_reach_out_replies.py") if full or plan.mail else 0
    reach_result = run_script("sync_reach_out.py") if (full or plan.mail) and not route_result else int(bool(route_result))
    needs_result = run_script("sync_needs_review.py") if (full or plan.mail) and not monitor_result else int(bool(monitor_result))
    active_result = run_script("sync_active_label.py") if full or plan.active else 0
    if (full or plan.active) and active_result == 0:
        assignment_result = run_script("assign_active.py")
    else:
        assignment_result = int(bool(active_result))
    if full and active_result == 0:
        reminder_result = run_script("remind_overdue.py")
    else:
        reminder_result = int(bool(active_result))
    if full and not any((monitor_result, route_result, reach_result, needs_result, active_result)):
        digest_result = run_script("daily_digest.py")
    else:
        digest_result = int(bool(monitor_result or route_result or reach_result or needs_result or active_result))
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
        health_alerter = build_runtime_alerter(settings, config_rows)
        expected_email = settings["gmail_mailbox"].strip()
        topic_name = os.environ["GMAIL_PUBSUB_TOPIC"]
        subscription_name = os.environ["GMAIL_PUBSUB_SUBSCRIPTION"]

        trigger_queue = TriggerQueue()
        wake_event = threading.Event()
        inbox = PubSubInbox(wake_event)
        ringconn = None
        if expected_email.casefold() == RINGCONN_MAILBOX and RINGCONN_STATE_PATH.is_file():
            ringconn_alerter = build_runtime_alerter(
                settings, config_rows, recipient="U0BN7GWSX0S",
                state_path=ROOT / ".ringconn-alert-state.json",
            )
            ringconn = RingConnFollowup(
                sheets, gmail, os.environ["SLACK_BOT_TOKEN"],
                on_health=lambda *args: safe_health_alert(ringconn_alerter, *args),
            )

        def on_full_reconcile():
            mark_full_reconciliation(state_store)
            if ringconn:
                ringconn.dirty = True
        event_labels = {}
        pending_plan = [None]

        def refresh_event_labels():
            labels = gmail.labels().list(userId="me").execute().get("labels", [])
            active = resolve_active_labels(labels, config_rows)
            closed = resolve_closed_labels(labels, config_rows)
            event_labels.update({
                "active": set(active["active_ids"]),
                "review": active["review_id"],
                "closed": set(closed.values()),
            })

        def on_history(batch):
            pending_plan[0] = event_work_plan(
                batch, event_labels["active"], event_labels["review"], event_labels["closed"],
            )
            print(
                "event_plan=" + json.dumps({
                    "threads": len(batch.thread_ids),
                    **pending_plan[0].__dict__,
                }, sort_keys=True),
                flush=True,
            )
            if ringconn:
                ringconn.on_history(batch)

        def event_pipeline():
            plan = pending_plan[0]
            pending_plan[0] = None
            return compatibility_pipeline(plan)

        refresh_event_labels()
        runtime = EventRuntime(
            gmail, state_store, event_pipeline,
            on_full_reconcile=on_full_reconcile,
            on_history=on_history,
        )
        runtime_state = state_store.load("gmail_event_runtime", {}) or {}
        schedule = RuntimeSchedule.restored_at(time.monotonic(), time.time(), runtime_state)
        socket_process = None
        subscriber_future = None
        retrying = set()
        try:
            run_script("publish_slack_home.py")
            socket_process = _start_slack_socket()
            subscriber_future = _start_subscription(subscriber, subscription_name, inbox)
            runtime.renew_watch(topic_name)
            trigger_queue.enqueue("startup")
            print(f"{datetime.now(timezone.utc).isoformat()} event runtime started", flush=True)

            while True:
                inbox.drain(state_store, trigger_queue, expected_email)
                if ringconn:
                    ringconn.tick()
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
                    failed = set()
                    try:
                        if "config_refresh" in triggers:
                            config_rows, config_changed = refresh_config(sheets, sheet_id, state_store)
                            settings = _settings(config_rows)
                            health_alerter = build_runtime_alerter(settings, config_rows)
                            expected_email = settings["gmail_mailbox"].strip()
                            if config_changed:
                                refresh_event_labels()
                                triggers.add("full_reconciliation")
                        if "watch_renewal" in triggers:
                            runtime.renew_watch(topic_name)
                        if "full_reconciliation" in triggers:
                            full_success = runtime.full_reconcile("scheduled_or_config_full")
                            if not full_success:
                                failed.add("full_reconciliation")
                        if triggers & {"startup", "notification", "history_catchup"}:
                            if not runtime.sync_history("+".join(sorted(triggers))):
                                failed.add("history_catchup")
                            else:
                                retrying.discard("history_catchup")
                        if "maintenance" in triggers and "full_reconciliation" not in triggers:
                            if not time_maintenance():
                                failed.add("maintenance")
                    finally:
                        trigger_queue.finish()
                    completed = triggers - failed
                    if "full_reconciliation" in failed:
                        completed.discard("maintenance")
                    retrying.difference_update(completed)
                    retrying.update(failed)
                    for trigger in failed:
                        schedule.retry(trigger, time.monotonic(), delay=60)
                    success = not failed
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
                    if not retrying:
                        safe_health_alert(health_alerter, "recovered")
                    elif failed:
                        safe_health_alert(
                            health_alerter, "failure", "business synchronization",
                            "One or more Gmail, Sheet, assignment, reminder, or digest workers failed.",
                        )

                wake_limit = 15.0 if ringconn else 60.0
                wake_event.wait(timeout=min(wake_limit, schedule.seconds_until_next(time.monotonic())))
        except Exception as exc:
            safe_health_alert(
                health_alerter, "failure", "service",
                f"{type(exc).__name__}: {exc}",
            )
            raise
        finally:
            if subscriber_future is not None:
                subscriber_future.cancel()
            subscriber.close()
            if socket_process is not None and socket_process.poll() is None:
                socket_process.terminate()
                socket_process.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
