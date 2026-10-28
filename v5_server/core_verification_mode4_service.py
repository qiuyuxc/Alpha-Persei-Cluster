"""Mode 4 reward eligibility comes from the matching persisted stage progress."""
import time
import json

from client_rule_loader import load_rules
from core import OperationError
from inventory_service import InventoryService
from operation_notifications import operation_transaction


def get_rules():
    return load_rules('core_verification_mode4_rules.json')


def canonical_activity(activity_id, *, strict=True):
    for key, activity in get_rules()['activities'].items():
        if int(activity_id) in activity['aliases']:
            return int(key)
    if strict:
        raise OperationError(406, '未知的 Mode 4 活动')
    return None


def _aliases(activity_id):
    return get_rules()['activities'][str(activity_id)]['aliases']


def claimed_tasks(db, uid, activity_id):
    aliases = _aliases(activity_id)
    slots = ','.join('?' for _ in aliases)
    rows = db.query(f'''SELECT task_id FROM core_verification_cl_task_claimed
        WHERE uid=? AND activity_id IN ({slots})''', (uid, *aliases))
    return {row['task_id'] for row in rows}


def stage_progress(db, uid, activity_id):
    aliases = _aliases(activity_id)
    slots = ','.join('?' for _ in aliases)
    rows = db.query(f'''SELECT * FROM core_verification_cl_progress
        WHERE uid=? AND activity_id IN ({slots}) AND mode=4''', (uid, *aliases))
    progress = {}
    for row in rows:
        row['heroes'] = json.loads(row.get('heroes_json') or '[]')
        row['select_buffs'] = json.loads(row.get('select_buffs') or '[]')
        old = progress.get(row['stage_id'])
        if old is None or (int(row['is_cleared'] or 0), int(row['score'] or 0)) > (
                int(old['is_cleared'] or 0), int(old['score'] or 0)):
            progress[row['stage_id']] = row
    return progress


def _complete(rule, progress):
    condition = rule['condition']
    params = condition['params']
    row = progress.get(params[0]) or {}
    if int(row.get('is_cleared') or 0) != 1:
        return False
    if condition['type'] == 11304:
        return True
    if condition['type'] == 11307:
        return int(row.get('score') or 0) >= params[1]
    raise OperationError(406, 'Mode 4 奖励条件配置无效')


def task_states(db, uid, activity_id):
    activity_id = canonical_activity(activity_id)
    claimed = claimed_tasks(db, uid, activity_id)
    progress = stage_progress(db, uid, activity_id)
    return [{'assignment_id': int(key), 'state': 2 if int(key) in claimed else 1 if _complete(rule, progress) else 0}
            for key, rule in get_rules()['tasks'].items() if rule['activity_id'] == activity_id]


def claim_rewards(ctx, uid, activity_id, task_ids):
    with operation_transaction(ctx):
        activity_id = canonical_activity(activity_id)
        if not isinstance(task_ids, (list, tuple)) or not task_ids:
            raise OperationError(2, 'Mode 4 领奖任务列表为空或无效')
        task_ids = list(dict.fromkeys(int(value) for value in task_ids))
        claimed = claimed_tasks(ctx.db, uid, activity_id)
        progress = stage_progress(ctx.db, uid, activity_id)
        plans = []
        for tid in task_ids:
            rule = get_rules()['tasks'].get(str(tid))
            if not rule or rule['activity_id'] != activity_id:
                raise OperationError(2, '奖励任务不属于该 Mode 4 活动')
            if tid in claimed:
                continue
            if not _complete(rule, progress):
                raise OperationError(2, 'Mode 4 奖励任务尚未完成')
            if not rule.get('items'):
                raise OperationError(406, 'Mode 4 奖励配置缺失')
            plans.append((tid, rule['items']))
        rewards = []
        for tid, items in plans:
            changed = ctx.db.execute('''INSERT OR IGNORE INTO core_verification_cl_task_claimed
                (uid,activity_id,task_id,update_ts) VALUES(?,?,?,?)''',
                (uid, activity_id, tid, int(time.time())))
            if changed.rowcount:
                InventoryService.grant_items(ctx, uid, [(item['id'], item['num']) for item in items], source='core_verification_mode4')
                rewards.extend(items)
        return rewards
