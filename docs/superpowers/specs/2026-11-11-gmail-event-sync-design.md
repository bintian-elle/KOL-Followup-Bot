# Gmail Event Sync Design

## Goal

Replace the fixed two-minute Gmail polling and seven-minute reconciliation loop with a reliable event-driven Gmail synchronization service while preserving the existing business ordering, Gmail labels, Sheet output, Slack behavior, and manual-override semantics.

## Runtime model

- One long-lived `run_bot.py` process owns reusable Gmail, Sheets, and Pub/Sub clients.
- Gmail `users.watch` publishes mailbox-change notifications to a Cloud Pub/Sub topic.
- EC2 receives notifications with a StreamingPull subscription, so no public webhook or inbound port is required.
- Notifications only wake the worker. The durable Gmail `historyId` cursor is the source of truth.
- Notifications arriving close together are coalesced; one worker drains all history pages from the committed cursor.
- A single serialized processing queue preserves the existing business order and prevents concurrent Gmail/Sheet writers.

## Timing

- Pub/Sub notification: process as soon as it is received and coalesced.
- History safety catch-up: every 30 minutes, even when no notification arrives.
- Full Gmail/Sheet reconciliation: every 24 hours.
- Config reload: every 2 hours, plus once at startup.
- Gmail watch renewal: once per day and whenever the stored expiration is near.
- Slack Socket Mode remains continuously connected and immediate.
- Time-based 24-hour reply reminders and Daily Digest remain scheduled independently; the scheduler must not wait 24 hours to run them.

## Durable state and delivery semantics

`Bot_State` stores the committed Gmail history cursor, watch expiration, notification receipts, and runtime timestamps. A history cursor advances only after the entire ordered processing batch succeeds. Duplicate Pub/Sub messages and repeated history records are idempotent. Pub/Sub is acknowledged after the notification is durably queued; failure to process leaves the Gmail cursor unchanged, so the next notification or 30-minute catch-up replays the changes.

If Gmail returns HTTP 404 for an expired `startHistoryId`, run the existing full reconciliation, obtain a current mailbox history ID, and resume incremental processing. The service also performs a full reconciliation on first migration before committing its initial cursor.

## Processing scope and ordering

History records collect affected Gmail `threadId` values and label-add/remove facts. The worker coalesces them and runs the existing business pipeline in this order:

1. Completed and Not Moving Forward processing.
2. Needs Review departures and moves to Active.
3. Upfluence Reach Out first replies.
4. New outbound Reach Out synchronization.
5. New inbound Needs Review classification.
6. Active Track synchronization.
7. Member-label manual overrides.
8. Assignment, member Gmail labels, and Slack notifications.
9. Overdue reminders and Daily Digest checks.

The first implementation may invoke existing workers as a compatibility pipeline after a real history change, but idle operation must not repeatedly launch them. Long-lived Gmail, Sheets, and Pub/Sub clients handle watch/history/config orchestration; full reconciliation is retained as a recovery path.

## Configuration

Add environment variables for the Google Cloud project, fully qualified Pub/Sub topic, and subscription. The Pub/Sub topic must be in the same Google Cloud project as the Gmail OAuth client used for `watch`. The Sheets service account must have the Pub/Sub subscriber permissions needed by the EC2 process. Gmail's publishing service account must have topic publisher permission.

Config changes are not Gmail events. Cache Config in memory, refresh it every two hours, and refresh immediately after startup. A changed Config fingerprint is recorded and applied to subsequent batches; label-map changes trigger a reconciliation.

## Recovery and observability

- Renew watch daily; retry transient failures with bounded exponential backoff.
- Restart StreamingPull after disconnects without changing the history cursor.
- Record Pub/Sub message IDs for diagnostic deduplication with bounded retention.
- Log trigger type, history range, affected messages/threads, pipeline duration, API calls, and outcome.
- Emit a periodic health line containing last notification, last successful history sync, last full reconciliation, next watch renewal, and next Config refresh.
- Keep the systemd restart policy.

## Acceptance

Automated tests cover new mail, replies, label additions/removals, duplicate notifications, lost notifications recovered by catch-up, restart from a stored cursor, Config refresh, watch renewal, expired cursor recovery, and serialized processing. A production smoke test must not create or alter real email beyond the currently authorized bot behavior. Compare before/after idle and changed-event CPU, memory, process count, and Gmail/Sheets API request counts.
