"""Persist return activities, configuration sign IDs and resource-find quotas."""
import json
import time

from client_rule_loader import load_rules
from core import OperationError
from inventory_service import InventoryService
from lazy_timer import get_daily_5am_ts
from operation_notifications import operation_transaction


def get_rules():
    return load_rules('regression_rules.json')


def _received(state):
    try:
        values = json.loads(state.get('received_sign_list') or '[]')
        if not isinstance(values, list) or any(type(value) is not int for value in values):
            raise ValueError('invalid sign history')
        rules = get_rules()
        sign_ids = [rule['id'] for rule in rules['signs']]
        if not state.get('state_version'):
            legacy_ids = rules['legacy_sign_ids']
            values = [sign_ids[value - 1] if 1 <= value <= len(sign_ids) else
                      sign_ids[legacy_ids.index(value)] if value in legacy_ids else value
                      for value in values]
        if any(value not in sign_ids for value in values):
            raise ValueError('unknown sign in history')
        return list(dict.fromkeys(values))
    except (ValueError, TypeError) as exc:
        raise OperationError(406, '回归签到记录无效') from exc


def _state(db, uid):
    rows = db.query('SELECT * FROM regression WHERE uid=?', (uid,))
    if not rows:
        return None
    state = rows[0]
    if not state.get('state_version'):
        received = _received(state)
        quota = int(state.get('find_time') or 0)
        maximum = get_rules()['initial_find_times'] + get_rules()['find_task_bonus']
        # Timestamp-shaped values came from the old bug; they are not quotas.
        if not 0 <= quota <= maximum:
            quota = 0
        db.execute('''UPDATE regression SET received_sign_list=?,find_time=?,state_version=1
            WHERE uid=?''', (json.dumps(received), quota, uid))
        state.update(received_sign_list=json.dumps(received), find_time=quota, state_version=1)
    return state


def _unlocked_signs(state, now):
    start = int(state.get('trigger_time') or 0)
    if start <= 0 or now < start or now >= int(state.get('end_ts') or 0):
        return 0
    days = (get_daily_5am_ts(now) - get_daily_5am_ts(start)) // 86400 + 1
    return min(len(get_rules()['signs']), max(0, days))


def _active_state(db, uid, now):
    state = _state(db, uid)
    if not state or not _unlocked_signs(state, now):
        raise OperationError(2, '回归活动尚未激活或已结束')
    if int(state.get('return_vs_id') or 0) != get_rules()['version_id']:
        raise OperationError(406, '回归活动版本未配置')
    user = db.query('SELECT level FROM users WHERE uid=?', (uid,))
    if not user or int(user[0]['level'] or 0) < get_rules()['min_account_level']:
        raise OperationError(2, '账号等级未达到回归活动条件')
    return state


def _credit_task(db, uid, state):
    cfg = get_rules()
    rows = db.query('''SELECT t.claimed_ts FROM task t JOIN task_cfg c USING(task_id)
        WHERE t.uid=? AND t.task_id=? AND t.complete_flag=1 AND t.progress>=c.need
        AND c.need>0 AND t.claimed_ts>=? AND t.claimed_ts>0''',
        (uid, cfg['find_task_id'], state['trigger_time']))
    timestamp = int(rows[0]['claimed_ts']) if rows else 0
    if timestamp and not int(state.get('find_task_claimed_ts') or 0):
        quota = int(state.get('find_time') or 0) + cfg['find_task_bonus']
        db.execute('UPDATE regression SET find_time=?,find_task_claimed_ts=? WHERE uid=?',
                   (quota, timestamp, uid))
        state.update(find_time=quota, find_task_claimed_ts=timestamp)


def _activate_for_login(db, uid, state, now):
    rules = get_rules()
    if state and int(state.get('end_ts') or 0) > now:
        return state
    user = db.query('SELECT level FROM users WHERE uid=?', (uid,))
    watermark = db.query('SELECT last_logout_ts FROM user_timer_watermark WHERE uid=?', (uid,))
    offline = int(watermark[0]['last_logout_ts'] or 0) if watermark else 0
    old_start = int(state.get('trigger_time') or 0) if state else 0
    if (not user or int(user[0]['level'] or 0) < rules['min_account_level'] or
            offline <= old_start or now - offline < rules['min_offline_days'] * 86400):
        return state
    # This is a new qualifying activity instance, never a renewal on reconnect.
    db.execute('''INSERT INTO regression(uid,trigger_time,end_ts,return_vs_id,
        receive_sign_index,received_sign_list,find_time,find_task_claimed_ts,state_version,update_ts)
        VALUES(?,?,?,?,1,'[]',?,0,1,?) ON CONFLICT(uid) DO UPDATE SET
        trigger_time=excluded.trigger_time,end_ts=excluded.end_ts,return_vs_id=excluded.return_vs_id,
        receive_sign_index=1,received_sign_list='[]',find_time=excluded.find_time,
        find_task_claimed_ts=0,state_version=1,update_ts=excluded.update_ts''',
        (uid, now, now + rules['duration_days'] * 86400, rules['version_id'], rules['initial_find_times'], now))
    task_id = rules['find_task_id']
    cfg = db.query('SELECT name,task_type FROM task_cfg WHERE task_id=?', (task_id,))
    if not cfg:
        raise OperationError(406, '回归资源找回任务配置缺失')
    db.execute('''INSERT INTO task(uid,task_id,name,task_type,progress,complete_flag,expired_ts,claimed_ts)
        VALUES(?,?,?,?,0,0,?,0) ON CONFLICT(uid,task_id) DO UPDATE SET
        progress=0,complete_flag=0,claimed_ts=0,expired_ts=excluded.expired_ts''',
        (uid, task_id, cfg[0]['name'], cfg[0]['task_type'], now + rules['duration_days'] * 86400))
    db.execute("DELETE FROM claim_ledger WHERE uid=? AND kind='task' AND key_id=?", (uid, task_id))
    return _state(db, uid)


def build_payload(db, uid, now_ts=None):
    now = int(time.time() if now_ts is None else now_ts)
    with db.transaction(immediate=True):
        state = _activate_for_login(db, uid, _state(db, uid), now) or {}
        unlocked = _unlocked_signs(state, now)
        received = _received(state)
        active = bool(unlocked)
        if active:
            _credit_task(db, uid, state)
        sign_ids = [rule['id'] for rule in get_rules()['signs'][:unlocked]]
        available = sum(sign not in received for sign in sign_ids)
        if state and int(state.get('receive_sign_index') or 0) != available:
            db.execute('UPDATE regression SET receive_sign_index=? WHERE uid=?', (available, uid))
        end = int(state.get('end_ts') or 0)
        return {
            'end_timestamps': end, 'return_vs_id': int(state.get('return_vs_id') or get_rules()['version_id']),
            'return_level': 1 if active else 0, 'mul_times': 1 if active else 0,
            'receive_sign_index': available, 'received_sign_list': received,
            'left_day': max(0, (end - now + 86399) // 86400) if active else 0,
            'find_time': int(state.get('find_time') or 0) if active else 0,
            'open_draw_sign': active, 'pic_sign': False,
        }


def claim_sign(ctx, uid, index):
    with operation_transaction(ctx):
        now = int(time.time())
        state = _active_state(ctx.db, uid, now)
        signs = get_rules()['signs']
        if not 1 <= index <= _unlocked_signs(state, now):
            raise OperationError(2, '回归签到奖励尚未开放或档位无效')
        sign = signs[index - 1]
        received = _received(state)
        if sign['id'] in received:
            raise OperationError(2, '该回归签到奖励已领取')
        received.append(sign['id'])
        unlocked = _unlocked_signs(state, now)
        available = sum(rule['id'] not in received for rule in signs[:unlocked])
        ctx.db.execute('''UPDATE regression SET received_sign_list=?,receive_sign_index=?,update_ts=?
            WHERE uid=?''', (json.dumps(received), available, now, uid))
        InventoryService.grant_items(ctx, uid, [(item['id'], item['num']) for item in sign['items']], source='regression_sign')
        return sign['items']


def claim_resources(ctx, uid):
    with operation_transaction(ctx):
        now = int(time.time())
        state = _active_state(ctx.db, uid, now)
        _credit_task(ctx.db, uid, state)
        quota = int(state.get('find_time') or 0)
        maximum = get_rules()['initial_find_times'] + get_rules()['find_task_bonus']
        if not 0 < quota <= maximum:
            raise OperationError(2, '回归资源找回次数不足或记录无效')
        ctx.db.execute('UPDATE regression SET find_time=?,update_ts=? WHERE uid=?', (quota - 1, now, uid))
        rewards = get_rules()['find_rewards']
        InventoryService.grant_items(ctx, uid, [(item['id'], item['num']) for item in rewards], source='regression_find')
        return rewards
