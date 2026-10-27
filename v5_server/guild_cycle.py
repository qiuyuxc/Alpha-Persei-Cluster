"""Guild rift calendar: Monday 05:00 preparation, Saturday 05:00 challenge."""
import time

from lazy_timer import get_weekly_mon_5am_ts

DAY = 86400
KEY = 'gm.guild_rift_cycle.v1'


def window(now_ts=None):
    now = int(time.time()) if now_ts is None else int(now_ts)
    week = get_weekly_mon_5am_ts(now)
    return week, week + 5 * DAY, week + 7 * DAY


def phase(now_ts=None):
    now = int(time.time()) if now_ts is None else int(now_ts)
    return 'prepare' if now < window(now)[1] else 'challenge'


def activities(now_ts=None):
    start, change, stop = window(now_ts)
    return [dict(activity_id=aid, start_time=begin, stop_time=end, state=1,
                 theme=0, template=template, sub_activity_id_list=[])
            for aid, begin, end, template in (
                (102, start, change, 57), (103, change, stop, 58), (104, start, stop, 59))]


def apply_windows(rows, now_ts=None):
    """Replace only the guild's three activities, including missing legacy rows."""
    return [row for row in rows if row['activity_id'] not in (102, 103, 104)] + activities(now_ts)


def initialize(db, now_ts=None):
    """Add a preparation ledger without resetting any existing guild progress."""
    if db.query('SELECT 1 FROM server_global_config WHERE key=?', (KEY,)):
        return
    now = int(time.time()) if now_ts is None else int(now_ts)
    with db.transaction(immediate=True):
        if db.query('SELECT 1 FROM server_global_config WHERE key=?', (KEY,)):
            return
        db.execute('CREATE TABLE IF NOT EXISTS social_prepose_scores('
                   'guild_id INTEGER NOT NULL,uid INTEGER NOT NULL,week_id INTEGER NOT NULL,'
                   'level_id INTEGER NOT NULL,score INTEGER NOT NULL,updated_at INTEGER NOT NULL,'
                   'PRIMARY KEY(guild_id,uid,week_id,level_id))')
        # Previous code derived preparation from the current Boss fraction.
        # Adopt that visible value once; subsequent phases advance independently.
        import social_service as social
        week = window(now)[0]
        for guild in db.query('SELECT * FROM social_guilds'):
            total = db.query('SELECT COALESCE(SUM(damage),0) AS n FROM social_boss_scores '
                             'WHERE guild_id=? AND week_id=? AND difficulty=?',
                             (guild['id'], week, guild['difficulty']))[0]['n']
            hp = social.CATALOG['bosses'][str(guild['difficulty'])]['boss_hp']
            members = db.query('SELECT uid FROM social_members WHERE guild_id=? AND is_bot=1 ORDER BY uid',
                               (guild['id'],)) or [{'uid': 0}]
            for level, cfg in social.CATALOG['prepose'].items():
                target = int(cfg['point_max'] * min(total, hp) / hp)
                for uid, amount in social._allocate(target, members, f'adopt:{guild["id"]}:{week}:{level}'):
                    db.execute('INSERT INTO social_prepose_scores VALUES(?,?,?,?,?,?)',
                               (guild['id'], uid, week, int(level), amount, now))
        db.execute('INSERT INTO server_global_config(key,value) VALUES(?,?)', (KEY, '1'))


def advance_prepare(db, guild, bots, now, slot):
    import social_service as social
    week, change, _ = window(now)
    anchor = max(week, guild['created_at'])
    if anchor >= change:
        return
    ratio = min(1, max(0, now - anchor) / (change - anchor))
    for level, cfg in social.CATALOG['prepose'].items():
        target = int(cfg['point_max'] * ratio)
        existing = db.query('SELECT COALESCE(SUM(score),0) AS n FROM social_prepose_scores '
                            'WHERE guild_id=? AND week_id=? AND level_id=?',
                            (guild['id'], week, int(level)))[0]['n']
        for uid, amount in social._allocate(max(0, target - existing), bots,
                                           f'{guild["id"]}:{week}:{slot}:prepare:{level}'):
            db.execute('INSERT INTO social_prepose_scores VALUES(?,?,?,?,?,?) '
                       'ON CONFLICT(guild_id,uid,week_id,level_id) DO UPDATE SET '
                       'score=score+excluded.score,updated_at=excluded.updated_at',
                       (guild['id'], uid, week, int(level), amount, now))


def boss_target(guild, hp, hours, now):
    _, start, end = window(now)
    anchor = max(start, guild['created_at'], guild['boss_started_at'])
    if now <= anchor:
        return 0
    # Fit NPC support inside the weekend and leave time to collect rewards.
    duration = min(hours * 3600, max(900, end - anchor - 3600))
    return int(hp * min(1, max(0, now - anchor) / duration))
