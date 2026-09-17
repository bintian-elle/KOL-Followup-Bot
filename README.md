# KOL Follow-up Automation

This worker detects the first external reply in Gmail threads selected by a
strict Upfluence Gmail query, appends the KOL to Google Sheets, assigns one of
three owners in round-robin order, and sends the owner a Slack task.

Slack has two independent notification flows:

- Send each eligible reply to its round-robin owner immediately.
- At 09:00 America/Los_Angeles on working days, send Candice one summary of
  assignments accumulated since the preceding digest. Weekend assignments
  roll into the next working-day summary. `pilot_mode` controls this summary
  destination; it does not suppress owner task notifications.

During testing, the active `Testing Channel` row in Sheet Config routes both
flows exclusively to that channel. Developer DM environment overrides are ignored.
When the testing row is inactive, normal owner/digest routing resumes.
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
