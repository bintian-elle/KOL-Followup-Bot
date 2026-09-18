"""Independent content-scoped overdue reply reminders; no Queue mutation."""
import html
import sqlite3
from datetime import datetime, timezone
import time
from pathlib import Path


def pending_reply(thread, mailbox, brand, cutoff_ms=None, enabled_ms=0, notified_ms=0):
    from kol_followup import timestamp, header_map, addresses, is_automated_message, message_text
    brand = brand.strip().casefold()
    if not brand:
        return None
    # Conversation context counts: a short subsequent reply need not repeat
    # the campaign name. Labels alone are never evidence of relevance.
    relevant = any(brand in (header_map(m).get('subject', '') + '\n' + message_text(m)).casefold()
                   for m in thread.get('messages', [])
                   if not set(m.get('labelIds', [])) & {'DRAFT', 'TRASH'})
    if not relevant:
        return None
    pending = None
    pending_since = None
    latest_activity = None
    seen_inbound = False
    has_answered = False
    for message in sorted(thread.get('messages', []), key=timestamp):
        labels = set(message.get('labelIds', []))
        if labels & {'DRAFT', 'TRASH'}:
            continue
        if 'SENT' in labels:
            # An initial cold outreach is not an answer. Require a preceding
            # human inbound message before enrolling this conversation.
            if seen_inbound:
                has_answered = True
            latest_activity = timestamp(message)
            pending = pending_since = None
            continue
        headers = header_map(message)
        sender = addresses(headers.get('from', ''))
        if not sender or sender[0][1].lower() == mailbox.lower():
            continue
        if is_automated_message(headers, sender[0][1]):
            continue
        latest_activity = timestamp(message)
        seen_inbound = True
        if not has_answered or timestamp(message) < enabled_ms or timestamp(message) <= notified_ms:
            continue
        if pending_since is None:
            pending_since = timestamp(message)
        pending = message
    if cutoff_ms is not None and (latest_activity is None or latest_activity < cutoff_ms):
        return None
    return (pending, pending_since) if pending else None


def initialize_state(db, now_ms):
    """Persist the new policy's activation once, including upgrades from v1."""
    db.execute('CREATE TABLE IF NOT EXISTS sent (key TEXT PRIMARY KEY, ts TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS reminder_meta (key TEXT PRIMARY KEY, value INTEGER)')
    db.execute('CREATE TABLE IF NOT EXISTS reminder_rounds (thread TEXT PRIMARY KEY, notified_ms INTEGER)')
    db.execute('INSERT OR IGNORE INTO reminder_meta VALUES (?, ?)', ('answered_policy_enabled_ms', now_ms))
    db.commit()
    return db.execute('SELECT value FROM reminder_meta WHERE key=?', ('answered_policy_enabled_ms',)).fetchone()[0]


def check_reminders(gmail, store, owners, settings, now, dry_run=False, preview=None):
    from kol_followup import SlackNotifier, project_path, LOG, header_map, addresses, timestamp
    raw = settings.get('unreplied_reminder_hours', '').strip()
    if not raw:
        return 0
    hours = float(raw)
    if hours <= 0:
        return 0
    brand = settings.get('campaign_name_contains', '').strip().casefold()
    if not brand:
        return 0
    # Inclusive midnight in the configured business timezone, not Gmail's
    # default timezone. The query bounds discovery; full-thread validation
    # excludes recent drafts/automated mail from reopening old conversations.
    cutoff = datetime.strptime(settings.get('reply_cutoff_date', '08/20/2026'), '%m/%d/%Y').replace(tzinfo=now.tzinfo)
    cutoff_ms = int(cutoff.timestamp() * 1000)
    # Discover all recently active threads. Searching Gmail for a brand token
    # could miss substring matches or short replies in an older brand thread.
    queue = store.queue_rows()
    # Latest assignment wins; row position is not authoritative after sorting.
    queue = sorted(queue, key=lambda r: datetime.strptime(r[9], '%m/%d/%Y %H:%M')
                   if len(r)>9 and r[9] else datetime.min)
    owner_by_thread = {r[29]: r[7] for r in queue if len(r)>29 and r[7] and r[8] in {'Assigned', 'Reassigned'}}
    users = {o.name: o.slack_user_id for o in owners}
    pilot = settings.get('pilot_recipient_slack_id', '')
    if not pilot:
        raise ValueError('pilot_recipient_slack_id required for overdue reminders')
    notifier = None if dry_run else SlackNotifier(
        __import__('os').getenv('SLACK_BOT_TOKEN', ''), testing_channel_id=(
            settings.get('testing_channel_id','') if settings.get('testing_channel_active','').upper()=='TRUE' else ''))
    db = None
    enabled_ms = int(now.timestamp() * 1000)
    if not dry_run:
        db = sqlite3.connect(project_path('.unreplied-reminders.sqlite3'))
        enabled_ms = initialize_state(db, enabled_ms)
    elif Path(project_path('.unreplied-reminders.sqlite3')).exists():
        # Preview must not initialize activation, schema or notification state.
        db = sqlite3.connect(Path(project_path('.unreplied-reminders.sqlite3')).as_uri() + '?mode=ro', uri=True)
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'reminder_meta' in tables and 'reminder_rounds' in tables:
            row = db.execute('SELECT value FROM reminder_meta WHERE key=?', ('answered_policy_enabled_ms',)).fetchone()
            if row:
                enabled_ms = row[0]
        else:
            db.close()
            db = None
    query = f'in:anywhere after:{max(int(cutoff.timestamp()), enabled_ms // 1000) - 1}'
    count = 0
    seen = set()
    try:
        page = None
        while True:
            found = gmail._execute(gmail.api.users().threads().list(
                userId='me', q=query, maxResults=100, pageToken=page))
            for item in found.get('threads', []):
                if item['id'] in seen:
                    continue
                seen.add(item['id'])
                thread = gmail._execute(gmail.api.users().threads().get(userId='me',id=item['id'],format='full'))
                time.sleep(max(0, float(__import__('os').getenv('GMAIL_THREAD_INTERVAL_SECONDS','2.5'))))
                notified = db.execute('SELECT notified_ms FROM reminder_rounds WHERE thread=?', (item['id'],)).fetchone() if db else None
                pending = pending_reply(thread, gmail.mailbox, brand, cutoff_ms, enabled_ms, notified[0] if notified else 0)
                if not pending:
                    continue
                message, since = pending
                age = (now.timestamp()-since/1000)/3600
                if age < hours:
                    continue
                headers = header_map(message)
                sender = addresses(headers.get('from',''))[0][1]
                owner = owner_by_thread.get(item['id'],'')
                text = (f'Unreplied KOL email reminder — overdue {age:.1f} hours (threshold {hours:g}h)\n'
                        f'Owner: {html.escape(owner or "Unassigned")}\nEmail: {html.escape(sender)}\n'
                        f'Subject: {html.escape(headers.get("subject",""))}\n'
                        f'Gmail: https://mail.google.com/mail/u/?authuser={gmail.mailbox.replace("@","%40")}#all/{message["id"]}')
                if preview is not None:
                    preview.append({'thread_id': item['id'], 'email': sender,
                        'subject': headers.get('subject',''), 'owner': owner or 'Unassigned',
                        'waiting_hours': round(age,1),
                        'waiting_since': datetime.fromtimestamp(since/1000, now.tzinfo).isoformat(),
                        'gmail': f'https://mail.google.com/mail/u/?authuser={gmail.mailbox.replace("@","%40")}#all/{message["id"]}'})
                # Stable until this round is delivered. Afterwards only a new
                # inbound message starts the next clock, not polling/restarts.
                for user in dict.fromkeys([pilot] + ([users[owner]] if owner in users else [])):
                    key = f'unreplied:{item["id"]}:{since}:{user}'
                    if dry_run:
                        LOG.info('DRY RUN reminder thread=%s recipient=%s age=%.1fh',item['id'],user,age)
                        count += 1
                        continue
                    if db.execute('SELECT 1 FROM sent WHERE key=?',(key,)).fetchone():
                        continue
                    ts = notifier.send_digest(user, text, key, direct_message=True)
                    db.execute('INSERT OR REPLACE INTO sent VALUES (?,?)',(key,ts))
                    db.commit()
                    count += 1
                if not dry_run:
                    db.execute('INSERT OR REPLACE INTO reminder_rounds VALUES (?, ?)', (item['id'], timestamp(message)))
                    db.commit()
            page = found.get('nextPageToken')
            if not page:
                break
    finally:
        if db:
            db.close()
    LOG.info('Unreplied reminder scan completed; %d notification(s)',count)
    return count
