"""Remove obsolete Queue events and reconcile Bot Logic without sending Slack."""
import json
import os
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
from kol_followup import OperationalSheetStore, single_instance_lock

load_dotenv()
with single_instance_lock():
    s = OperationalSheetStore(os.environ['GOOGLE_SHEET_ID'], os.environ['GOOGLE_SERVICE_ACCOUNT_FILE'])
    meta = s.api.spreadsheets().get(spreadsheetId=s.sheet_id, fields='sheets(properties)').execute()
    ids = {sh['properties']['title']: sh['properties']['sheetId'] for sh in meta['sheets']}
    q = s.queue_rows()
    logic = s.values("'Bot Logic'!A1:D150")
    target = Path('backups') / ('remove-reactivated-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.json')
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps({'queue': q, 'bot_logic': logic}, ensure_ascii=False, indent=2))
    delete_rows = [n for n,r in enumerate(q,6) if len(r)>8 and r[8]=='Reactivated']
    fixes = {
        '首封邮件规则': '正式扫描强制验证首封 outreach 的首次合格回复；不会因 first_email_only=FALSE 放宽到后续 follow-up。主动来信规则保持不变。',
        'Thread 后续新回复': '已取消重新激活。分配后的后续对话回复不再生成任务；对后续 follow-up 的回复继续排除。',
        '推进轮询': '只有新的 Assigned（Human Reply 或 Needs Review）推进轮询；Ignored 不推进，不再生成 Reactivated。',
        'Reactivated（对话重新激活）': '已取消，不在业务范围内：团队回复后的后续 KOL 回复不再触发分配或通知；旧 Queue Reactivated 记录已移除，Audit 保留历史记录。',
        '重新激活示例': '当前规则：首封 outreach → KOL 首次合格回复 → Assigned。之后团队回复、KOL 再回复均不新增任务。',
        '连续外部消息': '同一线程仅取最初的合格回复事件；后续来回及连续外部消息不产生新任务。',
        '保留原负责人': 'reactivation_keep_owner 已不再被分配逻辑使用；取消重新激活后无须据此创建后续任务。',
        '重新激活记录': '不再写新的 REACTIVATED 事件，也不再因后续对话填写 Previous Owner / Reassigned At / Reassignment Reason。',
        '无先前 Queue 任务': '不再为后续对话创建重新激活候选。只对最初符合首封规则的回复或允许的主动来信执行分配。',
        'Needs Review 的字段归属': 'Needs Review 是 Reply Classification，不是 Assignment Status。缺少线程头、品牌不明确或 Spam 双重证据可形成 Assigned，并要求人工核查。',
        'Slack 当前测试路由': 'Config 的 Testing Channel Active=TRUE 时，任务及汇总只发该 channel；FALSE 时恢复负责人/汇总正常路由。Developer_ID 已不使用。',
    }
    edits=[]
    for n,r in enumerate(logic,1):
        if len(r)>1 and r[1] in fixes:
            edits.append({'range':f"'Bot Logic'!D{n}",'values':[[fixes[r[1]]]]})
        if r and 'Assignment Status 与 Reactivated' in r[0]:
            edits.append({'range':f"'Bot Logic'!A{n}",'values':[['16. Assignment Status 与首次回复范围（Reactivated 已取消）']]})
        if len(r)>1 and r[1]=='事件':
            edits.append({'range':f"'Bot Logic'!D{n}",'values':[['新事件仅含 ASSIGNED、IGNORED、NOTIFICATION_RETRIED 和 DAILY_DIGEST_SENT/SKIPPED；历史 REACTIVATED Audit 保留。']]})
    requests=[{'deleteDimension':{'range':{'sheetId':ids['KOL Followup Queue'],'dimension':'ROWS','startIndex':n-1,'endIndex':n}}} for n in reversed(delete_rows)]
    if requests:
        s._write(s.api.spreadsheets().batchUpdate(spreadsheetId=s.sheet_id,body={'requests':requests}))
    if edits:
        s._write(s.api.spreadsheets().values().batchUpdate(spreadsheetId=s.sheet_id,body={'valueInputOption':'RAW','data':edits}))
    s.refresh_owner_totals()
    s.sort_newest_first()
    remaining=s.queue_rows()
    assert not any(len(r)>8 and r[8]=='Reactivated' for r in remaining)
    for e in edits:
        assert s.values(e['range'])==e['values']
    print('Removed',len(delete_rows),'Reactivated Queue rows; remaining',len(remaining),'Bot Logic corrected',len(edits),'cells; backup',target)
