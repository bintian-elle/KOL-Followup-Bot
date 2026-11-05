# Tiered Gmail Scanning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run incremental mail scans every two minutes and full label reconciliation every seven minutes without losing Reach Out replies.

**Architecture:** `run_bot.py` becomes a two-tier sequential scheduler. `route_reach_out_replies.py` gains a Bot_State checkpoint and only fetches tracked threads with messages after the checkpoint.

**Tech Stack:** Python 3, Gmail API, Google Sheets Bot_State, unittest, systemd

**Spec:** `docs/superpowers/specs/2026-11-04-tiered-gmail-scanning-design.md`

## Global Constraints

- Fast interval is exactly 120 seconds.
- Full interval is exactly 420 seconds.
- Gmail workers execute sequentially.
- A failed scan must not advance its checkpoint.
- Existing Slack routing behavior must not change.

## Review Focus

- Both schedules due together: run one full cycle containing fast work once.
- Long-running cycle: advance deadlines past the current time without tight-loop repetition.
- Reach Out checkpoint absent: inspect all currently tracked threads once.
- Reach Out checkpoint present: inspect only tracked threads with recent messages.
- Gmail failure: checkpoint remains unchanged for retry.

---

### Task 1: Incremental Reach Out Reply Detection

**Files:**
- Modify: `route_reach_out_replies.py`
- Modify: `sheet_state.py`
- Create: `test_route_reach_out_replies.py`

**Interfaces:**
- Produces: `reply_scan_start(checkpoint, scan_started) -> tuple[int, bool]`
- Produces: `candidate_thread_ids(tracked_ids, recent_messages, full_scan) -> set[str]`

- [ ] Write failing tests for first full scan, incremental overlap, and tracked-thread intersection.
- [ ] Run the focused tests and verify the expected failures.
- [ ] Implement the checkpoint helpers and use them in the Gmail scan.
- [ ] Save the checkpoint only after successful thread processing.
- [ ] Run the focused tests and full suite.

### Task 2: Two-tier Sequential Scheduler

**Files:**
- Modify: `run_bot.py`
- Create: `test_run_bot.py`

**Interfaces:**
- Produces: `advance_deadline(deadline, interval, now) -> float`
- Produces: `cycle(full_scan: bool) -> bool`

- [ ] Write failing tests for the 120/420-second constants and deadline advancement.
- [ ] Run the focused tests and verify the expected failures.
- [ ] Split fast and full work while preserving ordering dependencies.
- [ ] Remove concurrent Gmail worker execution.
- [ ] Run the focused tests and full suite.

### Task 3: Deploy and Observe

**Files:**
- Modify: production checkout only through Git fast-forward deployment.

- [ ] Compile all Python files and run the full unit suite.
- [ ] Push the verified commit to `main`.
- [ ] Stop, pull, test, and restart `kol-followup.service` on ANC EC2.
- [ ] Verify the deployed revision, active service, checkpoint creation, and absence of Gmail quota errors across the first fast/full cycles.

