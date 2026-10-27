"""Persistent level-range mail rules; delivery and receipts commit together."""
import json
import logging
import re
import time

from account_db import savepoint_transaction
from event_bus import bus, Events
from game_cycles import integer
from mail_service import MailService, normalize_attachments, resolve_expiry

logger = logging.getLogger('mail_automation')
DEFAULT_KEY = 'gm.level_mail.defaults.v1'
SAFE_TEXT = re.compile(r'^[\u4e00-\u9fa5a-zA-Z0-9\s，。！？、：；“”‘’（）《》【】—…·,.!?:;\'"()\[\]<>\-—\n\r]*$')
FIELDS = {'name', 'enabled', 'level_min', 'level_max', 'title', 'sender', 'content', 'attachments', 'valid_days'}


def _schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS gm_level_mail_rule(
        id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,enabled INTEGER NOT NULL,
        level_min INTEGER NOT NULL,level_max INTEGER NOT NULL,title TEXT NOT NULL,sender TEXT NOT NULL,
        content TEXT NOT NULL,attachments_json TEXT NOT NULL,valid_days INTEGER NOT NULL,
        created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS gm_level_mail_delivery(
        rule_id INTEGER NOT NULL,uid INTEGER NOT NULL,status TEXT NOT NULL,mail_id INTEGER NOT NULL,
        sent_at INTEGER NOT NULL,level INTEGER NOT NULL,PRIMARY KEY(rule_id,uid))''')
    db.execute('''CREATE TABLE IF NOT EXISTS gm_level_mail_failure(
        rule_id INTEGER NOT NULL,uid INTEGER NOT NULL,attempts INTEGER NOT NULL,last_error TEXT NOT NULL,
        last_attempt_ts INTEGER NOT NULL,PRIMARY KEY(rule_id,uid))''')


def _unpack(row):
    return {**{k: v for k, v in row.items() if k != 'attachments_json'},
            'enabled': bool(row['enabled']), 'attachments': json.loads(row['attachments_json'])}


def _text(value, label, maximum):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or not SAFE_TEXT.fullmatch(value):
        raise ValueError(f'{label}须为 1 至 {maximum} 个字符，只允许中英文、数字及常用标点')
    return value.strip()


def save_rule(db, payload, now_ts=None):
    if not isinstance(payload, dict) or set(payload) - (FIELDS | {'id', 'include_existing'}):
        raise ValueError('等级邮件规则格式无效')
    now = int(time.time()) if now_ts is None else int(now_ts)
    with savepoint_transaction(db, immediate=True):
        _schema(db)
        rule_id = integer(payload['id'], '规则编号', 1) if 'id' in payload else None
        if rule_id is not None:
            rows = db.query('SELECT * FROM gm_level_mail_rule WHERE id=?', (rule_id,))
            if not rows:
                raise ValueError('等级邮件规则不存在')
            values = _unpack(rows[0])
        else:
            values = {'name': '成长礼包', 'enabled': True, 'level_min': 11, 'level_max': 100,
                      'title': '成长礼包', 'sender': '隐科组总务部', 'content': '管理员，成长物资已送达，请在有效期内领取。',
                      'attachments': [], 'valid_days': 30}
        values.update({k: v for k, v in payload.items() if k in FIELDS})
        if not isinstance(values['enabled'], bool):
            raise ValueError('规则启用状态必须是布尔值')
        values['level_min'] = integer(values['level_min'], '等级下限', 1, 100)
        values['level_max'] = integer(values['level_max'], '等级上限', values['level_min'], 100)
        values['valid_days'] = integer(values['valid_days'], '邮件有效天数', 1, 3650)
        for key, label, maximum in (('name', '规则名称', 80), ('title', '邮件标题', 120),
                                    ('sender', '发件人', 80), ('content', '邮件正文', 4000)):
            values[key] = _text(values[key], label, maximum)
        values['attachments'] = normalize_attachments(values['attachments'], db)
        if not values['attachments']:
            raise ValueError('等级礼包至少需要一项附件')
        include_existing = payload.get('include_existing', True)
        if not isinstance(include_existing, bool):
            raise ValueError('现有账号补发选项必须是布尔值')
        columns = ('name', 'enabled', 'level_min', 'level_max', 'title', 'sender', 'content', 'attachments_json', 'valid_days')
        stored = {**values, 'enabled': int(values['enabled']), 'attachments_json': json.dumps(values['attachments'], ensure_ascii=False)}
        args = tuple(stored[k] for k in columns)
        if rule_id is None:
            cursor = db.execute('INSERT INTO gm_level_mail_rule(' + ','.join(columns) + ',created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)', (*args, now, now))
            rule_id = int(cursor.lastrowid)
            if not include_existing:
                # Mark existing accounts already past the threshold. This remains
                # a receipt when a rule is edited, disabled, or enabled again.
                db.execute("INSERT INTO gm_level_mail_delivery(rule_id,uid,status,mail_id,sent_at,level) SELECT ?,uid,'skipped',0,?,level FROM users WHERE level>=?", (rule_id, now, values['level_min']))
        else:
            db.execute('UPDATE gm_level_mail_rule SET ' + ','.join(k + '=?' for k in columns) + ',updated_at=? WHERE id=?', (*args, now, rule_id))
        return _unpack(db.query('SELECT * FROM gm_level_mail_rule WHERE id=?', (rule_id,))[0])


def initialize(db, now_ts=None):
    now = int(time.time()) if now_ts is None else int(now_ts)
    with db.transaction(immediate=True):
        _schema(db)
        if not db.query('SELECT value FROM server_global_config WHERE key=?', (DEFAULT_KEY,)):
            rule = save_rule(db, {'name': '11级成长礼包', 'title': '11级成长礼包',
                'level_min': 11, 'level_max': 100, 'include_existing': True,
                'content': '管理员，恭喜达到11级！20张精确探测凭证与基础养成物资已送达，请在30天内查收。',
                'attachments': [{'id': 38, 'number': 20}, {'id': 2, 'number': 200000},
                                {'id': 40103, 'number': 30}, {'id': 40301, 'number': 50}, {'id': 41301, 'number': 30}]}, now)
            db.execute('INSERT INTO server_global_config(key,value) VALUES(?,?)', (DEFAULT_KEY, str(rule['id'])))


def _eligible_sql():
    return '''FROM users u LEFT JOIN auth_accounts a ON a.uid=u.uid
        LEFT JOIN gm_level_mail_delivery d ON d.uid=u.uid AND d.rule_id=?
        WHERE u.level BETWEEN ? AND ? AND d.uid IS NULL
        AND COALESCE(a.disabled,0)=0 AND (u.register_ts>0 OR a.uid IS NOT NULL)'''


def get_rules(db):
    initialize(db)
    result = []
    for row in db.query('SELECT * FROM gm_level_mail_rule ORDER BY id'):
        rule = _unpack(row)
        counts = db.query("SELECT status,COUNT(*) AS n FROM gm_level_mail_delivery WHERE rule_id=? GROUP BY status", (rule['id'],))
        summary = {r['status']: r['n'] for r in counts}
        pending = db.query('SELECT COUNT(*) AS n ' + _eligible_sql(), (rule['id'], rule['level_min'], rule['level_max']))[0]['n']
        failures = db.query('SELECT uid,attempts,last_error,last_attempt_ts FROM gm_level_mail_failure WHERE rule_id=? ORDER BY last_attempt_ts DESC LIMIT 20', (rule['id'],))
        result.append({**rule, 'sent_count': summary.get('sent', 0), 'skipped_count': summary.get('skipped', 0),
                       'pending_count': pending if rule['enabled'] else 0, 'failures': failures})
    return result


def deliver_for_user(db, uid, now_ts=None, ctx=None):
    now = int(time.time()) if now_ts is None else int(now_ts)
    sent, failures = [], []
    for item in db.query('SELECT id FROM gm_level_mail_rule WHERE enabled=1 ORDER BY id'):
        rule_id = int(item['id'])
        try:
            with savepoint_transaction(db, immediate=True):
                rules = db.query('SELECT * FROM gm_level_mail_rule WHERE id=? AND enabled=1', (rule_id,))
                if not rules:
                    continue
                rule = _unpack(rules[0])
                players = db.query('SELECT u.uid,u.level ' + _eligible_sql() + ' AND u.uid=?',
                                   (rule_id, rule['level_min'], rule['level_max'], uid))
                if not players:
                    continue
                receipt = db.execute("INSERT OR IGNORE INTO gm_level_mail_delivery(rule_id,uid,status,mail_id,sent_at,level) VALUES(?,?,'sent',0,?,?)", (rule_id, uid, now, players[0]['level']))
                if receipt.rowcount != 1:
                    continue
                expiry = resolve_expiry(valid_days=rule['valid_days'], now_ts=now)
                mid = MailService.get_instance(db).send_mail(uid, rule['title'], rule['content'],
                        attachments=rule['attachments'], sender=rule['sender'], expire_time=expiry, db=db, now_ts=now)
                if not mid:
                    raise ValueError('邮件未成功入库')
                db.execute('UPDATE gm_level_mail_delivery SET mail_id=? WHERE rule_id=? AND uid=?', (mid, rule_id, uid))
                db.execute('DELETE FROM gm_level_mail_failure WHERE rule_id=? AND uid=?', (rule_id, uid))
                sent.append({'uid': uid, 'rule_id': rule_id, 'mail_id': mid, 'expire_time': expiry})
        except Exception as error:
            message = (type(error).__name__ + ': ' + str(error))[:400]
            failures.append({'uid': uid, 'rule_id': rule_id, 'error': message})
            try:
                db.execute('''INSERT INTO gm_level_mail_failure(rule_id,uid,attempts,last_error,last_attempt_ts) VALUES(?,?,1,?,?)
                    ON CONFLICT(rule_id,uid) DO UPDATE SET attempts=attempts+1,last_error=excluded.last_error,last_attempt_ts=excluded.last_attempt_ts''', (rule_id, uid, message, now))
            except Exception:
                logger.exception('等级邮件失败记录无法保存 rule_id=%s uid=%s', rule_id, uid)
    if sent and ctx is not None and hasattr(ctx, 'append_frame'):
        from codec import encode
        from middleware import DownFrame
        ctx.append_frame(DownFrame(30001, encode('sc_30001', MailService.get_instance(db).get_login_summary(uid, db))))
    return {'sent': sent, 'failures': failures}


def run_pending(db, now_ts=None, limit=500):
    now = int(time.time()) if now_ts is None else int(now_ts)
    users = db.query('''SELECT DISTINCT u.uid FROM users u
        JOIN gm_level_mail_rule r ON r.enabled=1 AND u.level BETWEEN r.level_min AND r.level_max
        LEFT JOIN auth_accounts a ON a.uid=u.uid
        LEFT JOIN gm_level_mail_delivery d ON d.uid=u.uid AND d.rule_id=r.id
        WHERE d.uid IS NULL AND COALESCE(a.disabled,0)=0 AND (u.register_ts>0 OR a.uid IS NOT NULL)
        ORDER BY u.uid LIMIT ?''', (limit,))
    result = {'sent': [], 'failures': []}
    for player in users:
        delivery = deliver_for_user(db, int(player['uid']), now)
        result['sent'].extend(delivery['sent'])
        result['failures'].extend(delivery['failures'])
    return result


@bus.subscribe(Events.USER_LOGIN)
@bus.subscribe(Events.PLAYER_LEVEL_UP)
def on_player_ready(ctx, uid, now_ts=None, **kwargs):
    if not uid or not getattr(ctx, 'db', None):
        return
    # Startup creates rules; isolated domain tests may not boot the scheduler.
    if not ctx.db.query("SELECT 1 FROM sqlite_master WHERE type='table' AND name='gm_level_mail_rule'"):
        return
    deliver_for_user(ctx.db, uid, now_ts, ctx)
