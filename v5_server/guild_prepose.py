"""Real-player preparation runs and daily rewards for the family rift."""
import json

from lazy_timer import get_daily_5am_ts, get_weekly_mon_5am_ts

KEY = 'gm.guild_prepose_players.v1'


def initialize(db):
    if db.query('SELECT 1 FROM server_global_config WHERE key=?', (KEY,)):
        return
    with db.transaction(immediate=True):
        if db.query('SELECT 1 FROM server_global_config WHERE key=?', (KEY,)):
            return
        db.execute('CREATE TABLE IF NOT EXISTS social_prepose_daily('
                   'uid INTEGER NOT NULL,day_id INTEGER NOT NULL,'
                   'clear_times INTEGER NOT NULL DEFAULT 0,claimed INTEGER NOT NULL DEFAULT 0,'
                   'PRIMARY KEY(uid,day_id))')
        db.execute('CREATE TABLE IF NOT EXISTS social_prepose_runs('
                   'uid INTEGER NOT NULL,battle_id INTEGER NOT NULL,guild_id INTEGER NOT NULL,'
                   'week_id INTEGER NOT NULL,day_id INTEGER NOT NULL,level_id INTEGER NOT NULL,'
                   'heroes TEXT NOT NULL,'
                   'state INTEGER NOT NULL DEFAULT 0,result INTEGER NOT NULL DEFAULT 0,'
                   'score INTEGER NOT NULL DEFAULT 0,use_seconds INTEGER NOT NULL DEFAULT 0,'
                   'finished_at INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(uid,battle_id))')
        db.execute('INSERT INTO server_global_config(key,value) VALUES(?,?)', (KEY, '1'))


def daily(db, uid, now):
    rows = db.query('SELECT clear_times,claimed FROM social_prepose_daily WHERE uid=? AND day_id=?',
                    (uid, get_daily_5am_ts(now)))
    return rows[0] if rows else {'clear_times': 0, 'claimed': 0}


def find_run(db, uid, bid):
    if not db.query('SELECT 1 FROM server_global_config WHERE key=?', (KEY,)):
        return None
    rows = db.query('SELECT * FROM social_prepose_runs WHERE uid=? AND battle_id=?', (uid, bid))
    return rows[0] if rows else None


def validate_team(db, uid, info):
    """Validate each owner before assembling any borrowed hero's battle stats."""
    import social_service as social
    social.require_member(db, uid)
    details = info.get('hero_details') or []
    if not 1 <= len(details) <= 3 or len({h['hero_id'] for h in details}) != len(details):
        raise ValueError('家族战斗队伍无效')
    for hero in details:
        hid, kind, owner = hero['hero_id'], hero['hero_type'], hero.get('owner_id', 0)
        if kind == 1:
            if owner not in (0, uid) or not db.query(
                    'SELECT 1 FROM hero WHERE uid=? AND id=? AND unlock=1', (uid, hid)):
                raise ValueError('尚未拥有出战角色')
        elif kind == 3:
            social.require_assist(db, uid, owner, hid)
        else:
            raise ValueError('家族战斗角色来源无效')


def validate_start(db, uid, info, now):
    import guild_cycle
    import social_service as social
    social.initialize(db, now)
    member = social.require_member(db, uid)
    validate_team(db, uid, info)
    if info.get('battle_times', 1) != 1:
        raise ValueError('家族战斗不支持多倍挑战')
    if info['type'] == 32:
        if guild_cycle.phase(now) != 'prepare':
            raise ValueError('裂隙征兆阶段才能进行前置挑战')
        social.integer(info['dest'], '前置关卡', 1, len(social.CATALOG['prepose']))
        if daily(db, uid, now)['clear_times'] >= social.CATALOG['preparation_rules']['daily_limit']:
            raise ValueError('今日前置挑战次数已用完')
    elif guild_cycle.phase(now) != 'challenge' or info['dest'] != 6:
        raise ValueError('当前无法进行家族首领挑战')
    return member


def start(db, uid, bid, info, now):
    with db.transaction(immediate=True):
        member = validate_start(db, uid, info, now)
        db.execute('INSERT INTO social_prepose_runs(uid,battle_id,guild_id,week_id,day_id,level_id,heroes) '
                   'VALUES(?,?,?,?,?,?,?)', (uid, bid, member['guild_id'], get_weekly_mon_5am_ts(now),
                                            get_daily_5am_ts(now), info['dest'], json.dumps(info['heroes'])))


def cancel(db, uid, guild_id):
    db.execute('UPDATE social_prepose_runs SET state=2 WHERE uid=? AND guild_id=? AND state=0',
               (uid, guild_id))


def settle(db, uid, bid, result, battle_info, now):
    import guild_cycle
    import social_service as social
    with db.transaction(immediate=True):
        rows = db.query('SELECT * FROM social_prepose_runs WHERE uid=? AND battle_id=?', (uid, bid))
        if not rows:
            raise ValueError('前置挑战记录不存在或不属于该玩家')
        run = rows[0]
        if run['state'] == 1:
            return run, True
        if run['state'] != 0:
            raise ValueError('离会前的挑战已失效')
        social.require_member(db, uid, run['guild_id'])
        if run['week_id'] != get_weekly_mon_5am_ts(now) or run['day_id'] != get_daily_5am_ts(now):
            raise ValueError('前置挑战已跨越刷新时间')
        if guild_cycle.phase(now) != 'prepare':
            raise ValueError('前置挑战阶段已结束')
        social.integer(result, '战斗结果', 1, 3)
        if result == 1 and 'battle_time' not in battle_info:
            raise ValueError('缺少前置挑战战斗时长')
        milliseconds = social.integer(battle_info.get('battle_time', 0), '战斗时长', 0, 2147483647)
        seconds, score = milliseconds // 1000, 0
        if result == 1:
            rules = social.CATALOG['preparation_rules']
            if daily(db, uid, now)['clear_times'] >= rules['daily_limit']:
                raise ValueError('今日前置挑战次数已用完')
            base, duration, bonus = rules['score']
            score = max(0, (base * duration + (duration - seconds) * bonus) // duration)
            cfg = social.CATALOG['prepose'][str(run['level_id'])]
            total = db.query('SELECT COALESCE(SUM(score),0) AS n FROM social_prepose_scores '
                             'WHERE guild_id=? AND week_id=? AND level_id=?',
                             (run['guild_id'], run['week_id'], run['level_id']))[0]['n']
            contribution = min(score, max(0, cfg['point_max'] - total))
            db.execute('INSERT INTO social_prepose_scores VALUES(?,?,?,?,?,?) '
                       'ON CONFLICT(guild_id,uid,week_id,level_id) DO UPDATE SET '
                       'score=score+excluded.score,updated_at=excluded.updated_at',
                       (run['guild_id'], uid, run['week_id'], run['level_id'], contribution, now))
            db.execute('INSERT INTO social_prepose_daily(uid,day_id,clear_times) VALUES(?,?,1) '
                       'ON CONFLICT(uid,day_id) DO UPDATE SET clear_times=clear_times+1', (uid, run['day_id']))
        db.execute('UPDATE social_prepose_runs SET state=1,result=?,score=?,use_seconds=?,finished_at=? '
                   'WHERE uid=? AND battle_id=?', (result, score, seconds, now, uid, bid))
        run.update(state=1, result=result, score=score, use_seconds=seconds, finished_at=now)
        return run, False
