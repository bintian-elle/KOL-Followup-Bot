# Gmail Event Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace recurring Gmail polling with Gmail Watch, Pub/Sub StreamingPull, and a durable History API cursor while preserving existing KOL workflow results.

**Architecture:** A long-lived event runtime reuses API clients, coalesces Pub/Sub notifications, drains Gmail history serially, and invokes the compatibility business pipeline only when changes exist. Thirty-minute history catch-up and 24-hour full reconciliation provide recovery; Config reloads every two hours.

**Tech Stack:** Python 3.11, Gmail API, Google Sheets API, Google Cloud Pub/Sub, systemd, unittest.

**Spec:** `docs/superpowers/specs/2026-11-11-gmail-event-sync-design.md`

## Global Constraints

- Notification processing must be idempotent and serialized.
- Commit a Gmail history cursor only after the ordered business batch succeeds.
- Use 30 minutes for catch-up, 24 hours for full reconciliation, 2 hours for Config reload, and one day for watch renewal.
- Do not expose a public webhook or add an inbound EC2 port.
- Preserve Slack Socket Mode responsiveness and existing business worker order.
- Real Gmail and Sheet mutations stay within the bot's existing authorization.

## Review Focus

- Duplicate or out-of-order Pub/Sub notifications must not repeat Gmail/Sheet/Slack side effects.
- A notification acknowledged before a worker crash must still be recovered from the unchanged Gmail history cursor.
- Gmail history 404 must produce one safe full reconciliation and a fresh cursor.
- Bot-originated label changes must not cause an infinite notification loop.
- Daily Digest and 24-hour reminders must still run on schedule despite the 24-hour reconciliation interval.

---

### Task 1: Durable history and notification model

**Files:**
- Create: `gmail_event_sync.py`
- Create: `test_gmail_event_sync.py`
- Modify: `sheet_state.py`

**Interfaces:**
- Produces: `HistoryBatch`, `collect_history()`, `dedupe_notification_ids()`, `HistoryCursorExpired`.

- [ ] Write failing tests for message/label event collection, pagination, duplicate records, notification dedupe, and HTTP 404 cursor expiry.
- [ ] Run the targeted tests and verify failures are caused by missing event-sync code.
- [ ] Implement the minimal durable history model and add Bot_State namespaces.
- [ ] Run targeted and full tests.
- [ ] Commit the task.

### Task 2: Long-lived runtime scheduler and ordered compatibility pipeline

**Files:**
- Modify: `gmail_event_sync.py`
- Replace: `run_bot.py`
- Modify: `test_run_bot.py`
- Modify: `requirements.txt`

**Interfaces:**
- Consumes: `HistoryBatch`, `collect_history()`.
- Produces: `EventRuntime`, `RuntimeSchedule`, serialized trigger handling, compatibility worker pipeline.

- [ ] Write failing tests for 30-minute catch-up, 24-hour reconciliation, 2-hour Config refresh, daily watch renewal, event coalescing, serialization, no-change idle behavior, and reminder/digest scheduling.
- [ ] Run tests and confirm the expected failures.
- [ ] Implement shared clients, scheduling, queueing, and compatibility pipeline triggering.
- [ ] Run targeted and full tests.
- [ ] Commit the task.

### Task 3: Pub/Sub StreamingPull and recovery

**Files:**
- Modify: `gmail_event_sync.py`
- Modify: `run_bot.py`
- Modify: `test_gmail_event_sync.py`
- Modify: `README.md`
- Modify: `deploy/kol-followup.service`

**Interfaces:**
- Consumes: runtime trigger queue.
- Produces: authenticated subscription callback, watch renewal, reconnect behavior, health logging.

- [ ] Write failing tests for valid/invalid notification payloads, duplicate message IDs, subscriber disconnect, watch expiration, restart cursor recovery, and acknowledgement timing.
- [ ] Run tests and confirm the expected failures.
- [ ] Implement StreamingPull, Gmail watch management, bounded retries, and deployment documentation.
- [ ] Run targeted and full tests.
- [ ] Commit the task.

### Task 4: Migration, metrics, and production acceptance

**Files:**
- Create: `event_sync_metrics.py`
- Create: `test_event_sync_metrics.py`
- Modify: `gmail_event_sync.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: runtime events and API wrapper counters.
- Produces: comparable CPU/memory/API/process metrics and migration bootstrap.

- [ ] Write failing tests for metrics aggregation and first-run/full-recovery cursor bootstrap.
- [ ] Run tests and confirm the expected failures.
- [ ] Implement metrics, bootstrap, and acceptance logging.
- [ ] Run the entire suite, compile all Python files, and run diff checks.
- [ ] Commit, self-review the whole branch, deploy, and observe notification/catch-up/full schedules without unauthorized Gmail mutations.
