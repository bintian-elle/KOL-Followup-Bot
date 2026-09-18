import unittest
import base64
import threading
from unittest.mock import patch
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from kol_followup import (
    PROJECT_DIR, Owner, first_eligible_gmail_message, first_external_reply,
    classify_reply, digest_window, eligible_gmail_events,
    has_profile_and_audience_evidence, is_business_day, load_owners,
    maybe_send_daily_digest, message_text, next_config_owner, next_owner,
    project_path, reply_after_cutoff, QUEUE_HEADERS, SlackNotifier,
)


def message(mid, date, sender, labels, subject="Hello", message_id=None, in_reply_to=None, extra=None):
    headers = [
        {"name": "From", "value": sender},
        {"name": "Subject", "value": subject},
        {"name": "Date", "value": "Tue, 16 Sep 2026 12:00:00 +0000"},
    ]
    if message_id:
        headers.append({"name": "Message-ID", "value": message_id})
    if in_reply_to:
        headers.append({"name": "In-Reply-To", "value": in_reply_to})
    for name, value in (extra or {}).items():
        headers.append({"name": name, "value": value})
    return {
        "id": mid,
        "internalDate": str(date),
        "labelIds": labels,
        "payload": {"headers": headers},
    }


class WorkflowTests(unittest.TestCase):
    def established(self, thread):
        thread['messages'] = [message('prior-in',10,'KOL <k@example.com>',[]),
                              message('prior-answer',20,'Me <me@example.com>',['SENT'])] + thread['messages']
        return thread

    def handoff_settings(self):
        return {'brand_team_emails':'mel@bluevua.com,jeremy.ku@bluevua.com',
                'forwarding_mailbox':'partnerships@bluevua.com',
                'brand_handoff_enabled_at':'1970-01-01T00:00:02+00:00',
                'generic_collab_keywords':'collab,partnership',
                'brand_handoff_keywords':'copying my colleagues',
                'brand_handoff_exclude_keywords':'certification,following up'}

    def test_brand_handoff_new_native_inquiry_has_real_creator(self):
        from brand_handoffs import thread_events
        t = {'id':'t','messages':[message('kol',2000,'Grace <grace@example.com>',[],subject='Bluevua partnership'),
             message('mel',3000,'Mel <mel@bluevua.com>',[],subject='Bluevua partnership')]}
        r = thread_events(t,'me@example.com',True,self.handoff_settings())[0]
        self.assertEqual(r.kol_email,'grace@example.com')
        self.assertEqual(r.threading_status,'brand_forward_verified')
        self.assertEqual(r.reply_date,'1970-01-01T00:00:03+00:00')

    def test_old_handoff_not_reopened_by_new_brand_reply(self):
        from brand_handoffs import thread_events
        t = {'id':'t','messages':[message('old',1000,'Mel <mel@bluevua.com>',[],subject='Bluevua partnership'),
             message('new',3000,'Mel <mel@bluevua.com>',[],subject='Bluevua partnership')]}
        self.assertEqual(thread_events(t,'me@example.com',True,self.handoff_settings()),[])
        self.assertEqual(thread_events(t,'me@example.com',True,{'brand_team_emails':'mel@bluevua.com'}),[])

    def test_brand_followup_and_prior_creator_replies_excluded(self):
        from brand_handoffs import thread_events
        t = {'id':'t','messages':[message('new',3000,'Jeremy <jeremy.ku@bluevua.com>',[],subject='Bluevua certification following up')]}
        self.assertEqual(thread_events(t,'me@example.com',True,self.handoff_settings()),[])
        t = {'id':'t','messages':[message('a',2000,'KOL <kol@example.com>',[]),
             message('b',2500,'KOL <kol@example.com>',[]),
             message('mel',3000,'Mel <mel@bluevua.com>',[],subject='Bluevua partnership')]}
        self.assertEqual(thread_events(t,'me@example.com',True,self.handoff_settings()),[])

    def test_brand_forward_body_is_review_not_brand_as_creator(self):
        from brand_handoffs import thread_events
        m = message('mel',3000,'Mel <mel@bluevua.com>',[],subject='Fwd: Bluevua partnership')
        m['payload']['mimeType']='text/plain'
        m['payload']['body']={'data':base64.urlsafe_b64encode(b'From: Grace <grace@example.com>\nDate: Mon, 14 Sep 2026 12:00:00 +0000\nHello, partnership?').decode()}
        r = thread_events({'id':'t','messages':[m]},'me@example.com',True,self.handoff_settings())[0]
        self.assertEqual(r.kol_email,'grace@example.com')
        self.assertEqual(r.threading_status,'brand_forward_needs_review')

    def test_old_direct_auto_forward_is_not_admitted(self):
        from brand_handoffs import thread_events
        m = message('old',1000,'KOL <kol@example.com>',[],extra={'To':'partnerships@bluevua.com'})
        self.assertEqual(thread_events({'id':'t','messages':[m]},'me@example.com',True,self.handoff_settings()),[])

    def test_new_direct_auto_forward_uses_receipt_and_preserves_original_date(self):
        from brand_handoffs import thread_events
        m = message('new',3000,'KOL <kol@example.com>',[],subject='Bluevua partnership',extra={'To':'partnerships@bluevua.com'})
        r = thread_events({'id':'t','messages':[m]},'me@example.com',True,self.handoff_settings())[0]
        self.assertEqual(r.reply_date,'1970-01-01T00:00:03+00:00')
        self.assertTrue(r.original_reply_date.startswith('2026-09-16'))
        self.assertEqual(r.kol_email,'kol@example.com')

    def test_reminder_cutoff_excludes_old_threads_and_recent_drafts(self):
        from unreplied_reminders import pending_reply
        t = {'messages': [message('a',1000,'KOL <k@example.com>',['brand'],subject='Bluevua'),
                          message('draft',4000,'Me <me@example.com>',['DRAFT'])]}
        self.established(t)
        self.assertIsNone(pending_reply(t,'me@example.com','Bluevua',2000))
        t['messages'].append(message('auto',5000,'Bot <bot@example.com>',['brand'],extra={'Auto-Submitted':'auto-replied'}))
        self.assertIsNone(pending_reply(t,'me@example.com','Bluevua',2000))

    def test_reminder_cutoff_inclusive_latest_activity_not_episode_start(self):
        from unreplied_reminders import pending_reply
        t = {'messages': [message('a',1000,'KOL <k@example.com>',['brand'],subject='Bluevua'),
                          message('b',2000,'KOL <k@example.com>',['brand'])]}
        self.established(t)
        self.assertEqual(pending_reply(t,'me@example.com','Bluevua',2000)[1],1000)
        t['messages'].append(message('out',3000,'Me <me@example.com>',['SENT']))
        self.assertIsNone(pending_reply(t,'me@example.com','Bluevua',2000))

    def test_pending_reminder_ignores_draft_and_resets_on_sent(self):
        from unreplied_reminders import pending_reply
        inbound = message('in', 1000, 'KOL <k@example.com>', ['INBOX', 'brand'],subject='Bluevua')
        draft = message('draft', 2000, 'Me <me@example.com>', ['DRAFT'])
        t = {'messages': [inbound, draft]}
        self.established(t)
        self.assertEqual(pending_reply(t, 'me@example.com', 'Bluevua')[0]['id'], 'in')
        t['messages'].append(message('out', 3000, 'Me <alias@example.com>', ['SENT']))
        self.assertIsNone(pending_reply(t, 'me@example.com', 'Bluevua'))

    def test_pending_reminder_keeps_first_wait_time_and_latest_content(self):
        from unreplied_reminders import pending_reply
        t = {'messages': [message('a',1000,'KOL <k@example.com>',['brand'],subject='Bluevua'),
                          message('b',2000,'KOL <k@example.com>',['brand'])]}
        self.established(t)
        m, since = pending_reply(t,'me@example.com','Bluevua')
        self.assertEqual((m['id'],since),('b',1000))
        self.assertIsNone(pending_reply(t,'me@example.com','OtherBrand'))

    def test_reminder_content_without_labels_and_thread_context(self):
        from unreplied_reminders import pending_reply
        t = {'messages':[message('sent',1000,'Me <me@example.com>',['SENT'],subject='BLUEVUA partnership'),
                         message('reply',2000,'KOL <k@example.com>',[],subject='Interested!')]}
        self.established(t)
        self.assertEqual(pending_reply(t,'me@example.com','Bluevua')[0]['id'],'reply')
        t['messages'][2]['payload']['headers'][1]['value']='Other project'
        t['messages'][3]['labelIds']=['Bluevua']
        self.assertIsNone(pending_reply(t,'me@example.com','Bluevua'))

    def test_reminder_html_body_relevance_without_brand_subject(self):
        from unreplied_reminders import pending_reply
        m = message('reply',2000,'KOL <k@example.com>',[])
        m['payload']['mimeType']='text/html'
        m['payload']['body']={'data':base64.urlsafe_b64encode(b'<p>Interested in Bluevua collaboration</p>').decode()}
        self.assertIsNotNone(pending_reply(self.established({'messages':[m]}),'me@example.com','bluevua'))

    def test_reminder_requires_actual_answer_not_cold_outreach_or_draft(self):
        from unreplied_reminders import pending_reply
        t = {'messages':[message('outreach',100,'Me <me@example.com>',['SENT'],subject='Bluevua'),
                         message('first-reply',200,'KOL <k@example.com>',[])]}
        self.assertIsNone(pending_reply(t,'me@example.com','Bluevua'))
        t['messages'].append(message('draft',300,'Me <me@example.com>',['DRAFT']))
        self.assertIsNone(pending_reply(t,'me@example.com','Bluevua'))
        t['messages'].append(message('answer',400,'Me <me@example.com>',['SENT']))
        self.assertIsNone(pending_reply(t,'me@example.com','Bluevua'))
        t['messages'].append(message('new-reply',500,'KOL <k@example.com>',[]))
        self.assertEqual(pending_reply(t,'me@example.com','Bluevua')[1],500)

    def test_reminder_activation_ignores_old_mail_but_uses_old_answer(self):
        from unreplied_reminders import pending_reply
        t = self.established({'messages':[message('old',1000,'KOL <k@example.com>',[],subject='Bluevua')]})
        self.assertIsNone(pending_reply(t,'me@example.com','Bluevua',enabled_ms=2000))
        t['messages'].append(message('new',3000,'KOL <k@example.com>',[]))
        self.assertEqual(pending_reply(t,'me@example.com','Bluevua',enabled_ms=2000)[1],3000)

    def test_reminder_once_until_new_inbound_starts_new_round(self):
        from unreplied_reminders import pending_reply
        t = self.established({'messages':[message('a',1000,'KOL <k@example.com>',[],subject='Bluevua'),
                                           message('b',2000,'KOL <k@example.com>',[])]})
        self.assertIsNone(pending_reply(t,'me@example.com','Bluevua',notified_ms=2000))
        t['messages'].append(message('new',4000,'KOL <k@example.com>',[]))
        self.assertEqual(pending_reply(t,'me@example.com','Bluevua',notified_ms=2000)[1],4000)
        t['messages'].append(message('answer',5000,'Me <me@example.com>',['SENT']))
        self.assertIsNone(pending_reply(t,'me@example.com','Bluevua',notified_ms=2000))

    def test_reminder_activation_persists_across_restarts_and_upgrade(self):
        import sqlite3
        from unreplied_reminders import initialize_state
        db = sqlite3.connect(':memory:')
        db.execute('CREATE TABLE sent (key TEXT PRIMARY KEY, ts TEXT)')
        self.assertEqual(initialize_state(db,1000),1000)
        self.assertEqual(initialize_state(db,9999),1000)
        db.close()

    def test_reminder_worker_activation_once_and_new_round_delivery(self):
        from unittest.mock import MagicMock
        from tempfile import TemporaryDirectory
        from unreplied_reminders import check_reminders
        gmail = MagicMock()
        gmail.mailbox = 'me@example.com'
        gmail._execute.side_effect = lambda request: request.execute()
        gmail.api.users().threads().list().execute.return_value = {'threads':[{'id':'t'}]}
        thread = {'messages':[message('initial',60000,'KOL <k@example.com>',[],subject='Bluevua'),
                              message('answer',70000,'Me <me@example.com>',['SENT']),
                              message('old',80000,'KOL <k@example.com>',[])]}
        gmail.api.users().threads().get().execute.side_effect = lambda: thread
        store = MagicMock()
        store.queue_rows.return_value = []
        settings = {'unreplied_reminder_hours':'1', 'campaign_name_contains':'Bluevua',
                    'reply_cutoff_date':'01/01/1970', 'pilot_recipient_slack_id':'pilot'}
        def now(seconds):
            return datetime.fromtimestamp(seconds, ZoneInfo('UTC'))
        with TemporaryDirectory() as directory, patch('kol_followup.project_path',return_value=str(Path(directory)/'state.sqlite3')), \
             patch('kol_followup.SlackNotifier') as slack, patch.dict('os.environ',{'GMAIL_THREAD_INTERVAL_SECONDS':'0'}):
            slack.return_value.send_digest.return_value = 'ts'
            self.assertEqual(check_reminders(gmail,store,[],settings,now(100)),0)
            thread['messages'].append(message('new',200000,'KOL <k@example.com>',[]))
            self.assertEqual(check_reminders(gmail,store,[],settings,now(6000)),1)
            self.assertEqual(check_reminders(gmail,store,[],settings,now(6500)),0)
            thread['messages'].append(message('next-round',7000000,'KOL <k@example.com>',[]))
            self.assertEqual(check_reminders(gmail,store,[],settings,now(11000)),1)
            self.assertEqual(check_reminders(gmail,store,[],settings,now(12000)),0)
            self.assertEqual(slack.return_value.send_digest.call_count,2)

    def test_prior_followup_reply_blocks_later_reply_to_initial(self):
        thread = {'id': 't1', 'messages': [
            message('initial', 1, 'Me <me@example.com>', ['SENT'], message_id='<initial>'),
            message('followup', 2, 'Me <me@example.com>', ['SENT'], message_id='<followup>'),
            message('prior', 3, 'KOL <kol@example.com>', ['INBOX'], in_reply_to='<followup>'),
            message('later', 4, 'KOL <kol@example.com>', ['INBOX'], in_reply_to='<initial>'),
        ]}
        self.assertEqual(eligible_gmail_events(thread, 'me@example.com', True), [])

    def test_prior_first_reply_blocks_all_later_conversation_messages(self):
        thread = {'id': 't1', 'messages': [
            message('initial', 1, 'Me <me@example.com>', ['SENT'], message_id='<initial>'),
            message('prior', 2, 'KOL <kol@example.com>', ['INBOX'], in_reply_to='<initial>'),
            message('answer', 3, 'Me <me@example.com>', ['SENT'], message_id='<answer>'),
            message('later', 4, 'KOL <kol@example.com>', ['INBOX'], in_reply_to='<answer>'),
            message('later2', 5, 'KOL <kol@example.com>', ['INBOX'], in_reply_to='<initial>'),
        ]}
        self.assertEqual([r.message_id for r in eligible_gmail_events(thread, 'me@example.com')], ['prior'])

    def test_missing_headers_first_reply_does_not_admit_later_verified_reply(self):
        thread = {'id': 't1', 'messages': [
            message('initial', 1, 'Me <me@example.com>', ['SENT'], message_id='<initial>'),
            message('prior', 2, 'KOL <kol@example.com>', ['INBOX']),
            message('later', 3, 'KOL <kol@example.com>', ['INBOX'], in_reply_to='<initial>'),
        ]}
        events = eligible_gmail_events(thread, 'me@example.com')
        self.assertEqual([r.message_id for r in events], ['prior'])
        self.assertEqual(events[0].threading_status, 'missing_threading_headers')

    def test_testing_channel_copies_without_replacing_owner_dm(self):
        from kol_followup import Reply
        notifier = SlackNotifier('token', testing_channel_id='Ctest')
        reply = Reply('m1', 't1', 'Creator', 'creator@example.com', 'Bluevua', '')
        with patch.object(notifier, '_dm_channel', return_value='Downer') as dm, \
             patch('kol_followup.requests.post') as post:
            post.return_value.json.return_value = {'ok': True, 'ts': '123'}
            notifier.send(Owner('A', 'U1'), reply, 'owner-task:1')
        dm.assert_called_once_with('U1')
        self.assertEqual([c.kwargs['json']['channel'] for c in post.call_args_list], ['Downer', 'Ctest'])

    def test_testing_channel_copies_without_replacing_digest(self):
        notifier = SlackNotifier('token', testing_channel_id='Ctest')
        with patch.object(notifier, '_dm_channel', return_value='Dcandice'), \
             patch('kol_followup.requests.post') as post:
            post.return_value.json.return_value = {'ok': True, 'ts': '123'}
            notifier.send_digest('Ucandice', 'digest', 'daily-digest:1', True)
        self.assertEqual([c.kwargs['json']['channel'] for c in post.call_args_list], ['Dcandice', 'Ctest'])

    def test_inactive_testing_channel_leaves_owner_dm(self):
        from kol_followup import Reply
        notifier = SlackNotifier('token')
        with patch.object(notifier, '_dm_channel', return_value='Downer'), \
             patch('kol_followup.requests.post') as post:
            post.return_value.json.return_value = {'ok': True, 'ts': '123'}
            notifier.send(Owner('A', 'U1'), Reply('m', 't', 'K', 'k@example.com', 'Bluevua', ''), 'task')
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs['json']['channel'], 'Downer')

    def test_owner_totals_only_count_assigned_and_latest_date(self):
        from kol_followup import owner_assignment_totals
        rows = []
        for owner, status, date in [('A', 'Assigned', '12/31/2025 10:00'),
                                    ('A', 'Assigned', '01/01/2026 09:00'),
                                    ('A', 'Reactivated', '09/17/2026 10:00'),
                                    ('B', 'Ignored', '')]:
            row = [''] * len(QUEUE_HEADERS)
            row[7], row[8], row[9] = owner, status, date
            rows.append(row)
        self.assertEqual(owner_assignment_totals(rows), {'A': (2, '01/01/2026 09:00')})

    def test_monitor_scans_immediately_and_waits_after_failure(self):
        from kol_followup import monitor
        stop = threading.Event()
        waits = []
        def wait(seconds):
            waits.append(seconds)
            if len(waits) == 2:
                stop.set()
        with patch('kol_followup.run', side_effect=[RuntimeError('offline'), 0]) as run_mock, \
             patch.object(stop, 'wait', side_effect=wait), patch('kol_followup.LOG'):
            monitor(False, 300, stop)
        self.assertEqual(run_mock.call_count, 2)
        self.assertEqual(waits, [300, 300])

    def test_monitor_does_not_scan_when_stopped(self):
        from kol_followup import monitor
        stop = threading.Event()
        stop.set()
        with patch('kol_followup.run') as run_mock:
            monitor(False, 300, stop)
        run_mock.assert_not_called()

    def setUp(self):
        self.owners = [Owner("A", "U1"), Owner("B", "U2"), Owner("C", "U3")]

    def test_first_external_reply_after_outbound(self):
        thread = {"id": "t1", "messages": [
            message("sent", 2, "Me <me@example.com>", ["SENT"], message_id="<initial@example.com>"),
            message("reply", 3, "KOL <kol@example.com>", ["INBOX"], "Re: Hello", in_reply_to="<initial@example.com>"),
            message("reply2", 4, "KOL <kol@example.com>", ["INBOX"], in_reply_to="<initial@example.com>"),
        ]}
        reply = first_external_reply(thread, "me@example.com")
        self.assertEqual(reply.message_id, "reply")
        self.assertEqual(reply.kol_email, "kol@example.com")

    def test_no_reply(self):
        thread = {"id": "t1", "messages": [message("sent", 2, "Me <me@example.com>", ["SENT"])]}
        self.assertIsNone(first_external_reply(thread, "me@example.com"))

    def test_sent_alias_still_marks_outbound(self):
        thread = {"id": "t1", "messages": [
            message("sent", 1, "Alias <alias@example.com>", ["SENT"], message_id="<initial@example.com>"),
            message("reply", 2, "KOL <kol@example.com>", ["INBOX"], in_reply_to="<initial@example.com>"),
        ]}
        self.assertEqual(first_external_reply(thread, "me@example.com").message_id, "reply")

    def test_reply_to_followup_is_not_first_email_reply(self):
        thread = {"id": "t1", "messages": [
            message("initial", 1, "Me <me@example.com>", ["SENT"], message_id="<initial@example.com>"),
            message("followup", 2, "Me <me@example.com>", ["SENT"], message_id="<followup@example.com>"),
            message("reply", 3, "KOL <kol@example.com>", ["INBOX"], in_reply_to="<followup@example.com>",
                    extra={"References": "<initial@example.com> <followup@example.com>"}),
        ]}
        self.assertIsNone(first_external_reply(thread, "me@example.com"))

    def test_automated_reply_is_skipped(self):
        thread = {"id": "t1", "messages": [
            message("initial", 1, "Me <me@example.com>", ["SENT"], message_id="<initial@example.com>"),
            message("auto", 2, "KOL <kol@example.com>", ["INBOX"], "Automatic Reply",
                    in_reply_to="<initial@example.com>", extra={"Auto-Submitted": "auto-replied"}),
            message("human", 3, "KOL <kol@example.com>", ["INBOX"], "Re: Hello",
                    in_reply_to="<initial@example.com>"),
        ]}
        self.assertEqual(first_external_reply(thread, "me@example.com").message_id, "human")

    def test_missing_threading_headers_goes_to_review(self):
        thread = {"id": "t1", "messages": [
            message("initial", 1, "Me <me@example.com>", ["SENT"], message_id="<initial@example.com>"),
            message("reply", 2, "KOL <kol@example.com>", ["INBOX"]),
        ]}
        reply = first_external_reply(thread, "me@example.com")
        self.assertEqual(reply.threading_status, "missing_threading_headers")
        self.assertEqual(classify_reply(reply, {})[:2], (True, "Needs Review"))

    def test_externally_initiated_gmail_thread_is_eligible(self):
        thread = {"id": "t1", "messages": [
            message("inbound", 1, "Agency <agent@example.com>", ["INBOX"], "Bluevua creator pitch"),
            message("sent", 2, "Me <me@example.com>", ["SENT"], message_id="<answer@example.com>"),
        ]}
        reply = first_eligible_gmail_message(thread, "me@example.com")
        self.assertEqual(reply.message_id, "inbound")
        self.assertEqual(reply.outreach_message_id, "")

    def test_outbound_gmail_thread_uses_strict_upfluence_rule(self):
        thread = {"id": "t1", "messages": [
            message("initial", 1, "Me <me@example.com>", ["SENT"], message_id="<initial@example.com>"),
            message("followup", 2, "Me <me@example.com>", ["SENT"], message_id="<followup@example.com>"),
            message("reply", 3, "KOL <kol@example.com>", ["INBOX"], in_reply_to="<followup@example.com>"),
        ]}
        self.assertIsNone(first_eligible_gmail_message(thread, "me@example.com"))

    def test_round_robin_continues_from_sheet(self):
        blank = [""] * 7
        rows = [blank + ["A"], blank + ["B"]]
        self.assertEqual(next_owner(self.owners, rows).name, "C")
        rows.append(blank + ["C"])
        self.assertEqual(next_owner(self.owners, rows).name, "A")

    def test_exactly_three_owners_required(self):
        with self.assertRaises(ValueError):
            load_owners('[{"name":"A","slack_user_id":"U1"}]')

    def test_relative_credentials_path_uses_project_directory(self):
        self.assertEqual(
            Path(project_path("credentials/key.json")),
            PROJECT_DIR / "credentials/key.json",
        )

    def test_config_round_robin(self):
        self.assertEqual(next_config_owner(self.owners, "B").name, "C")
        self.assertEqual(next_config_owner(self.owners, "C").name, "A")

    def test_bluevua_reply_is_assigned(self):
        reply = first_eligible_gmail_message({"id": "t1", "messages": [
            message("inbound", 1, "KOL <kol@example.com>", ["INBOX"], "Bluevua collaboration"),
        ]}, "me@example.com")
        self.assertEqual(classify_reply(reply, {"campaign_name_contains": "Bluevua"})[:2], (True, "Human Reply"))

    def test_generic_ugc_pitch_is_ignored_from_config(self):
        reply = first_eligible_gmail_message({"id": "t1", "messages": [
            message("inbound", 1, "KOL <kol@example.com>", ["INBOX"], "UGC creator collaboration"),
        ]}, "me@example.com")
        assigned, _, reason = classify_reply(reply, {"gmail_ugc_outreach_action": "Ignore"})
        self.assertFalse(assigned)
        self.assertEqual(reason, "UGC creator outreach")

    def test_bluevua_payment_closure_is_ignored(self):
        reply = first_eligible_gmail_message({"id": "t1", "messages": [
            message("inbound", 1, "KOL <kol@example.com>", ["INBOX"], "Payment Request - Bluevua UGC"),
        ]}, "me@example.com")
        assigned, _, reason = classify_reply(reply, {
            "campaign_name_contains": "Bluevua", "gmail_completed_collab_action": "Ignore",
        })
        self.assertFalse(assigned)
        self.assertEqual(reason, "Collaboration completed")

    def test_newsletter_body_keyword_does_not_trigger_review(self):
        reply = first_eligible_gmail_message({"id": "t1", "messages": [
            message("inbound", 1, "News <news@example.com>", ["INBOX"], "Weekly industry news",
                    extra={}),
        ]}, "me@example.com")
        reply = type(reply)(**{**reply.__dict__, "body_text": "Advice for creator agencies"})
        self.assertFalse(classify_reply(reply, {})[0])

    def test_instagram_notification_is_not_a_reply(self):
        thread = {"id": "t1", "messages": [
            message("notification", 1, "Instagram <no-reply@mail.instagram.com>", ["INBOX"],
                    "emw3.collabteam, catch up on moments you've missed"),
        ]}
        self.assertIsNone(first_eligible_gmail_message(thread, "me@example.com"))

    def test_spam_with_profile_and_follower_scale_is_assigned_for_review(self):
        reply = first_eligible_gmail_message({"id": "t1", "messages": [
            message("spam", 1, "Creator <creator@example.com>", ["INBOX", "SPAM"], "Let's collaborate"),
        ]}, "me@example.com")
        reply = type(reply)(**{
            **reply.__dict__,
            "body_text": "Instagram: https://instagram.com/example — 125K followers",
        })
        self.assertTrue(has_profile_and_audience_evidence(reply))
        assigned, classification, reason = classify_reply(reply, {})
        self.assertTrue(assigned)
        self.assertEqual(classification, "Needs Review")
        self.assertIn("Spam", reason)

    def test_spam_profile_without_follower_scale_does_not_override_ugc_ignore(self):
        reply = first_eligible_gmail_message({"id": "t1", "messages": [
            message("spam", 1, "Creator <creator@example.com>", ["INBOX", "SPAM"], "UGC collaboration"),
        ]}, "me@example.com")
        reply = type(reply)(**{**reply.__dict__, "body_text": "https://tiktok.com/@example"})
        self.assertFalse(has_profile_and_audience_evidence(reply))
        self.assertFalse(classify_reply(reply, {"gmail_ugc_outreach_action": "Ignore"})[0])

    def test_later_reply_after_team_answer_is_excluded(self):
        thread = {"id": "t1", "messages": [
            message("initial", 1, "Me <me@example.com>", ["SENT"], message_id="<initial@example.com>"),
            message("first_reply", 2, "KOL <kol@example.com>", ["INBOX"], in_reply_to="<initial@example.com>"),
            message("team_answer", 3, "Me <me@example.com>", ["SENT"], message_id="<answer@example.com>"),
            message("new_reply", 4, "KOL <kol@example.com>", ["INBOX"], in_reply_to="<answer@example.com>"),
        ]}
        events = eligible_gmail_events(thread, "me@example.com")
        self.assertEqual([e.message_id for e in events], ["first_reply"])

    def test_other_brand_reactivation_remains_ignored(self):
        reply = first_eligible_gmail_message({"id": "t1", "messages": [
            message("inbound", 1, "KOL <kol@example.com>", ["INBOX"], "BICEEK partnership"),
        ]}, "me@example.com")
        reply = type(reply)(**{**reply.__dict__, "threading_status": "reactivated"})
        assigned, _, reason = classify_reply(reply, {"other_brand_keywords": "biceek,ringconn"})
        self.assertFalse(assigned)
        self.assertEqual(reason, "Other brand")

    def test_html_only_body_extracts_text_and_link(self):
        encoded = base64.urlsafe_b64encode(
            b'<html><body><a href="https://instagram.com/example">Profile</a><p>125K followers</p></body></html>'
        ).decode().rstrip("=")
        msg = {"payload": {"mimeType": "text/html", "body": {"data": encoded}}}
        text = message_text(msg)
        self.assertIn("https://instagram.com/example", text)
        self.assertIn("125K followers", text)

    def test_config_keywords_override_defaults(self):
        reply = first_eligible_gmail_message({"id": "t1", "messages": [
            message("inbound", 1, "KOL <kol@example.com>", ["INBOX"], "Acme partnership"),
        ]}, "me@example.com")
        self.assertEqual(classify_reply(reply, {"other_brand_keywords": "Acme,Other"})[2], "Other brand")

    def test_reply_cutoff_is_enforced_per_message(self):
        reply = first_eligible_gmail_message({"id": "t1", "messages": [
            message("inbound", 1, "KOL <kol@example.com>", ["INBOX"]),
        ]}, "me@example.com")
        self.assertFalse(reply_after_cutoff(reply, {"reply_cutoff_date": "09/17/2026"}, ZoneInfo("America/Los_Angeles")))

    def test_federal_holiday_and_weekend_digest_window(self):
        settings = {"holiday_calendar": "United States federal holidays", "daily_send_time": "09:00"}
        tz = ZoneInfo("America/Los_Angeles")
        self.assertFalse(is_business_day(datetime(2026, 9, 7).date(), settings))  # Labor Day
        monday = datetime(2026, 9, 14, 9, 5, tzinfo=tz)
        start, end = digest_window(monday, settings)
        self.assertEqual(start.date().isoformat(), "2026-09-11")
        self.assertEqual(end.date().isoformat(), "2026-09-14")

    def test_daily_digest_uses_previous_business_window(self):
        class Store:
            def __init__(self): self.audit = []; self.setting = None
            def append_audit(self, row): self.audit.append(row)
            def update_setting(self, rows, name, value): self.setting = (name, value)
        class Notifier:
            def __init__(self): self.calls = []
            def send_digest(self, *args, **kwargs): self.calls.append((args, kwargs)); return "ts"
        row = [""] * len(QUEUE_HEADERS)
        row[QUEUE_HEADERS.index("Assignment ID")] = "BV-1"
        row[QUEUE_HEADERS.index("Assigned At")] = "09/11/2026 12:00"
        row[QUEUE_HEADERS.index("Assignment Status")] = "Assigned"
        row[QUEUE_HEADERS.index("Assigned Owner")] = "A"
        row[QUEUE_HEADERS.index("KOL / Creator Name")] = "Creator"
        row[QUEUE_HEADERS.index("Campaign / Context")] = "Bluevua"
        settings = {
            "holiday_calendar": "United States federal holidays", "daily_send_time": "09:00",
            "pilot_mode": "TRUE", "pilot_recipient_slack_id": "U1", "skip_empty_digest": "TRUE",
        }
        store, notifier = Store(), Notifier()
        sent = maybe_send_daily_digest(
            store, [row], settings, {"last_daily_digest_date": 1}, notifier,
            datetime(2026, 9, 14, 9, 5, tzinfo=ZoneInfo("America/Los_Angeles")),
        )
        self.assertTrue(sent)
        self.assertEqual(len(notifier.calls), 1)
        self.assertEqual(store.setting, ("last_daily_digest_date", "2026-09-14"))

    def test_slack_idempotency_key_is_stable(self):
        first = SlackNotifier._client_msg_id("owner-task:BV-1")
        self.assertEqual(first, SlackNotifier._client_msg_id("owner-task:BV-1"))
        self.assertNotEqual(first, SlackNotifier._client_msg_id("owner-task:BV-2"))


if __name__ == "__main__":
    unittest.main()
