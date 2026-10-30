"""World/guild chat and persistent NPC contributions. No simulated game logins."""
import hashlib
import json
import logging
from pathlib import Path
import random
import re
import time

from lazy_timer import get_daily_5am_ts, get_weekly_mon_5am_ts

log = logging.getLogger(__name__)
KEY = 'gm.social_npcs.v1'
DEFAULT_GUILD = 700000001
DAY = 86400
SLOT = 900
JOIN_FREE = 1
JOIN_REVIEW = 2
JOIN_CLOSED = 3
CATALOG = json.loads(Path(__file__).with_name('social_catalog.json').read_text())
DEFAULTS = dict(enabled=True, basic_enabled=True, reply_enabled=True, ai_enabled=True,
                guild_progress_enabled=True, chatter_minutes=15, reply_minutes=10,
                api_daily_limit=20, reply_tokens=128, boss_hours=48,
                daily_vitality=600, daily_donation=120)
LIMITS = dict(chatter_minutes=(5, 180), reply_minutes=(5, 180), api_daily_limit=(0, 100),
              reply_tokens=(32, 256), boss_hours=(6, 168), daily_vitality=(0, 5000),
              daily_donation=(0, 1000))
SCHEMA = """
CREATE TABLE IF NOT EXISTS social_bots(
 uid INTEGER PRIMARY KEY,name TEXT NOT NULL,icon INTEGER NOT NULL,frame INTEGER NOT NULL,
 level INTEGER NOT NULL,hero_id INTEGER NOT NULL,prompt TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS social_guilds(
 id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL UNIQUE,icon INTEGER NOT NULL DEFAULT 1,
 notice TEXT NOT NULL,created_at INTEGER NOT NULL,exp INTEGER NOT NULL DEFAULT 0,
 examine_switch INTEGER NOT NULL DEFAULT 1,join_min_level INTEGER NOT NULL DEFAULT 1,
 difficulty INTEGER NOT NULL DEFAULT 1,boss_started_at INTEGER NOT NULL,last_tick_slot INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS social_members(
 guild_id INTEGER NOT NULL,uid INTEGER NOT NULL,is_bot INTEGER NOT NULL,post INTEGER NOT NULL DEFAULT 3,
 joined_at INTEGER NOT NULL,assist_heroes TEXT NOT NULL DEFAULT '[]',PRIMARY KEY(guild_id,uid));
CREATE UNIQUE INDEX IF NOT EXISTS social_one_real_guild ON social_members(uid) WHERE is_bot=0;
CREATE TABLE IF NOT EXISTS social_requests(
 guild_id INTEGER NOT NULL,uid INTEGER NOT NULL,created_at INTEGER NOT NULL,PRIMARY KEY(guild_id,uid));
CREATE TABLE IF NOT EXISTS social_contributions(
 guild_id INTEGER NOT NULL,uid INTEGER NOT NULL,day_id INTEGER NOT NULL,
 vitality INTEGER NOT NULL DEFAULT 0,donation INTEGER NOT NULL DEFAULT 0,sign_in INTEGER NOT NULL DEFAULT 0,
 updated_at INTEGER NOT NULL,PRIMARY KEY(guild_id,uid,day_id));
CREATE TABLE IF NOT EXISTS social_boss_scores(
 guild_id INTEGER NOT NULL,uid INTEGER NOT NULL,week_id INTEGER NOT NULL,difficulty INTEGER NOT NULL,
 damage INTEGER NOT NULL DEFAULT 0,updated_at INTEGER NOT NULL,PRIMARY KEY(guild_id,uid,week_id,difficulty));
CREATE TABLE IF NOT EXISTS social_rooms(uid INTEGER PRIMARY KEY,room_id INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS social_messages(
 id INTEGER PRIMARY KEY AUTOINCREMENT,channel INTEGER NOT NULL,scope_id INTEGER NOT NULL,
 sender_id INTEGER NOT NULL,msg_type INTEGER NOT NULL,content TEXT NOT NULL,created_at INTEGER NOT NULL,is_bot INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS social_chat_channel ON social_messages(channel,scope_id,id);
CREATE INDEX IF NOT EXISTS social_chat_sender ON social_messages(sender_id,id);
CREATE TABLE IF NOT EXISTS social_runtime_state(
 id INTEGER PRIMARY KEY CHECK(id=1),next_chatter_at INTEGER NOT NULL,last_reply_at INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS social_reply_jobs(
 id INTEGER PRIMARY KEY CHECK(id=1),message_id INTEGER NOT NULL,bot_id INTEGER NOT NULL,
 ready_at INTEGER NOT NULL,expires_at INTEGER NOT NULL,state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS social_api_budget(
 day_id INTEGER PRIMARY KEY,reserved_requests INTEGER NOT NULL DEFAULT 0,completed INTEGER NOT NULL DEFAULT 0,failed INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS social_reward_claims(
 uid INTEGER NOT NULL,week_id INTEGER NOT NULL,difficulty INTEGER NOT NULL,kind INTEGER NOT NULL,
 award_id INTEGER NOT NULL,guild_id INTEGER NOT NULL,claimed_at INTEGER NOT NULL,
 PRIMARY KEY(uid,week_id,difficulty,kind,award_id));
"""
WORLD_LINES = (
    '值班中。今天也一起慢慢推进主线吧。',
    '支援小队已经就位，需要公会的管理员可以看看推荐列表。',
    '刚整理完补给，大家记得检查邮件和每日任务。',
    '休息一下再出发，体力和队伍搭配也很重要。',
    '今天的巡逻结束了，公会支援还在继续。',
)
GUILD_LINES = (
    '本轮的签到和补给支援正在进行，贡献会陆续记到账上。',
    '支援小队继续推进首领进度，大家按自己的节奏来。',
    '补给整理完毕，继续值班。',
)
FALLBACK_LINES = (
    '收到，支援小队在。先按自己的节奏推进就好。',
    '我在值班。可以先检查一下队伍、刻印和当前活动。',
    '收到！先准备好补给，我们一起慢慢来。',
)


def integer(value, label, low=0, high=2147483647):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f'{label}须为 {low}～{high} 的整数')
    return value


def clean_text(value, limit=200):
    if not isinstance(value, str):
        raise ValueError('消息格式无效')
    value = re.sub(r'[\x00-\x1f\x7f]', ' ', value).strip()
    # Unity rich-text tags must never be interpreted as a player's UI markup.
    if not value or len(value) > limit or '<' in value or '>' in value:
        raise ValueError(f'请输入 1～{limit} 字普通文本')
    return value


def initialize(db, now_ts=None):
    found = db.query('SELECT value FROM server_global_config WHERE key=?', (KEY,))
    if found:
        import guild_cycle
        guild_cycle.initialize(db, now_ts)
        import guild_prepose
        guild_prepose.initialize(db)
        import guild_contributions
        guild_contributions.initialize(db, now_ts)
        return json.loads(found[0]['value'])
    now = int(time.time()) if now_ts is None else int(now_ts)
    with db.transaction(immediate=True):
        found = db.query('SELECT value FROM server_global_config WHERE key=?', (KEY,))
        if found:
            import guild_cycle
            guild_cycle.initialize(db, now)
            import guild_prepose
            guild_prepose.initialize(db)
            import guild_contributions
            guild_contributions.initialize(db, now)
            return json.loads(found[0]['value'])
        for statement in SCHEMA.split(';'):
            if statement.strip():
                db.execute(statement)
        # Reuse the established character profiles without changing private-chat
        # activation, credentials, personal history, or real player accounts.
        rows = db.query('SELECT * FROM ai_characters ORDER BY char_id LIMIT 6')
        for row in rows:
            uid = int(row['char_id'])
            if db.query('SELECT 1 FROM users WHERE uid=?', (uid,)):
                continue
            hero = str(row.get('hero_ids') or '1084').split(',')[0]
            db.execute('INSERT OR IGNORE INTO social_bots VALUES(?,?,?,?,?,?,?)',
                       (uid, re.sub(r'[<>\x00-\x1f\x7f]', '', str(row['char_name']))[:10] + '·人机', int(row.get('avatar_icon') or 1084),
                        int(row.get('icon_frame') or 2001), int(row.get('level') or 80),
                        int(hero) if hero.isdigit() else 1084, str(row.get('system_prompt') or '')[:800]))
        if not db.query('SELECT 1 FROM social_bots LIMIT 1'):
            for i, (name, hero) in enumerate((('值班员', 1084), ('补给员', 1066), ('巡逻员', 1039))):
                uid = 90009001 + i
                if db.query('SELECT 1 FROM users WHERE uid=?', (uid,)):
                    raise ValueError('人机编号与现有玩家冲突')
                db.execute('INSERT INTO social_bots VALUES(?,?,?,?,?,?,?)',
                           (uid, name + '·人机', hero, 2001, 80, hero, '你是深空之眼的值班支援角色。'))
        db.execute('INSERT OR IGNORE INTO social_guilds(id,name,notice,created_at,boss_started_at) VALUES(?,?,?,?,?)',
                   (DEFAULT_GUILD, '深空支援站', '人机支援公会，欢迎加入。贡献按时间推进。', now, now))
        bots = db.query('SELECT uid FROM social_bots ORDER BY uid')
        for i, bot in enumerate(bots):
            db.execute('INSERT OR IGNORE INTO social_members(guild_id,uid,is_bot,post,joined_at) VALUES(?,?,1,?,?)',
                       (DEFAULT_GUILD, bot['uid'], 1 if i == 0 else 3, now))
        db.execute('INSERT OR IGNORE INTO social_runtime_state(id,next_chatter_at) VALUES(1,?)',
                   (now + DEFAULTS['chatter_minutes'] * 60,))
        db.execute('INSERT INTO server_global_config(key,value) VALUES(?,?)', (KEY, json.dumps(DEFAULTS)))
        _append(db, 1, 1, bots[0]['uid'], '值班支援小队已就位，欢迎来到公屏。', now, is_bot=True)
        import guild_cycle
        guild_cycle.initialize(db, now)
        import guild_prepose
        guild_prepose.initialize(db)
        import guild_contributions
        guild_contributions.initialize(db, now)
    return dict(DEFAULTS)


def settings(db):
    return {**DEFAULTS, **initialize(db)}


def save_settings(db, patch):
    if not isinstance(patch, dict) or set(patch) - set(DEFAULTS):
        raise ValueError('包含未知设置项')
    current = settings(db)
    for key, value in patch.items():
        if isinstance(DEFAULTS[key], bool):
            if not isinstance(value, bool):
                raise ValueError(key + '须为开关')
        else:
            integer(value, key, *LIMITS[key])
    with db.transaction(immediate=True):
        current = settings(db)
        current.update(patch)
        db.execute('UPDATE server_global_config SET value=? WHERE key=?', (json.dumps(current), KEY))
        if not current['enabled'] or not current['reply_enabled']:
            db.execute("DELETE FROM social_reply_jobs WHERE state='pending'")
    return current


def profile(db, uid):
    rows = db.query('SELECT * FROM social_bots WHERE uid=?', (uid,))
    if rows:
        row = rows[0]
        return dict(nick=row['name'], icon=row['icon'], icon_frame=row['frame'])
    rows = db.query('SELECT nick,portrait,icon_frame FROM users WHERE uid=?', (uid,))
    if not rows:
        raise ValueError('玩家不存在')
    row = rows[0]
    return dict(nick=row['nick'] or str(uid), icon=row['portrait'] or 1084, icon_frame=row['icon_frame'] or 2001)


def membership(db, uid):
    rows = db.query('SELECT * FROM social_members WHERE uid=? AND is_bot=0', (uid,))
    return rows[0] if rows else None


def require_member(db, uid, guild_id=None, manager=False):
    member = membership(db, uid)
    if not member or guild_id is not None and member['guild_id'] != guild_id:
        raise ValueError('请先加入该公会')
    if manager and member['post'] not in (1, 2):
        raise ValueError('需要公会管理权限')
    return member


def require_assist(db, uid, owner, hero_id):
    member = require_member(db, uid)
    other = require_member(db, owner, member['guild_id'])
    if owner == uid or hero_id not in json.loads(other['assist_heroes']):
        raise ValueError('该角色未设为本家族助战')
    rows = db.query('SELECT * FROM hero WHERE uid=? AND id=? AND unlock=1', (owner, hero_id))
    if not rows:
        raise ValueError('助战角色已不可用')
    return rows[0]


def oath_display(db, uid, hero_id):
    rows = db.query('SELECT oath,nick FROM hero_oath WHERE uid=? AND hero_id=?', (uid, hero_id))
    row = rows[0] if rows else {}
    return dict(hero_id=hero_id, oath=bool(row.get('oath')), nick=row.get('nick') or '')


def assist_info(db, uid, guild_id):
    require_member(db, uid, guild_id)
    result = []
    for member in db.query('SELECT * FROM social_members WHERE guild_id=? AND is_bot=0 ORDER BY post,uid', (guild_id,)):
        for hid in json.loads(member['assist_heroes']):
            rows = db.query('SELECT * FROM hero WHERE uid=? AND id=? AND unlock=1', (member['uid'], hid))
            if not rows:
                continue
            hero = rows[0]
            result.append(dict(member_id=member['uid'], member_post=member['post'], assist_hero_id=hid,
                               assist_hero_skin=hero.get('battle_using_skin') or hero.get('using_skin') or hid,
                               fight_capacity=0, hero_rare=hero.get('star') or 100,
                               hero_level=hero.get('level') or 1, nick=profile(db, member['uid'])['nick'],
                               hero_oath_display=oath_display(db, member['uid'], hid)))
    return result


def room(db, uid):
    rows = db.query('SELECT room_id FROM social_rooms WHERE uid=?', (uid,))
    return int(rows[0]['room_id']) if rows else 1


def scope(db, uid, channel):
    if channel == 1:
        return room(db, uid)
    if channel == 3:
        return require_member(db, uid)['guild_id']
    raise ValueError('仅支持世界和公会频道')


def _append(db, channel, scope_id, sender_id, content, now, msg_type=1, is_bot=False):
    row = db.query('SELECT COALESCE(MAX(id),0) AS id FROM social_messages')[0]
    msg_id = max(now * 1000, int(row['id']) + 1)
    db.execute('INSERT INTO social_messages VALUES(?,?,?,?,?,?,?,?)',
               (msg_id, channel, scope_id, sender_id, msg_type, content, now, int(is_bot)))
    return db.query('SELECT * FROM social_messages WHERE id=?', (msg_id,))[0]


def message_wire(db, row):
    return {'id': row['sender_id'], 'user_profile_base': profile(db, row['sender_id']),
            'type': row['msg_type'], 'content': row['content'], 'timestamp': row['created_at'],
            'msg_id': row['id'], 'ip_location': '人机支援' if row['is_bot'] else '同服',
            'jump_link': '', 'chat_bubble': 9001}


def history(db, uid, channel, since=0):
    target = scope(db, uid, channel)
    rows = db.query('SELECT * FROM social_messages WHERE channel=? AND scope_id=? AND created_at>=? ORDER BY id DESC LIMIT 50',
                    (channel, target, since))
    return [{'msg': message_wire(db, row), 'room_id': target} for row in reversed(rows)]


def send_message(db, uid, channel, content, msg_type=1, now_ts=None):
    now = int(time.time()) if now_ts is None else int(now_ts)
    config = settings(db)
    integer(channel, '频道', 1, 3)
    integer(msg_type, '消息类型', 1, 2)
    content = clean_text(content)
    target = scope(db, uid, channel)
    if msg_type == 2 and (not content.isdigit() or not db.query(
            'SELECT 1 FROM chat_sticker WHERE emoji_id=? AND uid IN (0,?)', (int(content), uid))):
        raise ValueError('未解锁或无效的表情')
    previous = db.query('SELECT created_at FROM social_messages WHERE sender_id=? AND is_bot=0 ORDER BY id DESC LIMIT 1', (uid,))
    if previous and now - previous[0]['created_at'] < 5:
        raise ValueError('发言过于频繁')
    row = _append(db, channel, target, uid, content, now, msg_type)
    state = db.query('SELECT last_reply_at FROM social_runtime_state WHERE id=1')[0]
    if config['enabled'] and config['reply_enabled'] and msg_type == 1 and now - state['last_reply_at'] >= config['reply_minutes'] * 60:
        job = db.query('SELECT * FROM social_reply_jobs WHERE id=1')
        if not job or job[0]['expires_at'] < now or job[0]['state'] == 'done':
            bots = db.query('SELECT uid FROM social_bots ORDER BY uid')
            if channel == 3:
                bots = db.query('SELECT uid FROM social_members WHERE guild_id=? AND is_bot=1', (target,))
            if bots:
                bot = random.choice(bots)
                db.execute("INSERT OR REPLACE INTO social_reply_jobs VALUES(1,?,?,?,?, 'pending')",
                           (row['id'], bot['uid'], now + 3, now + 120))
                db.execute('UPDATE social_runtime_state SET last_reply_at=? WHERE id=1', (now,))
    return row


def join_policy(guild):
    # The first social version used 0 for review. Preserve that saved meaning
    # while returning a valid native toggle (1=free, 2=review, 3=closed).
    value = guild['examine_switch']
    return JOIN_REVIEW if value == 0 else integer(value, '入会方式', JOIN_FREE, JOIN_CLOSED)


def member_wire(db, member, now):
    week = get_weekly_mon_5am_ts(now)
    contribution = db.query('SELECT COALESCE(SUM(vitality),0) AS total,COALESCE(SUM(CASE WHEN day_id>=? THEN vitality ELSE 0 END),0) AS weekly '
                            'FROM social_contributions WHERE guild_id=? AND uid=?', (week, member['guild_id'], member['uid']))[0]
    import guild_contributions
    earned = guild_contributions.member_totals(db, member['guild_id'], member['uid'], week)
    contribution['total'] += earned['total']
    contribution['weekly'] += earned['weekly']
    if member['is_bot']:
        level = db.query('SELECT level FROM social_bots WHERE uid=?', (member['uid'],))[0]['level']
        online = 0
    else:
        row = db.get('users', member['uid'])
        level = row.get('level') or 1
        from server_net import get_online_uids
        presence = db.query('SELECT last_heartbeat_ts,last_logout_ts FROM user_timer_watermark WHERE uid=?',
                            (member['uid'],))
        recorded = presence[0] if presence else {}
        seen = max(int(member['joined_at']), int(recorded.get('last_heartbeat_ts') or 0),
                   int(recorded.get('last_logout_ts') or 0))
        online = 0 if member['uid'] in get_online_uids() else min(now, seen)
    return dict(id=member['uid'], user_profile_base=profile(db, member['uid']), duty=member['post'],
                online_info=online, vitality=min(contribution['weekly'], 4294967295),
                total_vitality=min(contribution['total'], 4294967295), level=level)


def guild_level(exp):
    level, threshold = 1, 0
    while level < 10:
        threshold += CATALOG['levels'][str(level)]['club_level_exp']
        if exp < threshold:
            break
        level += 1
    return level


def guild_wire(db, guild_id, viewer=0, now_ts=None):
    now = int(time.time()) if now_ts is None else int(now_ts)
    guilds = db.query('SELECT * FROM social_guilds WHERE id=?', (guild_id,))
    if not guilds:
        raise ValueError('公会不存在')
    guild = guilds[0]
    members = db.query('SELECT * FROM social_members WHERE guild_id=? ORDER BY post,uid', (guild_id,))
    leader = next((m for m in members if m['post'] == 1), members[0] if members else None)
    if leader is None:
        raise ValueError('公会缺少成员')
    week = get_weekly_mon_5am_ts(now)
    import guild_contributions
    earned = guild_contributions.totals(db, guild_id, week)
    exp = guild['exp'] + earned['total_exp']
    level = guild_level(exp)
    stats = db.query('SELECT COALESCE(SUM(CASE WHEN day_id>=? THEN vitality ELSE 0 END),0) AS xp,'
                    'COALESCE(SUM(CASE WHEN day_id>=? THEN donation ELSE 0 END),0) AS coin,'
                    'COALESCE(SUM(CASE WHEN day_id<? AND day_id>=? THEN vitality ELSE 0 END),0) AS previous '
                    'FROM social_contributions WHERE guild_id=?', (week, week, week, week - 7 * DAY, guild_id))[0]
    stats['xp'] += earned['weekly_exp']
    stats['coin'] += earned['weekly_coin']
    stats['previous'] += earned['previous']
    post = next((m['post'] for m in members if m['uid'] == viewer), 3)
    cfg = CATALOG['levels'][str(level)]
    return dict(id=guild_id, name=guild['name'], icon=guild['icon'], level=level, notice=guild['notice'],
                leader=member_wire(db, leader, now), member_count=len(members), member_count_limit=cfg['user_num_max'],
                club_setting=dict(examine_recruit_join_request_switch=join_policy(guild), join_request_limit=guild['join_min_level']),
                club_exp=exp, impeach_timestamp=0, last_week_total_vitality=stats['previous'],
                weekly_gain_exp=min(stats['xp'], cfg['max_exp_per_week']),
                weekly_gain_coin=min(stats['coin'], cfg['max_currency_per_week']), post=post, is_candidate=0)


def add_bots(db, guild_id, now):
    for bot in db.query('SELECT uid FROM social_bots ORDER BY uid'):
        db.execute('INSERT INTO social_members(guild_id,uid,is_bot,post,joined_at) VALUES(?,?,1,3,?)',
                   (guild_id, bot['uid'], now))


def create_guild(db, uid, name, icon, notice, now):
    if membership(db, uid):
        raise ValueError('已经加入公会')
    if db.query('SELECT COUNT(*) AS n FROM social_guilds')[0]['n'] >= 50:
        raise ValueError('公会数量已达上限')
    name = clean_text(name, 12)
    notice = clean_text(notice or '欢迎加入，一起推进。', 120)
    integer(icon, '公会图标', 1, 5)
    if db.query('SELECT 1 FROM social_guilds WHERE name=?', (name,)):
        raise ValueError('公会名称已存在')
    cursor = db.execute('INSERT INTO social_guilds(name,icon,notice,created_at,boss_started_at) VALUES(?,?,?,?,?)',
                        (name, icon, notice, now, now))
    guild_id = cursor.lastrowid
    db.execute('INSERT INTO social_members(guild_id,uid,is_bot,post,joined_at) VALUES(?,?,0,1,?)', (guild_id, uid, now))
    import guild_contributions
    guild_contributions.ensure_tasks(db, uid)
    db.execute('UPDATE users SET club_id=?,club_post=1 WHERE uid=?', (guild_id, uid))
    db.execute('DELETE FROM social_requests WHERE uid=?', (uid,))
    add_bots(db, guild_id, now)
    return guild_id


def join_guild(db, uid, guild_id, now, approved=False):
    if membership(db, uid):
        raise ValueError('已经加入公会')
    guild = db.query('SELECT * FROM social_guilds WHERE id=?', (guild_id,))
    if not guild:
        raise ValueError('公会不存在')
    guild = guild[0]
    policy = join_policy(guild)
    if policy == JOIN_CLOSED:
        raise ValueError('该家族已禁止加入')
    info = guild_wire(db, guild_id, now_ts=now)
    if info['member_count'] >= info['member_count_limit']:
        raise ValueError('公会已满')
    if int(db.get('users', uid).get('level') or 1) < guild['join_min_level']:
        raise ValueError('等级不足')
    if policy == JOIN_REVIEW and not approved:
        db.execute('INSERT OR IGNORE INTO social_requests VALUES(?,?,?)', (guild_id, uid, now))
        return False
    first_human = not db.query('SELECT 1 FROM social_members WHERE guild_id=? AND is_bot=0', (guild_id,))
    post = 1 if first_human else 3
    if first_human:
        db.execute('UPDATE social_members SET post=3 WHERE guild_id=?', (guild_id,))
    db.execute('INSERT INTO social_members(guild_id,uid,is_bot,post,joined_at) VALUES(?,?,0,?,?)', (guild_id, uid, post, now))
    import guild_contributions
    guild_contributions.ensure_tasks(db, uid)
    db.execute('DELETE FROM social_requests WHERE uid=?', (uid,))
    db.execute('UPDATE users SET club_id=?,club_post=? WHERE uid=?', (guild_id, post, uid))
    return True


def leave_guild(db, uid, guild_id):
    import guild_prepose
    member = require_member(db, uid, guild_id)
    guild_prepose.cancel(db, uid, guild_id)
    db.execute('DELETE FROM social_members WHERE guild_id=? AND uid=? AND is_bot=0', (guild_id, uid))
    db.execute('UPDATE users SET club_id=0,club_post=0 WHERE uid=?', (uid,))
    successor = []
    if member['post'] == 1:
        successor = db.query('SELECT uid,is_bot FROM social_members WHERE guild_id=? ORDER BY is_bot,post,uid LIMIT 1', (guild_id,))
        if successor:
            db.execute('UPDATE social_members SET post=1 WHERE guild_id=? AND uid=?', (guild_id, successor[0]['uid']))
            if not successor[0]['is_bot']:
                db.execute('UPDATE users SET club_post=1 WHERE uid=?', (successor[0]['uid'],))
    if not db.query('SELECT 1 FROM social_members WHERE guild_id=?', (guild_id,)):
        db.execute('DELETE FROM social_requests WHERE guild_id=?', (guild_id,))
        db.execute('DELETE FROM social_guilds WHERE id=?', (guild_id,))
    return successor[0] if successor else None


def _allocate(amount, members, seed):
    if amount <= 0 or not members:
        return []
    rng = random.Random(seed)
    weights = [rng.randint(1, 10) for _ in members]
    shares = [amount * weight // sum(weights) for weight in weights]
    remainder = amount - sum(shares)
    order = list(range(len(members))); rng.shuffle(order)
    for index in order[:remainder]:
        shares[index] += 1
    return [(member['uid'], share) for member, share in zip(members, shares) if share]


def advance_guilds(db, now, config):
    import guild_cycle
    if not config['enabled'] or not config['guild_progress_enabled']:
        return []
    changed = []
    week, today, slot = get_weekly_mon_5am_ts(now), get_daily_5am_ts(now), now // SLOT
    for guild in db.query('SELECT * FROM social_guilds ORDER BY id'):
        if slot <= guild['last_tick_slot']:
            continue
        gid = guild['id']
        bots = db.query('SELECT * FROM social_members WHERE guild_id=? AND is_bot=1 ORDER BY uid', (gid,))
        if not bots:
            continue
        xp_gain = 0
        prior_week_xp = db.query('SELECT COALESCE(SUM(vitality),0) AS n FROM social_contributions WHERE guild_id=? AND day_id>=?',
                                (gid, week))[0]['n']
        import guild_contributions
        earned = guild_contributions.totals(db, gid, week)
        prior_week_xp += earned['weekly_exp']
        xp_cap = CATALOG['levels'][str(guild_level(guild['exp'] + earned['total_exp']))]['max_exp_per_week']
        # Bound catch-up to this week. Derive a target from elapsed time and
        # subtract committed contributions, rather than blindly adding per tick.
        first_day = max(week, get_daily_5am_ts(guild['created_at']))
        for day in range(first_day, today + 1, DAY):
            eligible = [b for b in bots if b['joined_at'] < min(day + DAY, now + 1)]
            if not eligible:
                continue
            elapsed = max(0, min(now, day + DAY) - max(day, guild['created_at']))
            ratio = min(1, elapsed / DAY)
            for kind, target in [('vitality', int(config['daily_vitality'] * ratio)),
                                 ('donation', int(config['daily_donation'] * ratio))]:
                total = db.query(f'SELECT COALESCE(SUM(c.{kind}),0) AS n FROM social_contributions c '
                                 'JOIN social_bots b ON b.uid=c.uid WHERE guild_id=? AND day_id=?', (gid, day))[0]['n']
                delta = max(0, target - total)
                for bot_uid, amount in _allocate(delta, eligible, f'{gid}:{day}:{slot}:{kind}'):
                    db.execute('INSERT OR IGNORE INTO social_contributions(guild_id,uid,day_id,updated_at) VALUES(?,?,?,?)', (gid, bot_uid, day, now))
                    db.execute(f'UPDATE social_contributions SET {kind}={kind}+?,updated_at=? WHERE guild_id=? AND uid=? AND day_id=?',
                               (amount, now, gid, bot_uid, day))
                if kind == 'vitality':
                    xp_gain += delta
            for bot in eligible:
                signin_at = max(day, guild['created_at'], bot['joined_at']) + int(
                    hashlib.sha256(f'{gid}:{bot["uid"]}:{day}'.encode()).hexdigest()[:8], 16) % (6 * 3600)
                if min(now, day + DAY) >= signin_at:
                    db.execute('INSERT OR IGNORE INTO social_contributions(guild_id,uid,day_id,updated_at) VALUES(?,?,?,?)', (gid, bot['uid'], day, now))
                    db.execute('UPDATE social_contributions SET sign_in=1 WHERE guild_id=? AND uid=? AND day_id=?', (gid, bot['uid'], day))
        difficulty = guild['difficulty']
        hp = CATALOG['bosses'][str(difficulty)]['boss_hp']
        guild_cycle.advance_prepare(db, guild, bots, now, slot)
        target = guild_cycle.boss_target(guild, hp, config['boss_hours'], now)
        existing = db.query('SELECT COALESCE(SUM(s.damage),0) AS n FROM social_boss_scores s JOIN social_bots b ON b.uid=s.uid '
                            'WHERE guild_id=? AND week_id=? AND difficulty=?', (gid, week, difficulty))[0]['n']
        for bot_uid, amount in _allocate(max(0, target - existing), bots, f'{gid}:{week}:{slot}:boss'):
            db.execute('INSERT INTO social_boss_scores VALUES(?,?,?,?,?,?) ON CONFLICT(guild_id,uid,week_id,difficulty) '
                       'DO UPDATE SET damage=damage+excluded.damage,updated_at=excluded.updated_at',
                       (gid, bot_uid, week, difficulty, amount, now))
        earned_xp = max(0, min(xp_cap, prior_week_xp + xp_gain) - min(xp_cap, prior_week_xp))
        db.execute('UPDATE social_guilds SET exp=exp+?,last_tick_slot=? WHERE id=?', (earned_xp, slot, gid))
        changed.append(gid)
    return changed


def boss_info(db, uid, guild_id=None, now_ts=None):
    import guild_prepose
    now = int(time.time()) if now_ts is None else int(now_ts)
    member = membership(db, uid)
    gid = guild_id if guild_id is not None else member['guild_id'] if member else 0
    if guild_id is not None:
        require_member(db, uid, guild_id)
    guild = db.query('SELECT * FROM social_guilds WHERE id=?', (gid,))
    difficulty = guild[0]['difficulty'] if guild else 1
    config = CATALOG['bosses'][str(difficulty)]
    week = get_weekly_mon_5am_ts(now)
    scores = db.query('SELECT uid,damage FROM social_boss_scores WHERE guild_id=? AND week_id=? AND difficulty=?', (gid, week, difficulty))
    total = min(config['boss_hp'], sum(s['damage'] for s in scores))
    personal = next((s['damage'] for s in scores if s['uid'] == uid), 0)
    claims = db.query('SELECT kind,award_id FROM social_reward_claims WHERE uid=? AND week_id=? AND difficulty=?', (uid, week, difficulty))
    cleared = sorted({r['difficulty'] for r in db.query(
        'SELECT difficulty,SUM(damage) AS n FROM social_boss_scores WHERE guild_id=? '
        'GROUP BY week_id,difficulty', (gid,))
        if r['n'] >= CATALOG['bosses'][str(r['difficulty'])]['boss_hp']})
    # Existing captured IDs are only configuration sources. Recorded scores and
    # personal award state are never copied into a player's guild.
    pre = db.query('SELECT level_id,MIN(stage_id) AS stage_id FROM guild_boss_prebuff GROUP BY level_id ORDER BY level_id')
    pre_scores = {r['level_id']: r['score'] for r in db.query(
        'SELECT level_id,SUM(score) AS score FROM social_prepose_scores WHERE guild_id=? AND week_id=? GROUP BY level_id', (gid, week))}
    pre_info = [dict(level_id=r['level_id'], stage_id=r['stage_id'],
                    score=min(CATALOG['prepose'][str(r['level_id'])]['point_max'], pre_scores.get(r['level_id'], 0))) for r in pre]
    affixes = db.query('SELECT DISTINCT affix_id,level,aff_type FROM guild_boss_affix ORDER BY affix_id')
    personal_prepose = guild_prepose.daily(db, uid, now) if member else dict(clear_times=0, claimed=0)
    return dict(result=0, affx_list=[dict(affix_id=r['affix_id'],affix_level=r['level'],type=r['aff_type']) for r in affixes],
                pre_info=pre_info, day_first_clear_prepose=int(personal_prepose['clear_times'] > 0),
                day_clear_prepose_times=personal_prepose['clear_times'],
                day_first_clear_award_admitted=personal_prepose['claimed'],
                info=dict(boss_tempalte_id=config['boss_list'][0], difficulty=difficulty, total_damage=total,
                          collective_damage_award=int(100 * total / config['boss_hp']),
                          collective_damage_award_admitted=[c['award_id'] for c in claims if c['kind'] == 2],
                          personal_score_award_admitted=[c['award_id'] for c in claims if c['kind'] == 1],
                          personal_score=personal, cleared_difficulty_list=cleared))


def boss_rank(db, uid, guild_id, now):
    require_member(db, uid, guild_id)
    info = boss_info(db, uid, guild_id, now)['info']
    rows = db.query('SELECT s.uid,s.damage,s.updated_at,COALESCE(m.post,3) AS post FROM social_boss_scores s '
                    'LEFT JOIN social_members m ON m.guild_id=s.guild_id AND m.uid=s.uid '
                    'WHERE s.guild_id=? AND s.week_id=? AND s.difficulty=? ORDER BY s.damage DESC,s.uid',
                    (guild_id, get_weekly_mon_5am_ts(now), info['difficulty']))
    return [dict(id=r['uid'], base_info=profile(db, r['uid']), score=r['damage'], hero_type_info=[],
                 timestamp=r['updated_at'], member_post=r['post']) for r in rows]


def status(db, now_ts=None):
    import guild_cycle
    now = int(time.time()) if now_ts is None else int(now_ts)
    config = settings(db)
    day, week = get_daily_5am_ts(now), get_weekly_mon_5am_ts(now)
    usage = db.query('SELECT * FROM social_api_budget WHERE day_id=?', (day,))
    state = db.query('SELECT * FROM social_runtime_state WHERE id=1')[0]
    guilds = []
    for g in db.query('SELECT * FROM social_guilds ORDER BY id'):
        info = guild_wire(db, g['id'], now_ts=now)
        daily = db.query('SELECT COALESCE(SUM(vitality),0) AS vitality,COALESCE(SUM(donation),0) AS donation,'
                         'COALESCE(SUM(sign_in),0) AS signed_in FROM social_contributions WHERE guild_id=? AND day_id=?', (g['id'], day))[0]
        damage = db.query('SELECT COALESCE(SUM(damage),0) AS damage FROM social_boss_scores WHERE guild_id=? AND week_id=? AND difficulty=?',
                          (g['id'], week, g['difficulty']))[0]['damage']
        guilds.append(dict(id=g['id'], name=g['name'], level=info['level'], members=info['member_count'],
                           **daily, boss_damage=damage, boss_hp=CATALOG['bosses'][str(g['difficulty'])]['boss_hp']))
    week_start, prepare_end, next_week = guild_cycle.window(now)
    return dict(settings=config, guild_cycle=dict(phase=guild_cycle.phase(now), week_start=week_start,
                prepare_end=prepare_end, next_week=next_week),
                bots=db.query('SELECT uid,name,icon,level FROM social_bots ORDER BY uid'),
                guilds=guilds, day_start=day, week_start=week, next_chatter_at=state['next_chatter_at'],
                next_reply_at=state['last_reply_at'] + config['reply_minutes'] * 60,
                api_usage=usage[0] if usage else dict(reserved_requests=0, completed=0, failed=0))
