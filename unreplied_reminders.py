"""Independent label-scoped overdue reply reminders; no Queue mutation."""
import html
import sqlite3
from datetime import datetime, timezone
import time


def pending_reply(thread, mailbox, label_ids):
    from kol_followup import timestamp, header_map, addresses, is_automated_message
    pending = None
    pending_since = None
    for message in sorted(thread.get('messages', []), key=timestamp):
        labels = set(message.get('labelIds', []))
        if labels & {'DRAFT', 'TRASH'}:
            continue
        if 'SENT' in labels:
            pending = pending_since = None
            continue
        headers = header_map(message)
        sender = addresses(headers.get('from', ''))
        if not sender or sender[0][1].lower() == mailbox.lower():
            continue
        if is_automated_message(headers, sender[0][1]):
            continue
        # Thread may be found through an older labeled message. Remind only
        # when the outstanding inbound episode actually includes a match.
        if not labels & label_ids:
            continue
        if pending_since is None:
            pending_since = timestamp(message)
        pending = message
    return (pending, pending_since) if pending else None


def check_reminders(gmail, store, owners, settings, now, dry_run=False):
    from kol_followup import SlackNotifier, QUEUE_HEADERS, project_path, LOG, header_map, addresses
    raw = settings.get('unreplied_reminder_hours', '').strip()
    if not raw:
        return 0
    hours = float(raw)
    if hours <= 0:
        return 0
    brand = settings.get('campaign_name_contains', '').strip().casefold()
    if not brand:
        return 0
    labels = gmail._execute(gmail.api.users().labels().list(userId='me')).get('labels', [])
    matches = {r['id'] for r in labels if r.get('type') == 'user' and brand in r['name'].casefold()}
    if not matches:
        return 0
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
    if not dry_run:
        db = sqlite3.connect(project_path('.unreplied-reminders.sqlite3'))
        db.execute('CREATE TABLE IF NOT EXISTS sent (key TEXT PRIMARY KEY, ts TEXT)')
    count = 0
    seen = set()
    try:
        for label in sorted(matches):
            page = None
            while True:
                found = gmail._execute(gmail.api.users().threads().list(
                    userId='me', labelIds=[label], maxResults=100, pageToken=page))
                for item in found.get('threads', []):
                    if item['id'] in seen:
                        continue
                    seen.add(item['id'])
                    thread = gmail._execute(gmail.api.users().threads().get(userId='me',id=item['id'],format='full'))
                    time.sleep(max(0, float(__import__('os').getenv('GMAIL_THREAD_INTERVAL_SECONDS','2.5'))))
                    pending = pending_reply(thread, gmail.mailbox, matches)
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
                    # Episode key remains stable when additional inbound messages
                    # arrive, until a real outbound reply resets the episode.
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
                page = found.get('nextPageToken')
                if not page:
                    break
    finally:
        if db:
            db.close()
    LOG.info('Unreplied reminder scan completed; %d notification(s)',count)
    return count
