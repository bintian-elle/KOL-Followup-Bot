# Bluevua KOL Follow-up Bot

The bot monitors Gmail, keeps the Bluevua KOL tracking tabs current, assigns
ongoing work, and sends Slack notifications and daily summaries.

## Main workflow

- Scan Gmail every minute.
- Keep `Bluevua_KOL_Active_Track` synchronized with the Active parent label and
  configured member child labels.
- Give member child labels priority over round robin Sheet assignments.
- Keep unanswered Upfluence outreach in `Reach_Out_Track`.
- Move the first human reply to an Upfluence outreach directly into the
  `Bluevua_KOL_Active/Upfluence Reply` marker label for assignment, without a
  separate first-reply Slack alert.
- Route eligible unanswered inbound inquiries to `01_Needs Review`.
- Assign new Active tasks in Config round robin order.
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

## Runtime state

Delivery receipts, Gmail scan checkpoints, Needs Review suppression, and Daily
Digest snapshots are stored in the visible `Bot_State` Sheet tab. This lets the
EC2 worker restart without repeating completed notifications.
