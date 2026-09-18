"""Read-only overdue preview: no Slack, Sheets writes or local reminder state."""
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from kol_followup import OperationalSheetStore, GmailReader
from unreplied_reminders import check_reminders

load_dotenv()
store = OperationalSheetStore(os.environ['GOOGLE_SHEET_ID'], os.environ['GOOGLE_SERVICE_ACCOUNT_FILE'])
owners, settings, _ = store.config()
preview = []
check_reminders(GmailReader(), store, owners, settings,
                datetime.now(ZoneInfo(settings.get('timezone','America/Los_Angeles'))),
                dry_run=True, preview=preview)
print(json.dumps(sorted(preview,key=lambda r:r['waiting_hours'],reverse=True),ensure_ascii=False,indent=2))
