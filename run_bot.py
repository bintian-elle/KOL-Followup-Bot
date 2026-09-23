"""Run Gmail, Sheets, assignment, and Slack reminder scans every minute."""

import argparse
import fcntl
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent
LOCK = ROOT / ".bot-runner.lock"
INTERVAL_SECONDS = 60


def run_script(name):
    result = subprocess.run([sys.executable, str(ROOT / name)], cwd=ROOT, capture_output=True, text=True)
    stamp = datetime.now(timezone.utc).isoformat()
    for line in (result.stdout + result.stderr).splitlines():
        print(f"{stamp} {name}: {line}", flush=True)
    if result.returncode:
        print(f"{stamp} {name}: FAILED (exit {result.returncode})", flush=True)
    return result.returncode


def cycle():
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
    scans = ["sync_active_label.py"]
    if not reply_route_result:
        scans.append("sync_reach_out.py")
    if not monitor_result:
        scans.append("sync_needs_review.py")
    with ThreadPoolExecutor(max_workers=len(scans)) as pool:
        results = list(pool.map(run_script, scans))
    # Assignment must follow the Active Track rewrite to keep owners on the
    # correct row and to include newly labeled conversations in this cycle.
    if results[0] == 0:
        assignment_result = run_script("assign_active.py")
        reminder_result = run_script("remind_overdue.py")
    else:
        print("Active Track sync failed; skipping assignment this cycle", flush=True)
        assignment_result = 1
        reminder_result = 1
    if monitor_result == 0 and reply_route_result == 0 and all(result == 0 for result in results):
        digest_result = run_script("daily_digest.py")
    else:
        print("A source scan failed; skipping Daily Digest to avoid sending stale totals", flush=True)
        digest_result = 1
    return (monitor_result == 0 and reply_route_result == 0 and all(result == 0 for result in results)
            and assignment_result == 0 and reminder_result == 0 and digest_result == 0)


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
            while True:
                if not args.once and (socket_process is None or socket_process.poll() is not None):
                    socket_process = subprocess.Popen([sys.executable, str(ROOT / "slack_socket.py")], cwd=ROOT)
                    print(f"{datetime.now(timezone.utc).isoformat()} Slack Socket listener started", flush=True)
                started = time.monotonic()
                print(f"{datetime.now(timezone.utc).isoformat()} cycle started", flush=True)
                success = cycle()
                print(f"{datetime.now(timezone.utc).isoformat()} cycle {'completed' if success else 'had failures'}", flush=True)
                if args.once:
                    return 0 if success else 1
                elapsed = time.monotonic() - started
                if elapsed >= INTERVAL_SECONDS:
                    print(f"Cycle took {elapsed:.1f}s; starting next cycle immediately", flush=True)
                else:
                    time.sleep(INTERVAL_SECONDS - elapsed)
        finally:
            if socket_process is not None and socket_process.poll() is None:
                socket_process.terminate()
                socket_process.wait(timeout=10)


if __name__ == "__main__":
    raise SystemExit(main())
