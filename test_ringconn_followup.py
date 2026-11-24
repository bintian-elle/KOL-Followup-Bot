import unittest
import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from ringconn_followup import RingConnFollowup, due_date, gmail_thread_ids, render


class RingConnFollowupTests(unittest.TestCase):
    def test_sent_reply_removes_kol_and_new_inbound_puts_them_back(self):
        person = {"name": "Latest", "threads": ["aa11", "bb22"], "reply_id": "aa11",
                  "reply_at": 1795019185000, "marker": "inbound", "pending": True, "unread": 1}
        main, detail = render("2026-11-18", [person])
        initial = {"date": "2026-11-18", "people": [person], "parent": "parent",
                   "reply": "reply", "parent_text": main, "reply_text": detail}
        inbound = {"id": "inbound", "threadId": "aa11", "internalDate": "1795019185000",
                   "labelIds": ["INBOX", "UNREAD"]}
        sent = {"id": "sent", "threadId": "aa11", "internalDate": "1795019187000",
                "labelIds": ["SENT"]}
        unrelated = dict(sent, id="other", threadId="bb22")
        new_inbound = dict(inbound, id="new-inbound", internalDate="1795019189000")
        with TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            state_file.write_text(json.dumps(initial))
            report = RingConnFollowup(None, None, "bot", state_file)
            with patch.object(report, "_messages", side_effect=[
                [unrelated, inbound], [sent, inbound], [new_inbound, sent, inbound],
            ]), patch("ringconn_followup.slack_call") as slack:
                report.on_history(SimpleNamespace(thread_ids={"bb22"}, changed=True))
                self.assertTrue(report.state["people"][0]["pending"])
                report.on_history(SimpleNamespace(thread_ids={"aa11"}, changed=True))
                self.assertFalse(report.state["people"][0]["pending"])
                self.assertIn("0 位待我方", slack.call_args_list[0].args[2]["text"])
                self.assertNotIn("Latest", slack.call_args_list[1].args[2]["text"])
                report.on_history(SimpleNamespace(thread_ids={"aa11"}, changed=True))
                self.assertTrue(report.state["people"][0]["pending"])
                self.assertIn("1 位待我方", slack.call_args_list[2].args[2]["text"])
                self.assertIn("Latest", slack.call_args_list[3].args[2]["text"])
    def test_daily_report_starts_at_858_eastern_in_winter_and_summer(self):
        for month, utc_hour in ((1, 13), (7, 12)):
            self.assertIsNone(due_date(datetime(2026, month, 18, utc_hour, 57, tzinfo=timezone.utc)))
            self.assertEqual(
                due_date(datetime(2026, month, 18, utc_hour, 58, tzinfo=timezone.utc)),
                f"2026-{month:02d}-18",
            )

    def test_multiple_links_are_preserved_with_an_account_switcher(self):
        urls = [
            "https://mail.google.com/mail/u/?authuser=kol%40elle-media.com#all/aa11",
            "https://mail.google.com/mail/u/?authuser=kol%40elle-media.com#all/bb22",
        ]
        self.assertEqual(gmail_thread_ids(urls), ["aa11", "bb22"])

    def test_unread_marks_only_pending_creators_and_preserves_reply_order(self):
        people = [
            {"name": "Older", "threads": ["bb22"], "reply_id": "bb22", "reply_at": 1794926952000,
             "pending": True, "unread": 0},
            {"name": "Latest", "threads": ["aa11"], "reply_id": "aa11", "reply_at": 1795019185000,
             "pending": True, "unread": 2},
            {"name": "Waiting on KOL", "threads": ["cc33"], "pending": False, "unread": 3},
        ]
        main, detail = render("2026-11-18", people)
        self.assertIn("其中有 1 位", main)
        self.assertIn("11 月 18 日来信 · 1 位", detail)
        self.assertLess(detail.index("Latest"), detail.index("Older"))
        self.assertEqual(detail.count("未读"), 1)
        self.assertIn("未读 2 封", detail)
        self.assertNotIn("Waiting on KOL", detail)

    def test_read_event_edits_both_existing_messages_without_creating_a_post(self):
        person = {"name": "Latest", "threads": ["aa11"], "reply_id": "aa11",
                  "reply_at": 1795019185000, "pending": True, "unread": 1}
        main, detail = render("2026-11-18", [person])
        initial = {"date": "2026-11-18", "people": [person], "parent": "parent",
                   "reply": "reply", "parent_text": main, "reply_text": detail}
        with TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            state_file.write_text(json.dumps(initial))
            report = RingConnFollowup(None, None, "bot", state_file)
            with patch.object(report, "_messages", return_value=[{"labelIds": ["INBOX"]}]), \
                    patch("ringconn_followup.slack_call") as slack:
                report.on_history(SimpleNamespace(thread_ids={"aa11"}, changed=True))
                report.on_history(SimpleNamespace(thread_ids={"aa11"}, changed=True))
            self.assertEqual([call.args[1] for call in slack.call_args_list], ["chat.update", "chat.update"])
            self.assertEqual([call.args[2]["ts"] for call in slack.call_args_list], ["parent", "reply"])
            self.assertIn("其中有 0 位", slack.call_args_list[0].args[2]["text"])
            self.assertNotIn("未读", slack.call_args_list[1].args[2]["text"])
            self.assertEqual(json.loads(state_file.read_text())["people"][0]["unread"], 0)
