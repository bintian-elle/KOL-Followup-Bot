"""Document implemented Assignment Status behavior in the existing Bot Logic tab."""
import os
from dotenv import load_dotenv
from kol_followup import OperationalSheetStore

load_dotenv()
s = OperationalSheetStore(os.environ['GOOGLE_SHEET_ID'], os.environ['GOOGLE_SERVICE_ACCOUNT_FILE'])
rows = s.values("'Bot Logic'!A1:D150")
changes = []
replacements = {
    'Ignored 写入': 'Ignored 不再写入 Queue；只写 Audit，Details 保留 gmail_message:<message_id> 参与去重，不分配负责人、不推进轮询。历史 Ignored 已迁移。',
    '扫描重叠': '5 分钟扫描重叠通过 Queue Gmail Message ID 和 Audit Details 的 gmail_message:<message_id> 去重；不永久封锁 thread。',
    '推进轮询': '首次有效分配（Human Reply 或 Needs Review）推进轮询；Ignored 不推进。已识别为 Reactivated 的事件不推进轮询。',
}
for n, row in enumerate(rows, 1):
    if len(row) > 1 and row[1] in replacements:
        changes.append({'range': f"'Bot Logic'!D{n}", 'values': [[replacements[row[1]]]]})
new_rows = [
    ['16. Assignment Status 与 Reactivated 逻辑', '', '', 'Assignment Status 表示分配事件，不等于邮件分类或 Slack 发送状态。'],
    ['', 'Assigned（已分配）', '[程序 + Config]', '符合业务规则、需跟进且未被识别为已有任务重新激活的邮件。按 Config 的 active owners 和 last_round_robin_owner 分配，推进轮询；写 Queue 并通知。'],
    ['', 'Reactivated（对话重新激活）', '[程序]', '已处理过的线程中，初始合格事件之后我方发送 SENT 邮件，随后收到新的真人外部回复；该回复通过品牌/收尾等业务判断，并且 Queue 存在该线程的先前任务，才写 Reactivated。不是新的 KOL。'],
    ['', '重新激活示例', '[程序]', 'KOL 首次回复 → 分配 Dan → 我方回复 → KOL 再回复 → 新建 Reactivated 跟进事件，保留独立 Gmail Message ID 和 Assignment ID。'],
    ['', '连续外部消息', '[程序]', '每次我方 SENT 后只取下一条合格真人外部消息为重新激活候选；KOL 连续发多条而我方未再回复，不逐条产生 Reactivated。'],
    ['', '保留原负责人', 'Config + [程序]', 'reactivation_keep_owner=TRUE 且原负责人仍 active 时沿用原负责人。FALSE 或原负责人不再 active 时选择轮询指针的下一位；当前 Reactivated 分支仍不推进指针。'],
    ['', '重新激活记录', '[程序]', '写 Previous Owner、Reassigned At 和 Reassignment Reason，Audit 事件为 REACTIVATED。默认保留原负责人；不占用下一次首次分配名额。'],
    ['', '无先前 Queue 任务', '[程序]', '若 Gmail 内出现重新激活候选，但 Queue 找不到该 thread 的先前任务，按 Assigned 处理并推进轮询，无法恢复历史负责人。'],
    ['', 'Reassigned（重新分配）', '[程序：预留]', '表示任务变更负责人；当前 Worker 支持对此状态的通知重试和每日统计，但没有自动改派流程，不会自动生成 Reassigned。'],
    ['', 'Ignored（忽略）', '[程序 + Config]', '不符合品牌/相关性规则或属于合作收尾等内容。不写 Queue、不发送任务 Slack、不推进轮询；只记 Audit 的 IGNORED，保留邮件 ID 去重。'],
    ['', 'Needs Review 的字段归属', '[程序 + Config]', 'Needs Review 属于 Reply Classification，而非 Assignment Status。缺少线程头、品牌不明确或 Spam 双重证据等邮件仍可 Assigned/Reactivated，并要求人工核查。'],
    ['', 'Notification Status 的字段归属', '[程序]', 'PENDING / Sent / Failed 只表示 Slack 通知状态，不表示 KOL 是否已跟进完成，也不替代 Assignment Status。'],
    ['', 'Slack 当前测试路由', '[.env]', 'SLACK_OVERRIDE_USER_ID 或 Developer_ID 配置后，所有任务和每日汇总统一发 Developer DM；Sheet 的 Assigned Owner 仍是真实负责人。'],
    ['', '历史与新增记录显示', '[程序]', 'Queue 按 Reply Received At 倒序，Audit 按 Timestamp 倒序；不同日期之间显示蓝色分界线，每次正式运行结束刷新。'],
]
start = len(rows) + 2
changes.append({'range': f"'Bot Logic'!A{start}:D{start+len(new_rows)-1}", 'values': new_rows})
s._write(s.api.spreadsheets().values().batchUpdate(
    spreadsheetId=s.sheet_id, body={'valueInputOption': 'RAW', 'data': changes}))
assert s.values(f"'Bot Logic'!A{start}:D{start+len(new_rows)-1}") == new_rows
print('Verified Bot Logic', f'A{start}:D{start+len(new_rows)-1}', 'and corrected', len(changes)-1, 'existing rules')
