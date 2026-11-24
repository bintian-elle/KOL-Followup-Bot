"""Publish and maintain the RingConn KOL report using Sheets and Gmail."""

import json
import re
from collections import defaultdict
from datetime import datetime, time, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

from googleapiclient.errors import HttpError

from assign_active import slack_call
from gmail_event_sync import retry_execute

SHEET_ID = "1zTjdW_X47g0CnyRMBs-RCfLTHbVQA70sn9c-BaHVPew"
TAB = "推进中KOL进展汇总"
SHEET_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=490433399"
MAILBOX = "kol@elle-media.com"
CHANNEL = "C0BHLEH7D5L"
MENTIONS = "<@U088ZSEEU91> <@U08UDLV4HJ7>"
EASTERN = ZoneInfo("America/New_York")
STATE_PATH = Path(__file__).resolve().with_name(".ringconn-followup.json")
SEND_TIME = time(8, 58)


def due_date(now):
    local = now.astimezone(EASTERN)
    return local.date().isoformat() if local.time() >= SEND_TIME else None


def gmail_thread_ids(urls):
    result = []
    for url in urls:
        parsed = urlsplit(url)
        match = re.fullmatch(r"(?:all|inbox)/([0-9a-f]+)", parsed.fragment)
        if parsed.scheme == "https" and parsed.hostname == "mail.google.com" and match:
            result.append(match[1])
    return list(dict.fromkeys(result))


def _cell_links(cell):
    yield cell.get("hyperlink", "")
    yield cell.get("userEnteredFormat", {}).get("textFormat", {}).get("link", {}).get("uri", "")
    for run in cell.get("textFormatRuns", []):
        yield run.get("format", {}).get("link", {}).get("uri", "")


def _display_name(name):
    return "Sarah Berger" if name.startswith("die...Sarah") else name.splitlines()[0]


def _slack_text(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("|", "｜")


def render(day, people):
    date = datetime.fromisoformat(day)
    pending = sorted(
        (person for person in people if person["pending"]),
        key=lambda person: person["reply_at"], reverse=True,
    )
    unread = sum(person["unread"] > 0 for person in pending)
    main = (
        f"{MENTIONS} *RingConn KOL 跟进｜{date.month} 月 {date.day} 日*\n"
        f"表内共 {len(people)} 位 KOL，{len(pending)} 位待我方邮件回复或补发资料。\n"
        f"其中有 {unread} 位 KOL 的邮件未读。\n"
        f"<{SHEET_URL}|推进中KOL进展汇总表>"
    )
    unresolved = sum(not person["threads"] for person in people)
    if unresolved:
        main += f"\n另有 {unresolved} 位 KOL 的邮件线程待确认。"

    groups = defaultdict(list)
    for person in pending:
        reply = datetime.fromtimestamp(person["reply_at"] / 1000, EASTERN)
        url = f"https://mail.google.com/mail/u/?authuser={quote(MAILBOX, safe='')}#all/{person['reply_id']}"
        badge = f"（未读 {person['unread']} 封）" if person["unread"] else ""
        name = _slack_text(_display_name(person["name"]))
        groups[(reply.month, reply.day)].append(f"• <{url}|{name}>{badge} · {reply:%H:%M}")
    detail = [f"*待我方回复的 KOL（{len(pending)} 位）*"]
    detail.extend(
        f"*{month} 月 {day} 日来信 · {len(lines)} 位*\n" + "\n".join(lines)
        for (month, day), lines in groups.items()
    )
    return main, "\n\n".join(detail)


def _is_inbound(message):
    labels = set(message.get("labelIds", []))
    return not labels.intersection({"SENT", "DRAFT", "TRASH", "SPAM"}) and not any(
        header["name"].lower() == "from" and MAILBOX in header["value"].lower()
        for header in message.get("payload", {}).get("headers", [])
    )


class RingConnFollowup:
    def __init__(self, sheets, gmail, token, path=STATE_PATH, on_health=None):
        self.sheets = sheets
        self.gmail = gmail
        self.token = token
        self.path = path
        self.state = json.loads(path.read_text())
        self.check_at = None
        self.dirty = True
        self.daily_retry = False
        self.on_health = on_health or (lambda *args: None)

    def _save(self):
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.state, ensure_ascii=False))
        temporary.replace(self.path)

    def _lookup(self, name):
        phrase = _display_name(name).replace('"', "")
        query = f'RingConn "{phrase}" -in:trash -in:spam'
        data = retry_execute(lambda: self.gmail.threads().list(
            userId="me", q=query, maxResults=10, fields="nextPageToken,threads(id)",
        ))
        matches = [thread["id"] for thread in data.get("threads", [])]
        return matches if len(matches) == 1 and not data.get("nextPageToken") else []

    def _roster(self, previous):
        response = retry_execute(lambda: self.sheets.get(
            spreadsheetId=SHEET_ID, ranges=[f"'{TAB}'!B2:O997"],
            fields=("sheets(data(rowData(values(formattedValue,hyperlink,"
                    "userEnteredFormat(textFormat(link)),textFormatRuns(format(link))))))"),
        ))
        rows = response["sheets"][0]["data"][0].get("rowData", [])
        roster = []
        for row in rows:
            cells = row.get("values", [])
            name = cells[0].get("formattedValue", "").strip() if cells else ""
            if not name:
                continue
            link_cell = cells[13] if len(cells) > 13 else {}
            stage = cells[2].get("formattedValue", "") if len(cells) > 2 else ""
            shipping = cells[7].get("formattedValue", "") if len(cells) > 7 else ""
            threads = gmail_thread_ids(_cell_links(link_cell))
            if not threads:
                threads = previous.get(name, {}).get("threads") or self._lookup(name)
            roster.append((name, threads, "已退出" in stage or "已退出" in shipping))
        return roster

    def _messages(self, threads):
        messages = []
        for thread in threads:
            try:
                data = retry_execute(lambda: self.gmail.threads().get(
                    userId="me", id=thread, format="metadata", metadataHeaders=["From"],
                    fields="messages(id,threadId,internalDate,labelIds,payload(headers))",
                ))
            except HttpError as exc:
                if exc.resp.status != 404:
                    raise
                continue
            messages.extend(data.get("messages", []))
        return sorted(messages, key=lambda message: int(message["internalDate"]), reverse=True)

    def _daily_people(self):
        previous = {person["name"]: person for person in self.state["people"]}
        people = []
        for name, threads, withdrawn in self._roster(previous):
            messages = self._messages(threads)
            latest = messages[0] if messages else {}
            reply = next((message for message in messages if _is_inbound(message)), {})
            prior = previous.get(name, {})
            marker = latest.get("id")
            pending = prior.get("pending") if marker == prior.get("marker") else latest == reply
            people.append({
                "name": name, "threads": threads, "marker": marker,
                "pending": bool(reply and pending and not withdrawn),
                "reply_id": reply.get("threadId"),
                "reply_at": int(reply.get("internalDate", 0)),
                "unread": sum(_is_inbound(message) and "UNREAD" in message.get("labelIds", [])
                              for message in messages),
            })
        return people

    def _deliver(self):
        state = self.state
        main, detail = render(state["date"], state["people"])
        options = {"channel": CHANNEL, "unfurl_links": False, "unfurl_media": False}
        for key, text in (("parent", main), ("reply", detail)):
            timestamp = state.get(key)
            if timestamp and state.get(f"{key}_text") == text:
                continue
            if timestamp:
                slack_call(self.token, "chat.update", {**options, "ts": timestamp, "text": text})
            else:
                payload = {**options, "text": text}
                if key == "reply":
                    payload["thread_ts"] = state["parent"]
                response = slack_call(self.token, "chat.postMessage", payload)
                state[key] = response["ts"]
            state[f"{key}_text"] = text
            self._save()

    def _refresh_status(self, changed=None):
        people = self.state["people"]
        for person in people:
            if not (person["pending"] or person.get("replied")):
                continue
            if changed is not None and not changed.intersection(person["threads"]):
                continue
            messages = self._messages(person["threads"])
            # An outbound message in a separate thread cannot clear the action on this thread.
            incoming = next((message for message in messages
                             if _is_inbound(message) and message.get("internalDate")), None)
            if incoming and int(incoming["internalDate"]) > person["reply_at"]:
                person.update(reply_at=int(incoming["internalDate"]), reply_id=incoming["threadId"],
                              pending=True, replied=False)
            outgoing = next((message for message in messages
                             if message.get("threadId") == person["reply_id"]
                             and "SENT" in message.get("labelIds", [])), None)
            if (outgoing and outgoing["id"] != person.get("marker")
                    and int(outgoing["internalDate"]) > person["reply_at"]):
                person.update(pending=False, replied=True, marker=outgoing["id"])
            person["unread"] = sum(
                _is_inbound(message) and "UNREAD" in message.get("labelIds", [])
                for message in messages
            )
        self._save()
        self._deliver()
        self.dirty = False
        if not self.daily_retry:
            self.on_health("recovered")

    def on_history(self, batch):
        if not batch.thread_ids:
            self.dirty = self.dirty or batch.changed
            return
        if not any((p["pending"] or p.get("replied")) and batch.thread_ids.intersection(p["threads"])
                   for p in self.state["people"]):
            return
        try:
            self._refresh_status(None if self.dirty else batch.thread_ids)
        except Exception as exc:
            self.dirty = True
            print(f"RingConn mail status sync failed: {type(exc).__name__}", flush=True)
            self.on_health("failure", "RingConn Gmail status", type(exc).__name__)

    def tick(self, now=None):
        now = now or datetime.now(timezone.utc)
        minute = int(now.timestamp()) // 60
        if minute == self.check_at:
            return
        self.check_at = minute
        try:
            day = due_date(now)
            if day and day > self.state["date"]:
                self.daily_retry = True
                people = self._daily_people()
                self.state = {"date": day, "people": people}
                self._save()
                self._deliver()
                self.dirty = False
                self.daily_retry = False
                self.on_health("recovered")
                print(f"RingConn report sent for {day}", flush=True)
            elif self.dirty or not self.state.get("reply"):
                self.daily_retry = False
                self._refresh_status()
        except Exception as exc:
            self.dirty = True
            print(f"RingConn report sync failed: {type(exc).__name__}", flush=True)
            self.on_health("failure", "RingConn report", type(exc).__name__)
