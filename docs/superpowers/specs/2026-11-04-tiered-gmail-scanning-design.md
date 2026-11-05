# Tiered Gmail Scanning Design

## Intent

Keep new inbound mail responsive while reducing Gmail API quota pressure caused by repeated full-label and full-thread scans.

## Selected approach

- Run incremental new-mail work every 120 seconds.
- Run full Gmail-label reconciliation every 420 seconds.
- Execute Gmail workers sequentially so they do not compete for the same per-user quota.
- Store a successful checkpoint for Reach Out reply detection and query only messages after that checkpoint, with the existing five-minute safety overlap.
- A full cycle includes the fast work, so the same worker is not run twice when both schedules are due.

## Fast work (120 seconds)

1. Monitor Needs Review label departures.
2. Detect new replies or bounces for Reach Out threads using an incremental checkpoint.
3. Scan newly sent Upfluence outreach using its existing checkpoint.
4. Scan new Needs Review candidates using its existing checkpoint.

## Full work (420 seconds)

1. Process Completed and Not Moving Forward labels first.
2. Run all fast work.
3. Rebuild Active Track from the Active label family.
4. Apply assignments, member labels, reminders, and the Daily Digest check.

## Safety

- Reach Out reply routing always runs before Reach Out Track rewriting.
- Failed scans do not advance their checkpoint.
- Each checkpoint uses a five-minute overlap to tolerate Gmail indexing delays and clock boundaries.
- Slack Socket Mode stays continuously connected and is independent of scan frequency.

## Alternatives considered

- Run everything every two minutes: simple, but retains the quota-heavy full scans.
- Run the current full cycle every seven minutes: reduces quota use but delays new inbound mail.
- Gmail push notifications and History API: best long-term efficiency, but substantially larger than this operational fix.

