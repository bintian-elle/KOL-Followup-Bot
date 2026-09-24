"""Small JSON state records stored in the hidden Bot_State Sheet tab."""

import json
from datetime import datetime, timezone


TAB = "Bot_State"
HEADER = ["Namespace", "Purpose", "JSON Value", "Updated At"]
PURPOSES = {
    "assignment_slack_sent": "New-task Slack delivery receipts; allows retry when assignment saved but Slack failed",
    "reply_reminders_sent": "One-time 24-hour reminder receipts for each KOL reply",
    "reach_out_replies": "First-reply progress for moving Reach Out threads directly into Active tracking",
    "needs_review_monitor": "Needs Review membership and manually removed threads that must not be re-added",
    "needs_review_scan": "Incremental Gmail scan checkpoint",
    "daily_digest": "Daily Digest snapshot, last-send date, route, and Slack delivery receipt",
}


class SheetState:
    def __init__(self, sheets, spreadsheet_id):
        self.sheets = sheets
        self.spreadsheet_id = spreadsheet_id
        self.sheet_id = self._ensure_tab()

    def _ensure_tab(self):
        metadata = self.sheets.get(
            spreadsheetId=self.spreadsheet_id,
            fields="sheets(properties(sheetId,title,hidden))",
        ).execute()
        found = next((sheet for sheet in metadata["sheets"] if sheet["properties"]["title"] == TAB), None)
        if found:
            return found["properties"]["sheetId"]
        response = self.sheets.batchUpdate(spreadsheetId=self.spreadsheet_id, body={"requests": [
            {"addSheet": {"properties": {"title": TAB, "hidden": False,
                                           "gridProperties": {"frozenRowCount": 1, "rowCount": 1000, "columnCount": 4}}}},
        ]}).execute()
        sheet_id = response["replies"][0]["addSheet"]["properties"]["sheetId"]
        self.sheets.values().update(
            spreadsheetId=self.spreadsheet_id, range=f"'{TAB}'!A1:D1",
            valueInputOption="RAW", body={"values": [HEADER]},
        ).execute()
        self.sheets.batchUpdate(spreadsheetId=self.spreadsheet_id, body={"requests": [
            {"repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1,
                                        "startColumnIndex": 0, "endColumnIndex": 4},
                            "cell": {"userEnteredFormat": {"backgroundColor": {"red": 0.12, "green": 0.47, "blue": 0.71},
                                                            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}}},
                            "fields": "userEnteredFormat(backgroundColor,textFormat)"}},
            {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
                                             "properties": {"pixelSize": 220}, "fields": "pixelSize"}},
            {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 2},
                                             "properties": {"pixelSize": 420}, "fields": "pixelSize"}},
            {"updateDimensionProperties": {"range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": 2, "endIndex": 3},
                                             "properties": {"pixelSize": 500}, "fields": "pixelSize"}},
        ]}).execute()
        return sheet_id

    def _rows(self):
        rows = self.sheets.values().get(
            spreadsheetId=self.spreadsheet_id, range=f"'{TAB}'!A1:D1000",
        ).execute().get("values", [])
        if not rows or rows[0] != HEADER:
            raise RuntimeError("Bot_State header differs from expected columns")
        return rows

    def load(self, namespace, default=None):
        for row in self._rows()[1:]:
            if row and row[0] == namespace:
                return json.loads(row[2]) if len(row) > 2 and row[2] else default
        return default

    def save(self, namespace, value):
        rows = self._rows()
        row_number = next((index for index, row in enumerate(rows[1:], 2) if row and row[0] == namespace), len(rows) + 1)
        self.sheets.values().update(
            spreadsheetId=self.spreadsheet_id, range=f"'{TAB}'!A{row_number}:D{row_number}",
            valueInputOption="RAW", body={"values": [[namespace, PURPOSES.get(namespace, "Bot runtime state"), json.dumps(value, sort_keys=True),
                                                        datetime.now(timezone.utc).isoformat()]]},
        ).execute()
