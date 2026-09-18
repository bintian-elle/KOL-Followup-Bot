# KOL Follow-up Automation

Overdue reminders use `campaign_name_contains` against case-insensitive subject
and parsed text/HTML body across the conversation, not Gmail label names.
Recently active threads throughout Gmail (including spam/archive) are checked
after `reply_cutoff_date`. A short reply can inherit brand context from earlier
mail. First-reach-out restrictions do not apply to reminders: an existing
brand conversation may still have an overdue unanswered message. Threshold,
pilot/owner routing, testing mirrors and episode deduplication are unchanged.

Reminders require an established exchange: a human inbound followed by an
actual message in our SENT mailbox, then a subsequent human inbound. Cold
outreach alone and drafts do not enroll a thread. The new policy records its
activation on the first production reminder run in `.unreplied-reminders.sqlite3`;
earlier inbound mail never starts a clock, but historical exchanges establish
eligibility. Preserve this database across updates/restarts. Each overdue round
is delivered once; a fresh inbound after the delivered round starts a new clock,
even if no outbound answer intervened. Our outbound answer clears the pending
clock. Dry runs do not initialize activation or alter deduplication state.

## Prospective Bluevua handoffs

Config `brand_team_emails`, `forwarding_mailbox`, and the timezone-aware ISO
`brand_handoff_enabled_at` enable the additional source only for newly received
handoffs/auto-forwarded creator inquiries. Never move the activation timestamp
backwards to backfill old mail. There is no 24-hour waiting rule or exclusion
based on Mel/Jeremy replying within 24 hours.

`brand_handoff_keywords` and `brand_handoff_exclude_keywords` are configurable.
The worker reads the full thread, extracts the creator instead of the brand
sender, checks related Gmail history and existing Queue identities, and never
reopens an old handoff. Unverifiable forwarded bodies or ambiguous identities
are routed to Needs Review, not confirmed first reach out. Original inquiry
dates enforce `reply_cutoff_date`; Gmail receipt time controls activation and
incremental scanning. Relevant historical messages are read for verification
only, never backfilled by this source.

This worker detects the first external reply in Gmail threads selected by a
strict Upfluence Gmail query, appends the KOL to Google Sheets, assigns one of
three owners in round-robin order, and sends the owner a Slack task.

Slack has two independent notification flows:

- Send each eligible reply to its round-robin owner immediately.
- At 09:00 America/Los_Angeles on working days, send Candice one summary of
  assignments accumulated since the preceding digest. Weekend assignments
  roll into the next working-day summary. `pilot_mode` controls this summary
  destination; it does not suppress owner task notifications.

The active `Testing Channel` row in Sheet Config mirrors both flows to that
channel in addition to normal owner/digest delivery. Developer DM environment
overrides are ignored. An inactive testing row disables only the extra copy.
The Queue still records the actual owner.
Use `--force-digest` to send the current daily summary immediately even when
today's digest has already been recorded.

## Safety and behavior

- Gmail access is read-only.
- The worker reads owners, source rules, timezone, cutoff and checkpoints from
  `KOL Followup Config`. `GMAIL_UPFLUENCE_QUERY` is an optional override; the
  default scan starts from the Sheet's Gmail checkpoint with a safe overlap.
- For an outbound/Upfluence thread, the event is the first human message whose
  `In-Reply-To` points directly to the RFC `Message-ID` of our first sent
  invitation. Replies to later campaign follow-ups, auto-replies and bounces
  are excluded. Missing threading headers are routed to `Needs Review`.
- `first_email_only` applies only to outbound/Upfluence conversations. When
  `gmail_include_inbound_initiated` is enabled, an externally initiated Gmail
  conversation can continue to the separate Bluevua relevance classifier
  without an outbound anchor.
- `Dedup Key` (`gmail_message:<id>`) prevents duplicate initial assignments.
  Later conversation replies never generate Reactivated tasks. Production
  scans always enforce the first-outreach rule regardless of first_email_only.
- Assignment continues from `last_round_robin_owner` in Config.
- First reply is a hard history boundary: only the earliest human external
  reply after initial outreach is considered, including history before the
  polling checkpoint/cutoff. If it replies to a follow-up, the thread is
  excluded; later replies cannot qualify even if they reference the initial
  invitation. Missing headers on the first reply remain Needs Review, never
  permitting a later message to replace it. Queue absence does not imply a
  new conversation. Inbound-initiated handling remains separately configured.
- Assigned rows record notification state and any Slack error in the Queue.
- Plain-text and HTML-only email bodies are parsed; attachments remain ignored.
- Every candidate is checked against `reply_cutoff_date`, not only the query.
- Editable keyword lists live in Config: `other_brand_keywords`,
  `completed_collab_keywords`, and `generic_collab_keywords`.
- A non-blocking process lock prevents concurrent workers.
- Slack task and digest retries reuse deterministic `client_msg_id` values.
- Gmail rate-limit responses use bounded exponential backoff.
- Run only one worker instance at a time.

## Setup

1. Copy `.env.example` values into the existing `.env` (never commit it).
2. Create a Google Cloud service account, enable Google Sheets API, download
   its JSON key, and share the target Sheet with the service account email as
   Editor. Put the JSON under `credentials/` and set a project-relative path,
   such as `credentials/google-service-account.json`, in
   `GOOGLE_SERVICE_ACCOUNT_FILE`. Relative paths are resolved from this
   project's directory, including when cron or systemd uses another working
   directory.
3. Maintain team order, Slack Member IDs and business rules in the existing
   `KOL Followup Config` tab.
4. Create a Slack app with `chat:write`, install it, and set
   `SLACK_BOT_TOKEN`. An incoming webhook is also supported as a fallback.
5. Install and test:

   ```bash
   python3 -m pip install -r requirements.txt
   python3 -m unittest -v
   python3 kol_followup.py --dry-run
   ```

6. After reviewing dry-run output, set `DRY_RUN=false` and run again. On EC2,
   use the continuous monitor below; no cron or systemd timer is needed.

## Continuous EC2 monitor

Run `.venv/bin/python kol_followup.py --monitor` to scan immediately, then wait
300 seconds after each completed or failed scan. Scans never overlap. Sheet
Config is reloaded every cycle; failures are logged and retried next cycle.
SIGTERM/SIGINT finish the current scan and exit, or wake the idle wait immediately.
Single-run commands and `--dry-run` remain supported. Do not use `--force-digest`
in monitor mode. Only one machine should monitor this mailbox.

For Ubuntu deployed at `/home/ubuntu/KOL-Followup-Bot`, install the included service:

```bash
sudo cp deploy/kol-followup.service /etc/systemd/system/kol-followup.service
sudo systemctl daemon-reload
sudo systemctl enable --now kol-followup.service
sudo journalctl -u kol-followup.service -f
```

Adjust User and paths for other EC2 login users. Disable any old timer before
enabling this service. Stop monitoring with `sudo systemctl stop kol-followup.service`.

## Sheet contract

The worker validates the existing workbook before writing. `KOL Followup Queue`
must have its 31-column schema on row 5, `KOL Followup Audit Log` must have its
12-column schema on row 5, and `KOL Followup Config` supplies active owners and
settings. Eligible assignments write Queue and Audit rows. Ignored messages write
only Audit rows, whose Details retain the Gmail message ID for deduplication.
Queue is sorted by reply time descending and Audit by event time descending;
blue horizontal borders separate dates after each completed run.
Successful runs update `last_round_robin_owner` and
`last_gmail_successful_run_at` in Config.
