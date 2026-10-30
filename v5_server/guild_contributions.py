"""Shared guild rewards, booked once from task and rift reward claims.

Native type-17 items 52001/52002/52003 are guild coin, guild experience and
member vitality. Daily task points, reputation and battle score are separate.
"""
import json
import time

from account_db import savepoint_transaction
from lazy_timer import get_daily_5am_ts, get_weekly_mon_5am_ts

KEY = 'gm.guild_player_contributions.v1'
RESOURCE_IDS = {52001: 'coin', 52002: 'guild_exp', 52003: 'vitality'}


def ready(db):
    return bool(db.query('SELECT 1 FROM server_global_config WHERE key=?', (KEY,)))


def ensure_tasks(db, uid):
    db.execute('INSERT OR IGNORE INTO task '
               '(uid,task_id,name,task_type,progress,complete_flag,expired_ts,claimed_ts) '
               'SELECT ?,task_id,name,task_type,0,0,0,0 FROM task_cfg '
               'WHERE task_type=11 AND COALESCE(activity_id,0)=0', (uid,))


def totals(db, guild_id, week):
    return db.query(
        'SELECT COALESCE(SUM(guild_exp),0) AS total_exp,'
        'COALESCE(SUM(CASE WHEN day_id>=? THEN guild_exp ELSE 0 END),0) AS weekly_exp,'
        'COALESCE(SUM(CASE WHEN day_id>=? THEN coin ELSE 0 END),0) AS weekly_coin,'
        'COALESCE(SUM(CASE WHEN day_id<? AND day_id>=? THEN vitality ELSE 0 END),0) AS previous '
        'FROM social_player_contributions WHERE guild_id=?',
        (week, week, week, week - 7 * 86400, guild_id))[0]


def member_totals(db, guild_id, uid, week):
    return db.query(
        'SELECT COALESCE(SUM(vitality),0) AS total,'
        'COALESCE(SUM(CASE WHEN day_id>=? THEN vitality ELSE 0 END),0) AS weekly '
        'FROM social_player_contributions WHERE guild_id=? AND uid=?', (week, guild_id, uid))[0]


def _book(db, member, kind, key, rewards, event_at, now):
    if not int(member['joined_at']) <= event_at <= now or event_at <= 0:
        return False
    if db.query('SELECT 1 FROM social_player_contributions WHERE uid=? AND source_kind=? AND source_key=?',
                (member['uid'], kind, key)):
        return False
    values = dict(coin=0, guild_exp=0, vitality=0)
    for reward in rewards:
        if not isinstance(reward, (list, tuple)) or len(reward) != 2:
            raise ValueError('公会奖励格式无效')
        iid, count = reward
        if iid in RESOURCE_IDS:
            if type(count) is not int or not 0 <= count <= 2147483647:
                raise ValueError('公会奖励数量无效')
            values[RESOURCE_IDS[iid]] += count
    if not any(values.values()):
        return False
    guilds = db.query('SELECT exp FROM social_guilds WHERE id=?', (member['guild_id'],))
    if not guilds:
        return False
    import social_service as social
    week = get_weekly_mon_5am_ts(event_at)
    added = totals(db, member['guild_id'], week)
    legacy = db.query('SELECT COALESCE(SUM(vitality),0) AS xp,COALESCE(SUM(donation),0) AS coin '
                      'FROM social_contributions WHERE guild_id=? AND day_id>=?',
                      (member['guild_id'], week))[0]
    level = social.guild_level(guilds[0]['exp'] + added['total_exp'])
    cfg = social.CATALOG['levels'][str(level)]
    values['guild_exp'] = (min(values['guild_exp'], max(0, cfg['max_exp_per_week']
                                  - legacy['xp'] - added['weekly_exp'])) if level < 10 else 0)
    values['coin'] = min(values['coin'], max(0, cfg['max_currency_per_week']
                                           - legacy['coin'] - added['weekly_coin']))
    db.execute('INSERT INTO social_player_contributions '
               '(uid,source_kind,source_key,guild_id,day_id,vitality,guild_exp,coin,event_at) VALUES(?,?,?,?,?,?,?,?,?)',
               (member['uid'], kind, key, member['guild_id'], get_daily_5am_ts(event_at),
                values['vitality'], values['guild_exp'], values['coin'], event_at))
    return True


def record_task_claim(db, uid, task_id, now):
    if not ready(db):
        return False
    rows = db.query(
        'SELECT t.uid,t.task_id,t.claimed_ts,c.reward_json,m.guild_id,m.joined_at '
        'FROM task t JOIN task_cfg c ON c.task_id=t.task_id '
        'JOIN social_members m ON m.uid=t.uid AND m.is_bot=0 '
        'WHERE t.uid=? AND t.task_id=? AND c.task_type=11 AND COALESCE(c.activity_id,0)=0',
        (uid, task_id))
    if not rows:
        return False
    row = rows[0]
    ts = int(row['claimed_ts'] or 0)
    return _book(db, row, 'task', f'{task_id}:{ts}', json.loads(row['reward_json'] or '[]'), ts, now)


def record_rift_claim(db, uid, guild_id, kind, rewards, now, *, day_id=0, week_id=0,
                      difficulty=0, award_id=0):
    import social_service as social
    member = social.require_member(db, uid, guild_id)
    source = 'prepose' if kind == 3 else 'boss'
    key = str(day_id) if kind == 3 else f'{week_id}:{difficulty}:{kind}:{award_id}'
    return _book(db, member, source, key, rewards, now, now)


def legacy_events(db, catalog, now):
    """Only claims attributable to the player's current guild membership."""
    events = []
    for row in db.query(
            'SELECT t.uid,t.task_id,t.claimed_ts,c.reward_json,m.guild_id,m.joined_at '
            'FROM task t JOIN task_cfg c ON c.task_id=t.task_id '
            'JOIN social_members m ON m.uid=t.uid AND m.is_bot=0 '
            'WHERE c.task_type=11 AND COALESCE(c.activity_id,0)=0 '
            'AND t.claimed_ts>0 AND t.claimed_ts>=m.joined_at AND t.claimed_ts<=?', (now,)):
        ts = int(row['claimed_ts'])
        events.append((ts, row, 'task', f'{row["task_id"]}:{ts}', json.loads(row['reward_json'] or '[]')))
    # Old daily records lack a claim timestamp/guild. A successful run in one
    # unambiguous guild proves the earned day. Never attribute an old-guild
    # reward to a membership begun after that run, or to a day with mixed guilds.
    for row in db.query(
            'SELECT d.uid,d.day_id,m.guild_id,m.joined_at,MIN(r.finished_at) AS earned_at '
            'FROM social_prepose_daily d JOIN social_prepose_runs r ON r.uid=d.uid AND r.day_id=d.day_id '
            'JOIN social_members m ON m.uid=d.uid AND m.is_bot=0 '
            'WHERE d.claimed=1 AND r.state=1 AND r.result=1 '
            'GROUP BY d.uid,d.day_id,m.guild_id,m.joined_at '
            'HAVING COUNT(DISTINCT r.guild_id)=1 AND MIN(r.guild_id)=m.guild_id '
            'AND MIN(r.finished_at)>=m.joined_at AND MAX(r.finished_at)<=?', (now,)):
        events.append((int(row['earned_at']), row, 'prepose', str(row['day_id']),
                       catalog['prepose']['1']['personal_reward']))
    for row in db.query(
            'SELECT c.*,m.joined_at FROM social_reward_claims c JOIN social_members m '
            'ON m.uid=c.uid AND m.guild_id=c.guild_id AND m.is_bot=0 '
            'WHERE c.claimed_at>=m.joined_at AND c.claimed_at<=?', (now,)):
        cfg = catalog['bosses'].get(str(row['difficulty'])) or {}
        rewards = next((r[1] for r in cfg.get('personal_reward' if row['kind']==1 else 'reward', [])
                        if r[0] == row['award_id']), None)
        if rewards:
            key = f'{row["week_id"]}:{row["difficulty"]}:{row["kind"]}:{row["award_id"]}'
            events.append((int(row['claimed_at']), row, 'boss', key, rewards))
    return sorted(events, key=lambda e:(e[0],e[1]['uid'],e[2],e[3]))


def initialize(db, now_ts=None):
    if ready(db):
        return
    now = int(time.time()) if now_ts is None else int(now_ts)
    with savepoint_transaction(db, immediate=True):
        if ready(db):
            return
        db.execute('CREATE TABLE IF NOT EXISTS social_player_contributions('
                   'uid INTEGER NOT NULL,source_kind TEXT NOT NULL,source_key TEXT NOT NULL,'
                   'guild_id INTEGER NOT NULL,day_id INTEGER NOT NULL,vitality INTEGER NOT NULL,'
                   'guild_exp INTEGER NOT NULL,coin INTEGER NOT NULL,event_at INTEGER NOT NULL,'
                   'PRIMARY KEY(uid,source_kind,source_key))')
        import social_service as social
        for ts, member, kind, key, rewards in legacy_events(db, social.CATALOG, now):
            _book(db, member, kind, key, rewards, ts, now)
        db.execute('INSERT INTO server_global_config(key,value) VALUES(?,?)', (KEY, '1'))
