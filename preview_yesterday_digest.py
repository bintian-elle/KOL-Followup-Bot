"""Read-only Sheet preview; send yesterday's Assigned details to Testing Channel."""
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from kol_followup import OperationalSheetStore, SlackNotifier, maybe_send_daily_digest

load_dotenv()
s = OperationalSheetStore(os.environ['GOOGLE_SHEET_ID'], os.environ['GOOGLE_SERVICE_ACCOUNT_FILE'])
_, settings, _ = s.config()
assert settings.get('testing_channel_active','').upper() == 'TRUE', 'Testing Channel must be active'
channel = settings['testing_channel_id']
settings = dict(settings, pilot_mode='FALSE', production_channel_id=channel,
                skip_empty_digest='FALSE')
queue = [r for r in s.queue_rows() if len(r)>8 and r[8]=='Assigned']
now = datetime.now(ZoneInfo(settings.get('timezone','America/Los_Angeles')))
class PreviewStore:
    def append_audit(self, row):
        print(row[-1])
    def update_setting(self, *args):
        pass
# Use exactly yesterday's calendar day, not a business-day aggregation window.
settings['holiday_calendar']=''
settings['daily_send_time']='00:00'
import kol_followup
from unittest.mock import patch
start = (now-timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
end = now.replace(hour=0,minute=0,second=0,microsecond=0)
with patch.object(kol_followup,'digest_window',return_value=(start,end)):
    maybe_send_daily_digest(PreviewStore(),queue,settings,{},
        SlackNotifier(os.environ['SLACK_BOT_TOKEN']),now,force=True)
print('Preview sent to Testing Channel for',start.date())
