"""Prospective Bluevua handoffs, separate from ordinary first outreach replies."""
import re
from dataclasses import replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


def thread_events(thread, mailbox, first_email_only, settings):
    from kol_followup import (addresses, header_map, timestamp, message_text,
                              is_automated_message, eligible_gmail_events,
                              _reply_from_message, config_list)
    team = set(config_list(settings, 'brand_team_emails', []))
    forwarding = settings.get('forwarding_mailbox', '').lower().strip()
    internal = team | ({forwarding} if forwarding else set()) | {mailbox.lower()}
    messages = sorted(thread.get('messages', []), key=timestamp)
    brand_messages = []
    valid = []
    for m in messages:
        if set(m.get('labelIds', [])) & {'DRAFT', 'TRASH'}:
            continue
        h = header_map(m)
        sender = addresses(h.get('from', ''))
        if not sender or is_automated_message(h, sender[0][1]):
            continue
        valid.append(m)
        if sender[0][1] in internal - {mailbox.lower()}:
            brand_messages.append(m)
    if not brand_messages:
        # Direct auto-forwarding preserves the creator's From header. Ordinary
        # eligibility still applies; no need to mistake Reply-To for the KOL.
        events = eligible_gmail_events(thread, mailbox, first_email_only)
        if forwarding and valid:
            first = valid[0]
            h = header_map(first)
            route = ' '.join(h.get(k, '') for k in ('to', 'cc', 'delivered-to', 'x-forwarded-to', 'x-forwarded-for'))
            if forwarding in route.lower():
                if not active(first, settings):
                    return []
                content = (h.get('subject', '') + '\n' + message_text(first)).lower()
                if (any(k in content for k in config_list(settings, 'brand_handoff_exclude_keywords', []))
                        or not any(k in content for k in config_list(settings, 'generic_collab_keywords', []))):
                    return []
                events = [replace(r, original_reply_date=r.reply_date,
                                  reply_date=datetime.fromtimestamp(timestamp(first)/1000, timezone.utc).isoformat(),
                                  threading_status='brand_forward_verified')
                          for r in events if r.message_id == str(first['id'])]
        return events
    first = brand_messages[0]
    # An old handoff is not made eligible by a new follow-up in the same thread.
    if not active(first, settings):
        return []
    h = header_map(first)
    body = message_text(first)
    subject = h.get('subject', '')
    content = (subject + '\n' + body).lower()
    history_content = '\n'.join(header_map(m).get('subject', '') + '\n' + message_text(m) for m in valid).lower()
    exclusions = config_list(settings, 'brand_handoff_exclude_keywords', [])
    if any(k in history_content for k in exclusions):
        return []
    collab = config_list(settings, 'generic_collab_keywords', [])
    handoff = config_list(settings, 'brand_handoff_keywords', [])
    if not any(k in content for k in collab + handoff):
        return []
    # A reply from our mailbox means this was already handled, not a new lead.
    if any('SENT' in m.get('labelIds', []) or
           addresses(header_map(m).get('from', ''))[0][1] == mailbox.lower() for m in valid):
        return []
    candidates = {}
    for m in valid:
        n, e = addresses(header_map(m).get('from', ''))[0]
        if e not in internal and not e.endswith('@bluevua.com'):
            candidates[e] = n
    # Outlook/Gmail forwards: read header blocks only, never signature emails.
    forwarded = re.findall(r'^\s*>?\s*(?:From|发件人)\s*[:：]\s*(.+)$', body, re.M | re.I)
    for line in forwarded:
        for n, e in addresses(line):
            if '@' in e and e not in internal and not e.endswith('@bluevua.com'):
                candidates[e] = n
    if not candidates and any(k in content for k in handoff):
        for n, e in addresses(h.get('to', '')):
            if '@' in e and e not in internal and not e.endswith('@bluevua.com'):
                candidates[e] = n
    candidates = {e:n for e,n in candidates.items() if re.fullmatch(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', e)}
    if len(candidates) != 1:
        # Explicit unresolved identity, never a brand sender masquerading as KOL.
        email, name = '', 'Unknown creator — review forwarded email'
    else:
        email, name = next(iter(candidates.items()))
    original_date = ''
    native = [m for m in valid if addresses(header_map(m).get('from', ''))[0][1] == email]
    if native:
        # Multiple creator replies prove this is not the first inbound inquiry.
        if len(native) > 1:
            return []
        original_date = _reply_from_message(native[0], str(thread['id']), '', '').reply_date
    else:
        dates = re.findall(r'^\s*>?\s*(?:Date|Sent|日期|发送时间)\s*[:：]\s*(.+)$', body, re.M | re.I)
        if len(dates) == 1:
            try:
                dt = parsedate_to_datetime(dates[0])
                if dt.tzinfo is not None:
                    original_date = dt.astimezone(timezone.utc).isoformat()
            except (ValueError, TypeError, OverflowError):
                pass
    base = _reply_from_message(first, str(thread['id']), '', 'brand_forward_needs_review')
    # A forwarded body is incomplete history even with one parseable From/Date.
    # Verification is reserved for a native initial creator inquiry + handoff.
    verified = bool(email and original_date and native and valid[0] is native[0]
                    and len(brand_messages) == 1 and not forwarded)
    return [replace(base, kol_email=email, kol_name=name, original_reply_date=original_date,
                    reply_date=datetime.fromtimestamp(timestamp(first)/1000, timezone.utc).isoformat(),
                    threading_status='brand_forward_verified' if verified else 'brand_forward_needs_review')]


def active(message, settings):
    from kol_followup import timestamp
    value = settings.get('brand_handoff_enabled_at', '').strip()
    if not value:
        return False  # No activation timestamp must never mean scan all history.
    enabled = datetime.fromisoformat(value)
    if enabled.tzinfo is None:
        raise ValueError('brand_handoff_enabled_at must include timezone')
    return timestamp(message) >= int(enabled.timestamp() * 1000)
