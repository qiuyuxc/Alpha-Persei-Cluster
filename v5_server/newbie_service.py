"""Newbie rewards use the saved activity version and permanent claim records."""
import time

from client_rule_loader import load_rules
from core import OperationError
from inventory_service import InventoryService
from lazy_timer import get_daily_5am_ts
from operation_notifications import operation_transaction


def get_rules():
    return load_rules('newbie_rewards.json')


def active_state(db, uid, now_ts):
    rows = db.query('SELECT * FROM newbie_activity WHERE uid=?', (uid,))
    if not rows:
        raise OperationError(406, '新手活动记录不存在')
    state = rows[0]
    version = get_rules()['versions'].get(str(state['version_id']))
    if version is None:
        raise OperationError(406, '新手活动版本未配置')
    start = int(state.get('trigger_time') or 0)
    completed = int(state.get('completed_time') or 0)
    expiry = start + int(get_rules()['duration_days']) * 86400
    if start <= 0 or now_ts < start or now_ts >= expiry or (completed and now_ts >= completed):
        raise OperationError(2, '新手活动尚未开放或已结束')
    return state, version


def claim_sign(ctx, uid):
    with operation_transaction(ctx):
        now = int(time.time())
        state, version = active_state(ctx.db, uid, now)
        count = int(state.get('now_sign_times') or 0)
        if not 0 <= count < len(version['signs']):
            raise OperationError(2, '新手签到奖励已全部领取')
        if int(state.get('last_sign_ts') or 0) >= get_daily_5am_ts(now):
            raise OperationError(2, '本游戏日已签到')
        reward = version['signs'][count]['items']
        ctx.db.execute('UPDATE newbie_activity SET now_sign_times=?,last_sign_ts=?,update_ts=? WHERE uid=?',
                       (count + 1, now, now, uid))
        InventoryService.grant_items(ctx, uid, [(item['id'], item['num']) for item in reward], source='newbie_sign')
        return reward


def claim_level(ctx, uid, level):
    with operation_transaction(ctx):
        now = int(time.time())
        active_state(ctx.db, uid, now)
        reward = get_rules()['level_rewards'].get(str(level))
        if not reward:
            raise OperationError(2, '新手等级奖励档位无效')
        user = ctx.db.query('SELECT level FROM users WHERE uid=?', (uid,))
        if not user or int(user[0].get('level') or 0) < level:
            raise OperationError(2, '账号等级未达到领奖条件')
        if ctx.db.query('SELECT 1 FROM newbie_level_reward WHERE uid=? AND level=?', (uid, level)):
            raise OperationError(2, '该等级奖励已领取')
        ctx.db.execute('INSERT INTO newbie_level_reward(uid,level,update_ts) VALUES(?,?,?)', (uid, level, now))
        InventoryService.grant_items(ctx, uid, [(item['id'], item['num']) for item in reward], source='newbie_level')
        return reward


def claim_progress(ctx, uid, reward_id):
    with operation_transaction(ctx):
        now = int(time.time())
        _, version = active_state(ctx.db, uid, now)
        rule = version['progress_rewards'].get(str(reward_id))
        if not rule:
            raise OperationError(2, '新手累计任务奖励档位无效')
        if ctx.db.query('SELECT 1 FROM newbie_pt_reward WHERE uid=? AND pt_id=?', (uid, reward_id)):
            raise OperationError(2, '该累计任务奖励已领取')
        # TaskTools.GetNoobCompletedList counts submitted tasks of this version.
        completed = ctx.db.query('''SELECT COUNT(*) AS count FROM task t JOIN task_cfg c USING(task_id)
            WHERE t.uid=? AND t.task_type=? AND c.task_type=? AND t.complete_flag>=1''',
            (uid, version['task_type'], version['task_type']))[0]['count']
        if completed < int(rule['need']):
            raise OperationError(2, '已领取的新手任务数量不足')
        ctx.db.execute('INSERT INTO newbie_pt_reward(uid,pt_id,update_ts) VALUES(?,?,?)', (uid, reward_id, now))
        InventoryService.grant_items(ctx, uid, [(item['id'], item['num']) for item in rule['items']], source='newbie_progress')
        return rule['items']
