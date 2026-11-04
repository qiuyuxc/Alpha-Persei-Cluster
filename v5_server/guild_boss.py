"""Durable real-player Boss runs, native scores and weekly personal bests."""
import json
from pathlib import Path

from lazy_timer import get_weekly_mon_5am_ts

KEY = 'gm.guild_boss_players.v1'
CATALOG = json.loads(Path(__file__).with_name('guild_boss_cfg.json').read_text())


def initialize(db):
    if db.query('SELECT 1 FROM server_global_config WHERE key=?', (KEY,)):
        return
    with db.transaction(immediate=True):
        if db.query('SELECT 1 FROM server_global_config WHERE key=?', (KEY,)):
            return
        db.execute('CREATE TABLE IF NOT EXISTS social_boss_runs('
                   'uid INTEGER NOT NULL,battle_id INTEGER NOT NULL,'
                   'guild_id INTEGER NOT NULL,week_id INTEGER NOT NULL,'
                   'difficulty INTEGER NOT NULL,boss_id INTEGER NOT NULL,boss_hp INTEGER NOT NULL,'
                   'heroes TEXT NOT NULL,started_at INTEGER NOT NULL,'
                   'state INTEGER NOT NULL DEFAULT 0,result INTEGER NOT NULL DEFAULT 0,'
                   'damage INTEGER NOT NULL DEFAULT 0,score INTEGER NOT NULL DEFAULT 0,'
                   'use_seconds INTEGER NOT NULL DEFAULT 0,finished_at INTEGER NOT NULL DEFAULT 0,'
                   'PRIMARY KEY(uid,battle_id))')
        db.execute('CREATE INDEX IF NOT EXISTS social_boss_runs_owner '
                   'ON social_boss_runs(battle_id)')
        db.execute('INSERT INTO server_global_config(key,value) VALUES(?,?)', (KEY, '1'))


def find_run(db, uid, bid):
    if not db.query('SELECT 1 FROM server_global_config WHERE key=?', (KEY,)):
        return None
    rows = db.query('SELECT * FROM social_boss_runs WHERE uid=? AND battle_id=?', (uid, bid))
    if rows:
        return rows[0]
    if db.query('SELECT 1 FROM social_boss_runs WHERE battle_id=?', (bid,)):
        raise ValueError('首领挑战记录不属于该玩家')
    return None


def start(db, uid, bid, info, now):
    import guild_prepose
    import social_service as social
    if info.get('type') != 33 or info.get('dest') != 6:
        raise ValueError('首领挑战类型或关卡无效')
    with db.transaction(immediate=True):
        member = guild_prepose.validate_start(db, uid, info, now)
        guild = db.query('SELECT difficulty FROM social_guilds WHERE id=?', (member['guild_id'],))[0]
        difficulty = guild['difficulty']
        boss_id = social.CATALOG['bosses'][str(difficulty)]['boss_list'][0]
        hp = CATALOG['bosses'][str(boss_id)]['boss_hp'][difficulty - 1]
        db.execute('INSERT INTO social_boss_runs('
                   'uid,battle_id,guild_id,week_id,difficulty,boss_id,boss_hp,heroes,started_at) '
                   'VALUES(?,?,?,?,?,?,?,?,?)', (uid, bid, member['guild_id'],
                    get_weekly_mon_5am_ts(now), difficulty, boss_id, hp,
                    json.dumps(info['heroes']), now))


def cancel(db, uid, guild_id):
    db.execute('UPDATE social_boss_runs SET state=2 WHERE uid=? AND guild_id=? AND state=0',
               (uid, guild_id))


def calculate_score(damage, hp, seconds, result):
    if result == 3:
        return 0
    base, duration, bonus = CATALOG['score']
    numerator = min(damage, hp) * base * duration
    if result == 1:
        numerator += (duration - seconds) * bonus * hp
    return max(0, numerator // (hp * duration))


def settle(db, uid, bid, result, battle_info, now):
    import guild_cycle
    import social_service as social
    with db.transaction(immediate=True):
        run = find_run(db, uid, bid)
        if not run:
            raise ValueError('首领挑战记录不存在或不属于该玩家')
        if run['state'] == 1:
            return run, True
        if run['state'] != 0:
            raise ValueError('离会前的首领挑战已失效')
        social.require_member(db, uid, run['guild_id'])
        if run['week_id'] != get_weekly_mon_5am_ts(now):
            raise ValueError('首领挑战已跨越本周刷新时间')
        if guild_cycle.phase(now) != 'challenge':
            raise ValueError('裂隙开放阶段已结束')
        guild = db.query('SELECT difficulty FROM social_guilds WHERE id=?', (run['guild_id'],))[0]
        if guild['difficulty'] != run['difficulty']:
            raise ValueError('首领挑战难度已变化')
        social.integer(result, '战斗结果', 1, 3)
        if result != 3 and ('battle_time' not in battle_info or 'total_damage' not in battle_info):
            raise ValueError('缺少首领挑战战斗时长或真实伤害')
        milliseconds = social.integer(battle_info.get('battle_time', 0), '战斗时长', 0, 2147483647)
        damage = social.integer(battle_info.get('total_damage', 0), '首领伤害', 0, 9223372036854775807)
        statistics = battle_info.get('hero_damage')
        if statistics is not None:
            ids = [hero['hero_id'] for hero in statistics]
            if len(ids) != len(set(ids)) or not set(ids).issubset(json.loads(run['heroes'])):
                raise ValueError('首领战报英雄与出战队伍不一致')
            if sum(social.integer(hero['damage'], '英雄伤害', 0, 9223372036854775807)
                   for hero in statistics) != damage:
                raise ValueError('首领战报伤害不一致')
        seconds = milliseconds // 1000
        score = calculate_score(damage, run['boss_hp'], seconds, result)
        if score:
            # Native rules retain only a player's best score. Improving it adds
            # only the improvement to the guild's summed damage; retries and
            # lower scores cannot reduce or repeatedly damage the guild Boss.
            db.execute('INSERT INTO social_boss_scores VALUES(?,?,?,?,?,?) '
                       'ON CONFLICT(guild_id,uid,week_id,difficulty) DO UPDATE SET '
                       'damage=excluded.damage,updated_at=excluded.updated_at '
                       'WHERE excluded.damage>social_boss_scores.damage',
                       (run['guild_id'], uid, run['week_id'], run['difficulty'], score, now))
        db.execute('UPDATE social_boss_runs SET state=1,result=?,damage=?,score=?,use_seconds=?,finished_at=? '
                   'WHERE uid=? AND battle_id=?', (result, damage, score, seconds, now, uid, bid))
        run.update(state=1, result=result, damage=damage, score=score,
                   use_seconds=seconds, finished_at=now)
        return run, False
