# Bluevua KOL Follow-up Bot

The bot monitors Gmail, keeps the Bluevua KOL tracking tabs current, assigns
ongoing work, and sends Slack notifications and daily summaries.

## Main workflow

- Receive Gmail mailbox changes through Gmail Watch and Pub/Sub StreamingPull.
- Process message and label changes from a durable Gmail `historyId` cursor.
- Run a History API safety catch-up every 30 minutes and a complete Gmail/Sheet
  reconciliation every 24 hours.
- Cache the Sheet `Config` tab and refresh it every 2 hours. Bot-written round
  robin changes update the local cache immediately.
- Keep `Bluevua_KOL_Active_Track` synchronized with the Active parent label and
  configured member child labels.
- Give member child labels priority over round robin Sheet assignments.
- Keep unanswered Upfluence outreach in `Reach_Out_Track`.
- Store the last successful Reach Out scan in `Bot_State` and search only new
  outbound mail after that checkpoint, with a five-minute overlap.
- Move the first human reply to an Upfluence outreach directly into the
  `Bluevua_KOL_Active/02_Upfluence Replied` marker label for assignment, without a
  separate first-reply Slack alert.
- Move threads labeled `03_Completed` or `04_Not Moving Forward` out of Active
  tracking and record the outcome in `Closed_Track`.
- Route eligible unanswered inbound inquiries to `01_Needs Review`.
- Assign new Active tasks in Config round robin order.
- Add the assigned member's Gmail label once. Later manual label changes take
  priority and are not automatically reversed.
- Send one reminder when a `Needs Reply` task passes 24 hours.
- Send the Daily Digest at `daily_send_time` in the configured `timezone`.
- Handle Slack DM commands immediately over Socket Mode.

## Slack commands

- `task`: list the requesting member's assigned tasks and Gmail links.
- `summary`: show current totals and the working Sheet link.
- `rm`: remove the complete Bluevua Active label family from all current Needs
  Review threads. Only Shanshan and Candice are authorized. It never deletes
  email.

See [SLACK_COMMANDS.md](SLACK_COMMANDS.md) for Slack app configuration and
routing details.

## Configuration

Runtime behavior is configured in the Google Sheet `Config` tab. Secrets are
loaded from `.env`; Google credential files belong in `credentials/`. Both are
excluded from Git.

Create the environment file from `.env.example`, then install dependencies:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Run one complete scan cycle:

```bash
.venv/bin/python run_bot.py --once
```

Run the continuous worker used on EC2:

```bash
.venv/bin/python run_bot.py
```

The systemd unit template is in `deploy/kol-followup.service`.

## Gmail Watch and Pub/Sub

The continuous worker requires a Pub/Sub topic and pull subscription in the
same Google Cloud project as the Gmail OAuth client. EC2 uses StreamingPull, so
it does not expose an HTTPS webhook or require an inbound firewall port.

Create the resources and permissions once:

```bash
gcloud services enable pubsub.googleapis.com gmail.googleapis.com --project PROJECT_ID
gcloud pubsub topics create kol-followup-gmail --project PROJECT_ID
gcloud pubsub subscriptions create kol-followup-gmail-ec2 \
  --topic kol-followup-gmail --project PROJECT_ID
gcloud pubsub topics add-iam-policy-binding kol-followup-gmail \
  --project PROJECT_ID \
  --member serviceAccount:gmail-api-push@system.gserviceaccount.com \
  --role roles/pubsub.publisher
gcloud pubsub subscriptions add-iam-policy-binding kol-followup-gmail-ec2 \
  --project PROJECT_ID \
  --member serviceAccount:YOUR_BOT_SERVICE_ACCOUNT \
  --role roles/pubsub.subscriber
```

Set `GMAIL_PUBSUB_TOPIC` and `GMAIL_PUBSUB_SUBSCRIPTION` in `.env` to the
fully-qualified resource names. The bot renews Gmail Watch at startup and once
per day. Pub/Sub notifications only wake the worker; `history.list` remains the
source of truth, so duplicate or lost notifications are safe. An expired
history cursor automatically triggers a complete reconciliation.

Each processed batch writes an `event_metrics` JSON log containing process
uptime, resident memory, Gmail/Sheets requests made by the long-lived runtime,
compatibility worker launches, trigger counts, failures, affected threads, and
processing time. Use these lines together with `systemd-cgtop` or `systemctl
status` to compare idle and changed-mail behavior before and after migration.

## Runtime state

Delivery receipts, the committed Gmail history cursor, Watch expiration,
notification receipts, Needs Review suppression, and Daily Digest snapshots are
stored in the visible `Bot_State` Sheet tab. This lets the EC2 worker restart
without skipping mailbox history or repeating completed notifications.
