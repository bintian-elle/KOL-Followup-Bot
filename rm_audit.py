"""Visible Google Sheet audit trail for destructive-looking `rm` label changes."""

from datetime import datetime, timezone


TAB = "RM_Audit"
HEADER = ["Batch ID", "Removed At", "Requested By", "Gmail Thread ID", "Subject",
          "Gmail Thread Link", "Previous Bluevua Labels", "Status", "Restored At"]


def ensure_tab(sheets, spreadsheet_id):
    metadata = sheets.get(spreadsheetId=spreadsheet_id,
                          fields="sheets(properties(sheetId,title))").execute()
    if any(item["properties"]["title"] == TAB for item in metadata["sheets"]):
        rows = sheets.values().get(spreadsheetId=spreadsheet_id, range=f"'{TAB}'!A1:I1").execute().get("values", [])
        if not rows or rows[0] != HEADER:
            raise RuntimeError("RM_Audit header differs from expected columns")
        return
    response = sheets.batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": [{
        "addSheet": {"properties": {"title": TAB, "hidden": False,
                                      "gridProperties": {"frozenRowCount": 1, "rowCount": 1000,
                                                         "columnCount": len(HEADER)}}},
    }]}).execute()
    sheet_id = response["replies"][0]["addSheet"]["properties"]["sheetId"]
    sheets.values().update(spreadsheetId=spreadsheet_id, range=f"'{TAB}'!A1:I1",
                           valueInputOption="RAW", body={"values": [HEADER]}).execute()
    sheets.batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": [{
        "repeatCell": {"range": {"sheetId": sheet_id, "startRowIndex": 0, "endRowIndex": 1},
                       "cell": {"userEnteredFormat": {
                           "backgroundColor": {"red": 0.12, "green": 0.47, "blue": 0.71},
                           "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}}}},
                       "fields": "userEnteredFormat(backgroundColor,textFormat)"},
    }]}).execute()


def append_batch(sheets, spreadsheet_id, batch_id, requested_by, snapshots):
    removed_at = datetime.now(timezone.utc).isoformat()
    rows = [[batch_id, removed_at, requested_by, item["thread_id"], item["subject"],
             f"https://mail.google.com/mail/u/0/#all/{item['thread_id']}",
             ", ".join(item["labels"]), "Pending", ""] for item in snapshots]
    if rows:
        sheets.values().append(spreadsheetId=spreadsheet_id, range=f"'{TAB}'!A:I",
                               valueInputOption="RAW", insertDataOption="INSERT_ROWS",
                               body={"values": rows}).execute()
    return len(rows)


def mark_batch_removed(sheets, spreadsheet_id, batch_id):
    rows = sheets.values().get(spreadsheetId=spreadsheet_id, range=f"'{TAB}'!A2:I10000").execute().get("values", [])
    updates = []
    for row_number, row in enumerate(rows, 2):
        if row and row[0] == batch_id:
            updates.append({"range": f"'{TAB}'!H{row_number}", "values": [["Removed"]]})
    if updates:
        sheets.values().batchUpdate(spreadsheetId=spreadsheet_id,
                                    body={"valueInputOption": "RAW", "data": updates}).execute()
    return len(updates)
