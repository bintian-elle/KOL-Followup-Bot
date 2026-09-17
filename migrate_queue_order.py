"""One-off preservation-first migration of ignored Queue rows to Audit."""
import json
from datetime import datetime
from pathlib import Path
import os
from dotenv import load_dotenv
from kol_followup import OperationalSheetStore, QUEUE_HEADERS, single_instance_lock


def main():
    load_dotenv()
    with single_instance_lock():
        store = OperationalSheetStore(os.environ['GOOGLE_SHEET_ID'], os.environ['GOOGLE_SERVICE_ACCOUNT_FILE'])
        meta = store.api.spreadsheets().get(spreadsheetId=store.sheet_id, fields='sheets(properties)').execute()
        props = {s['properties']['title']: s['properties'] for s in meta['sheets']}
        queue, audit = store.queue_rows(), store.audit_rows()
        backup = Path('backups')
        backup.mkdir(exist_ok=True)
        target = backup / ('queue-audit-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.json')
        target.write_text(json.dumps({'queue': queue, 'audit': audit}, ensure_ascii=False, indent=2))
        index = {h: i for i, h in enumerate(QUEUE_HEADERS)}
        ignored = [(n, r + [''] * (31-len(r))) for n, r in enumerate(queue, 6)
                   if len(r) > 8 and r[8] == 'Ignored']
        known = '\n'.join(r[11] for r in audit if len(r) > 11)
        additions = []
        for _, r in ignored:
            mid = r[index['Gmail Message ID']]
            if mid and f'gmail_message:{mid}' not in known:
                additions.append([r[1], 'IGNORED', r[0], r[25],
                                  f'gmail_thread:{r[29]}', r[3], '', '',
                                  'KOL Follow-up Automation', r[11], 'Success',
                                  f'Migrated from Queue; gmail_message:{mid}; email:{r[4]}; subject:{r[2]}'])
        if additions:
            store._write(store.api.spreadsheets().values().append(
                spreadsheetId=store.sheet_id, range="'KOL Followup Audit Log'!A:L",
                valueInputOption='RAW', insertDataOption='INSERT_ROWS', body={'values': additions}))
        requests = [{'deleteDimension': {'range': {
            'sheetId': props['KOL Followup Queue']['sheetId'], 'dimension': 'ROWS',
            'startIndex': n-1, 'endIndex': n}}} for n, _ in reversed(ignored)]
        if requests:
            store._write(store.api.spreadsheets().batchUpdate(
                spreadsheetId=store.sheet_id, body={'requests': requests}))
        store.sort_newest_first()
        remaining = store.queue_rows()
        assert all(len(r) <= 8 or r[8] != 'Ignored' for r in remaining)
        after_audit = store.audit_rows()
        for name, rows, col in [('Queue', remaining, 1), ('Audit', after_audit, 0)]:
            dates = [datetime.strptime(r[col], '%m/%d/%Y %H:%M') for r in rows if len(r) > col and r[col]]
            assert dates == sorted(dates, reverse=True), name
            print(name, 'verified descending:', len(rows), 'rows')
        after_known = '\n'.join(r[11] for r in after_audit if len(r) > 11)
        assert all(not r[28] or f'gmail_message:{r[28]}' in after_known for _, r in ignored)
        print(f'Removed {len(ignored)} Ignored Queue rows; preserved {len(additions)} Audit records; Queue now {len(remaining)} rows; backup: {target}')


if __name__ == '__main__':
    main()
