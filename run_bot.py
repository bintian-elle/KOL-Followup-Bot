"""Run incremental Gmail scans every two minutes and full reconciliation every seven."""

import argparse
import fcntl
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
LOCK = ROOT / ".bot-runner.lock"
FAST_INTERVAL_SECONDS = 120
FULL_INTERVAL_SECONDS = 420


def advance_deadline(deadline, interval, now):
    """Advance a monotonic deadline past now without creating catch-up loops."""
    while deadline <= now:
        deadline += interval
    return deadline


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
        stamp = datetime.now(timezone.utc).isoformat()
        print(f"{stamp} {name}: FAILED (exit {return_code})", flush=True)
    return return_code


def cycle(full_scan):
    closure_result = 0
    # A closing label takes priority over every live tracking label. Process it
    # before reading Needs Review or rebuilding Active Track.
    if full_scan:
        closure_result = run_script("process_closed.py")
        if closure_result:
            print("Closure processing failed; skipping this full cycle to preserve Active rows", flush=True)
            return False
    # Observe removals first so the inquiry scanner cannot restore a label
    # that a team member just removed.
    monitor_result = run_script("monitor_needs_review.py")
    if monitor_result:
        print("Needs Review monitor failed; skipping inquiry relabeling this cycle", flush=True)
    # Capture replies while they are still in Reach_Out_Track. Only then may
    # its sync remove those rows from the unanswered list.
    reply_route_result = run_script("route_reach_out_replies.py")
    if reply_route_result:
        print("Reach Out reply routing failed; preserving Reach Out rows this cycle", flush=True)
    reach_result = run_script("sync_reach_out.py") if not reply_route_result else 1
    needs_result = run_script("sync_needs_review.py") if not monitor_result else 1
    fast_success = not any((monitor_result, reply_route_result, reach_result, needs_result))
    if not full_scan:
        return fast_success

    active_result = run_script("sync_active_label.py")
    # Assignment must follow the Active Track rewrite to keep owners on the
    # correct row and to include newly labeled conversations in this cycle.
    if active_result == 0:
        assignment_result = run_script("assign_active.py")
        reminder_result = run_script("remind_overdue.py")
    else:
        print("Active Track sync failed; skipping assignment this cycle", flush=True)
        assignment_result = 1
        reminder_result = 1
    if fast_success and active_result == 0:
        digest_result = run_script("daily_digest.py")
    else:
        print("A source scan failed; skipping Daily Digest to avoid sending stale totals", flush=True)
        digest_result = 1
    return (fast_success and active_result == 0 and assignment_result == 0
            and reminder_result == 0 and digest_result == 0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run one scan cycle and exit")
    args = parser.parse_args()
    socket_process = None
    with LOCK.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another bot runner is already active", flush=True)
            return 1
        try:
            home_result = run_script("publish_slack_home.py")
            if home_result:
                print("Slack Home publish failed; continuing with Gmail scans", flush=True)
            now = time.monotonic()
            next_fast = now
            next_full = now
            while True:
                if not args.once and (socket_process is None or socket_process.poll() is not None):
                    socket_process = subprocess.Popen([sys.executable, str(ROOT / "slack_socket.py")], cwd=ROOT)
                    print(f"{datetime.now(timezone.utc).isoformat()} Slack Socket listener started", flush=True)
                now = time.monotonic()
                full_scan = now >= next_full
                if not full_scan and now < next_fast:
                    time.sleep(min(next_fast, next_full) - now)
                    continue
                started = time.monotonic()
                kind = "full" if full_scan else "fast"
                print(f"{datetime.now(timezone.utc).isoformat()} {kind} cycle started", flush=True)
                success = cycle(full_scan)
                print(f"{datetime.now(timezone.utc).isoformat()} {kind} cycle {'completed' if success else 'had failures'}", flush=True)
                if args.once:
                    return 0 if success else 1
                finished = time.monotonic()
                next_fast = advance_deadline(next_fast, FAST_INTERVAL_SECONDS, finished)
                if full_scan:
                    next_full = advance_deadline(next_full, FULL_INTERVAL_SECONDS, finished)
                print(f"{kind.title()} cycle took {finished - started:.1f}s; next fast scan in {max(0, next_fast - finished):.1f}s; next full scan in {max(0, next_full - finished):.1f}s", flush=True)
        finally:
            if socket_process is not None and socket_process.poll() is None:
                socket_process.terminate()
                socket_process.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
