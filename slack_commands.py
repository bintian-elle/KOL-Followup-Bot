"""Shared implementations for immediate Slack bot commands."""

from collections import Counter
from assign_active import cell
from sync_needs_review import call


def summary_text(active_rows, reach_rows, needs_review_count, sheet_id):
    active = [row for row in active_rows[1:] if cell(row, 0)]
    reach = [row for row in reach_rows[1:] if cell(row, 0) and cell(row, 6).casefold() == "reach out sent"]
    needs_reply = sum(cell(row, 10).casefold() == "needs reply" for row in active)
    owners = Counter(cell(row, 8) or "Unassigned" for row in active)
    owner_lines = "\n".join(f"• {owner}: {count}" for owner, count in sorted(owners.items())) or "• None"
    link = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
    return ("Bluevua KOL Follow-up summary\n"
            f"Active Track: {len(active)}\n"
            f"Needs Reply: {needs_reply}\n"
            f"Needs Review: {needs_review_count}\n"
            "Tasks by team member:\n"
            f"{owner_lines}\n"
            f"Reach Out awaiting replies: {len(reach)}\n"
            f"Bluevua KOL Follow-up Bot working sheet: {link}")


def task_text(active_rows, owner_name):
    tasks = [row for row in active_rows[1:] if cell(row, 0) and cell(row, 8).casefold() == owner_name.casefold()]
    if not tasks:
        return f"You currently have no tasks in Bluevua KOL Active Track, {owner_name}."
    lines = [f"Your current KOL tasks ({len(tasks)}):"]
    for index, row in enumerate(tasks, 1):
        kol = cell(row, 4) or "Unknown KOL"
        status = cell(row, 10).strip().title() or "Unknown"
        link = cell(row, 6)
        lines.append(f"{index}. *{kol}* — {status}\n   <{link}|Open email>")
    return "\n".join(lines)


def remove_all_review_labels(gmail, review_id, active_label_ids=None, thread_ids=None):
    """Remove the whole Bluevua Active label family from Needs Review threads."""
    thread_ids = list(thread_ids) if thread_ids is not None else list_label_threads(gmail, review_id)
    remove_ids = sorted(set(active_label_ids or ()) | {review_id})
    for thread_id in thread_ids:
        call(gmail.threads().modify(userId="me", id=thread_id, body={"removeLabelIds": remove_ids}))
    return len(thread_ids)


def list_label_threads(gmail, label_id):
    thread_ids = []
    token = None
    while True:
        page = call(gmail.threads().list(userId="me", labelIds=[label_id], maxResults=500, pageToken=token))
        thread_ids.extend(item["id"] for item in page.get("threads", []))
        token = page.get("nextPageToken")
        if not token:
            break
    return thread_ids
