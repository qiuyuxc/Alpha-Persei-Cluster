"""Persistent game periods, task deadlines and editable rotation settings."""
import copy
import hashlib
import json
import time

from lazy_timer import get_daily_5am_ts, get_weekly_mon_5am_ts

KEY = 'gm.runtime_schedule.v1'
ROTATION_KEY = 'gm.runtime_rotation.v1'
DAY = 86400
DAILY_TYPES = (6, 7, 901, 1101, 3001)
WEEKLY_TYPES = (5, 8, 902)


def integer(value, label, minimum=0, maximum=2147483647):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(label + '必须是整数')
    if isinstance(value, str) and not value.isdigit():
        raise ValueError(label + '必须是整数')
    result = int(value)
    if not minimum <= result <= maximum:
        raise ValueError(f'{label}须在 {minimum} 至 {maximum} 之间')
    return result


def _schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS battlepass_cycle(uid INTEGER PRIMARY KEY, start_ts INTEGER NOT NULL, end_ts INTEGER NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS battlepass_cycle_history(uid INTEGER NOT NULL,start_ts INTEGER NOT NULL,end_ts INTEGER NOT NULL,data_json TEXT NOT NULL,PRIMARY KEY(uid,start_ts))')
    db.execute('CREATE TABLE IF NOT EXISTS task_cycle(uid INTEGER NOT NULL,kind TEXT NOT NULL,start_ts INTEGER NOT NULL,PRIMARY KEY(uid,kind))')


def _defaults(db, now):
    current = [int(r['pool_id']) for r in db.query('SELECT pool_id FROM active_draw_pool WHERE is_active=1 ORDER BY sort_order,pool_id')]
    available = {int(r['pool_id']) for r in db.query('SELECT pool_id FROM draw_pool')}
    fixed = [p for p in current if p in (10000, 10001, 10002, 4080101, 5030601, 5030301)]
    moving = [p for p in current if p not in fixed]
    steps = [{'name': '当前卡池', 'pool_ids': moving}]
    for name, ids in (('与破晓凯旋', [4050301, 4050601]), ('天通明晦', [4080301, 4080601]),
                      ('莫道奈何', [5000301, 5000601])):
        if set(ids) <= available and ids != moving:
            steps.append({'name': name, 'pool_ids': ids})
    return {'battlepass': {'duration_days': 42, 'anchor_ts': get_daily_5am_ts(now), 'next_duration_days': None},
            'polyhedron': {'duration_days': 7, 'anchor_ts': get_weekly_mon_5am_ts(now), 'next_duration_days': None},
            'rotation': {'enabled': len(steps) > 1, 'duration_days': 7, 'start_ts': get_daily_5am_ts(now),
                         'fixed_pool_ids': fixed, 'steps': steps, 'updated_at': now}}


CYCLES = ('battlepass', 'polyhedron')


def _window(settings, name, now):
    cfg = settings[name]
    length = int(cfg['duration_days']) * DAY
    anchor = int(cfg['anchor_ts'])
    start = anchor + max(0, (now - anchor) // length) * length
    return start, start + length


def _backfill(db, settings, now):
    """线上旧存档只有 battlepass/rotation 两项，补写多维变量默认周期后落盘。"""
    if 'polyhedron' not in settings:
        settings['polyhedron'] = {'duration_days': 7, 'anchor_ts': get_weekly_mon_5am_ts(now),
                                  'next_duration_days': None}
        db.execute('UPDATE server_global_config SET value=? WHERE key=?',
                   (json.dumps(settings, ensure_ascii=False), KEY))
    return settings


def get_settings(db, now_ts=None):
    now = int(time.time()) if now_ts is None else int(now_ts)
    rows = db.query('SELECT value FROM server_global_config WHERE key=?', (KEY,))
    if rows:
        settings = json.loads(rows[0]['value'])
    else:
        settings = _defaults(db, now)
        db.execute('INSERT INTO server_global_config(key,value) VALUES(?,?) ON CONFLICT(key) DO NOTHING',
                   (KEY, json.dumps(settings, ensure_ascii=False)))
        settings = json.loads(db.query('SELECT value FROM server_global_config WHERE key=?', (KEY,))[0]['value'])
    settings = _backfill(db, settings, now)
    rolling = [name for name in CYCLES
               if settings[name].get('next_duration_days')
               and now >= int(settings[name]['anchor_ts']) + int(settings[name]['duration_days']) * DAY]
    if rolling:
        with db.transaction(immediate=True):
            current = _backfill(db, json.loads(db.query('SELECT value FROM server_global_config WHERE key=?', (KEY,))[0]['value']), now)
            for name in rolling:
                cfg = current[name]
                old_end = int(cfg['anchor_ts']) + int(cfg['duration_days']) * DAY
                if cfg.get('next_duration_days') and now >= old_end:
                    cfg.update(anchor_ts=old_end, duration_days=cfg['next_duration_days'], next_duration_days=None)
            db.execute('UPDATE server_global_config SET value=? WHERE key=?', (json.dumps(current, ensure_ascii=False), KEY))
            settings = current
    return settings


def period_window(db, now_ts=None):
    now = int(time.time()) if now_ts is None else int(now_ts)
    return _window(get_settings(db, now), 'battlepass', now)


def polyhedron_window(db, now_ts=None):
    """多维变量本轮起止：以周一 05:00 为锚点按配置天数滚动。"""
    now = int(time.time()) if now_ts is None else int(now_ts)
    return _window(get_settings(db, now), 'polyhedron', now)


def pool_window(db, pool_id, now_ts=None):
    now = int(time.time()) if now_ts is None else int(now_ts)
    rotation = get_settings(db, now)['rotation']
    if rotation['enabled'] and pool_id not in rotation['fixed_pool_ids'] and now >= rotation['start_ts']:
        status = rotation_status(db, now)
        return status['start_ts'], status['next_rotation_ts']
    return period_window(db, now)


def save_settings(db, patch, now_ts=None):
    if not isinstance(patch, dict) or set(patch) - {'battlepass', 'polyhedron', 'rotation'}:
        raise ValueError('周期配置格式无效')
    now = int(time.time()) if now_ts is None else int(now_ts)
    with db.transaction(immediate=True):
        settings = copy.deepcopy(get_settings(db, now))
        for name, label, window in (('battlepass', '对策协议', period_window), ('polyhedron', '多维变量', polyhedron_window)):
            if name not in patch:
                continue
            incoming = patch[name]
            if not isinstance(incoming, dict) or set(incoming) != {'duration_days'}:
                raise ValueError(f'{label}只需配置周期天数')
            days = integer(incoming['duration_days'], f'{label}周期', 1, 365)
            current_start, _ = window(db, now)
            settings[name]['anchor_ts'] = current_start
            settings[name]['next_duration_days'] = days if days != settings[name]['duration_days'] else None
        if 'rotation' in patch:
            incoming = patch['rotation']
            allowed = {'enabled', 'duration_days', 'start_ts', 'fixed_pool_ids', 'steps'}
            if not isinstance(incoming, dict) or set(incoming) - allowed:
                raise ValueError('卡池轮换配置格式无效')
            rotation = {k: copy.deepcopy(settings['rotation'][k]) for k in allowed}
            rotation.update(incoming)
            if not isinstance(rotation['enabled'], bool):
                raise ValueError('轮换启用状态必须是布尔值')
            rotation['duration_days'] = integer(rotation['duration_days'], '轮换间隔', 1, 365)
            rotation['start_ts'] = integer(rotation['start_ts'], '轮换起始时间', 1)
            available = {int(r['pool_id']) for r in db.query('SELECT pool_id FROM draw_pool')}
            from draw_service import DrawService
            import res_version_manager
            pool_config = DrawService.get_instance(db).load_pool_activity_cfg()
            supported = {int(p) for p in pool_config} | {10000, 10001, 10002}
            available &= supported
            if str(res_version_manager.get_current_version()) == '229':
                available = {p for p in available if not str(p).startswith('503')
                             and int(pool_config.get(str(p), {}).get('theme', 0)) < 44}
            def pools(value):
                if not isinstance(value, list) or len(value) > 100:
                    raise ValueError('卡池清单须为数组，最多 100 项')
                ids = [integer(p, '卡池编号', 1) for p in value]
                if len(ids) != len(set(ids)) or set(ids) - available:
                    raise ValueError('卡池清单含重复或不存在的卡池')
                return ids
            rotation['fixed_pool_ids'] = pools(rotation['fixed_pool_ids'])
            if not isinstance(rotation['steps'], list) or not 1 <= len(rotation['steps']) <= 30:
                raise ValueError('轮换顺序须包含 1 至 30 组')
            steps = []
            for step in rotation['steps']:
                if not isinstance(step, dict) or set(step) != {'name', 'pool_ids'}:
                    raise ValueError('每组轮换须填写名称和卡池')
                name = str(step['name']).strip()
                if not 1 <= len(name) <= 80:
                    raise ValueError('轮换名称须为 1 至 80 个字符')
                ids = pools(step['pool_ids'])
                if set(ids) & set(rotation['fixed_pool_ids']):
                    raise ValueError('常驻卡池不能重复填入轮换组')
                if not ids and not rotation['fixed_pool_ids']:
                    raise ValueError('每组至少需要一个卡池')
                steps.append({'name': name, 'pool_ids': ids})
            rotation.update(steps=steps, updated_at=now)
            settings['rotation'] = rotation
        db.execute('UPDATE server_global_config SET value=? WHERE key=?', (json.dumps(settings, ensure_ascii=False), KEY))
    return settings


def ensure_battlepass(db, uid, now_ts=None):
    now = int(time.time()) if now_ts is None else int(now_ts)
    import res_version_manager
    list_id = int(res_version_manager.get_version_config()['battlepass_list_id'])
    with db.transaction(immediate=True):
        _schema(db)
        start, end = period_window(db, now)
        marker = db.query('SELECT * FROM battlepass_cycle WHERE uid=?', (uid,))
        rows = db.query('SELECT * FROM battlepass WHERE uid=?', (uid,))
        old = rows[0] if rows else None
        if marker and start < marker[0]['start_ts']:
            start, end = int(marker[0]['start_ts']), int(marker[0]['end_ts'])
        next_refresh = min(end, get_weekly_mon_5am_ts(max(now, start)) + 7 * DAY)
        if marker and old and int(marker[0]['start_ts']) == start:
            next_refresh = min(end, max(next_refresh, int(old.get('next_refresh_timestamp') or 0)))
        if marker and start > marker[0]['start_ts'] and old:
            history = {**old, 'experience': db.query('SELECT num FROM currency WHERE uid=? AND id=14', (uid,))}
            db.execute('INSERT INTO battlepass_cycle_history(uid,start_ts,end_ts,data_json) VALUES(?,?,?,?) ON CONFLICT(uid,start_ts) DO NOTHING',
                       (uid, marker[0]['start_ts'], marker[0]['end_ts'], json.dumps(history, ensure_ascii=False)))
            db.execute("UPDATE battlepass SET pay_level=0,weekly_gain_exp=0,receive_info='[]' WHERE uid=?", (uid,))
            db.execute('UPDATE currency SET num=0,update_ts=? WHERE uid=? AND id=14', (now, uid))
            ids = [r['task_id'] for r in db.query('SELECT task_id FROM task_cfg WHERE task_type IN (7,8)')]
            _reset_tasks(db, uid, ids)
        if not old:
            db.execute("INSERT INTO battlepass(uid,battlepass_list_id,pay_level,is_start,weekly_gain_exp,start_timestamp,end_timestamp,next_refresh_timestamp,receive_info,update_ts) VALUES(?,?,0,1,0,?,?,?,'[]',?)",
                       (uid, list_id, start, end, next_refresh, now))
        elif any(old.get(k) != v for k, v in {'battlepass_list_id': list_id, 'is_start': 1,
                    'start_timestamp': start, 'end_timestamp': end,
                    'next_refresh_timestamp': next_refresh}.items()):
            db.execute('UPDATE battlepass SET battlepass_list_id=?,is_start=1,start_timestamp=?,end_timestamp=?,next_refresh_timestamp=? WHERE uid=?',
                       (list_id, start, end, next_refresh, uid))
        if not marker or (int(marker[0]['start_ts']), int(marker[0]['end_ts'])) != (start, end):
            db.execute('INSERT INTO battlepass_cycle(uid,start_ts,end_ts) VALUES(?,?,?) ON CONFLICT(uid) DO UPDATE SET start_ts=excluded.start_ts,end_ts=excluded.end_ts', (uid, start, end))
        return db.query('SELECT * FROM battlepass WHERE uid=?', (uid,))[0]


def _reset_tasks(db, uid, ids):
    if ids:
        slots = ','.join('?' for _ in ids)
        db.execute(f'UPDATE task SET progress=0,complete_flag=0,claimed_ts=0 WHERE uid=? AND task_id IN ({slots})', (uid, *ids))
        db.execute(f"DELETE FROM claim_ledger WHERE uid=? AND kind='task' AND key_id IN ({slots})", (uid, *ids))


def sync_tasks(db, uid, now_ts=None):
    now = int(time.time()) if now_ts is None else int(now_ts)
    with db.transaction(immediate=True):
        _schema(db)
        previous = db.query('SELECT start_ts FROM battlepass_cycle WHERE uid=?', (uid,))
        bp = ensure_battlepass(db, uid, now)
        changes = {'daily_reset': False, 'weekly_reset': False,
                   'battlepass_reset': bool(previous and int(previous[0]['start_ts']) < bp['start_timestamp'])}
        configs = db.query('SELECT task_id,name,task_type FROM task_cfg WHERE task_type IN (5,6,7,8) AND COALESCE(activity_id,0)=0')
        rows = {int(r['task_id']): r for r in db.query('SELECT * FROM task WHERE uid=?', (uid,))}
        # Seed only recurring tasks, with zero progress; no recorded player values.
        for cfg in configs:
            tid = int(cfg['task_id'])
            if tid not in rows:
                db.execute('INSERT INTO task(uid,task_id,name,task_type,progress,complete_flag,expired_ts,claimed_ts) VALUES(?,?,?,?,0,0,0,0)',
                           (uid, tid, cfg['name'], cfg['task_type']))
        for kind, types, start, length in (
                ('daily', DAILY_TYPES, get_daily_5am_ts(now), DAY),
                ('weekly', WEEKLY_TYPES, get_weekly_mon_5am_ts(now), 7 * DAY)):
            marker = db.query('SELECT start_ts FROM task_cycle WHERE uid=? AND kind=?', (uid, kind))
            if marker and marker[0]['start_ts'] > start:
                start = int(marker[0]['start_ts'])
            selected = [r for r in db.query('SELECT t.*,COALESCE(c.task_type,t.task_type) AS current_type FROM task t LEFT JOIN task_cfg c ON c.task_id=t.task_id WHERE t.uid=?', (uid,)) if r['current_type'] in types]
            if marker:
                old_start = int(marker[0]['start_ts'])
            else:
                watermarks = db.query('SELECT last_daily_5am_ts,last_weekly_mon_ts FROM user_timer_watermark WHERE uid=?', (uid,))
                field = 'last_daily_5am_ts' if kind == 'daily' else 'last_weekly_mon_ts'
                old_start = int(watermarks[0].get(field) or start) if watermarks else start
            resetting = old_start < start
            reset_ids = []
            for row in selected:
                deadline = start + length
                if row['current_type'] in (7, 8):
                    deadline = min(deadline, int(bp['end_timestamp']))
                expired = int(row.get('expired_ts') or 0)
                stale = expired > 0 and expired <= start
                claimed_ts = int(row.get('claimed_ts') or 0)
                legacy = expired > deadline + DAY and 0 < claimed_ts < start
                if resetting or (not marker and (stale or legacy)):
                    reset_ids.append(row['task_id'])
                if int(row.get('task_type') or 0) != row['current_type'] or expired != deadline:
                    db.execute('UPDATE task SET task_type=?,expired_ts=? WHERE uid=? AND task_id=?',
                               (row['current_type'], deadline, uid, row['task_id']))
            _reset_tasks(db, uid, reset_ids)
            changes[kind + '_reset'] = resetting or bool(reset_ids)
            if changes[kind + '_reset']:
                pt_id, currency_id = (1, 22) if kind == 'daily' else (3, 35)
                db.execute("UPDATE activity_pt SET active_point=0,get_id_list='[]' WHERE uid=? AND activity_pt_id=?", (uid, pt_id))
                db.execute('UPDATE currency SET num=0 WHERE uid=? AND id=?', (uid, currency_id))
                if kind == 'weekly':
                    db.execute('UPDATE battlepass SET weekly_gain_exp=0 WHERE uid=?', (uid,))
                else:
                    db.execute('UPDATE task SET progress=1 WHERE uid=? AND task_id IN (6001,71001) AND claimed_ts=0', (uid,))
            if not marker or int(marker[0]['start_ts']) != start:
                db.execute('INSERT INTO task_cycle(uid,kind,start_ts) VALUES(?,?,?) ON CONFLICT(uid,kind) DO UPDATE SET start_ts=excluded.start_ts', (uid, kind, start))
    return {**changes, 'battlepass': db.query('SELECT * FROM battlepass WHERE uid=?', (uid,))[0]}


def rotation_status(db, now_ts=None):
    now = int(time.time()) if now_ts is None else int(now_ts)
    cfg = get_settings(db, now)['rotation']
    signature = hashlib.sha256(json.dumps({k: v for k, v in cfg.items() if k != 'updated_at'}, sort_keys=True).encode()).hexdigest()
    length = int(cfg['duration_days']) * DAY
    index = max(0, (now - int(cfg['start_ts'])) // length)
    recorded = db.query('SELECT value FROM server_global_config WHERE key=?', (ROTATION_KEY,))
    state = json.loads(recorded[0]['value']) if recorded else {}
    if state.get('signature') == signature:
        index = max(index, int(state.get('cycle_index', 0)))
    step_index = index % len(cfg['steps'])
    step = cfg['steps'][step_index]
    desired = list(dict.fromkeys(cfg['fixed_pool_ids'] + step['pool_ids']))
    start = int(cfg['start_ts']) + index * length
    return {'enabled': cfg['enabled'], 'waiting': now < int(cfg['start_ts']), 'signature': signature,
            'cycle_index': index, 'step_index': step_index, 'step_name': step['name'],
            'pool_ids': desired, 'start_ts': start, 'next_rotation_ts': start + length,
            'applied_at': int(state.get('applied_at') or 0),
            'notify_required': bool(state.get('notify_required', False))}


def apply_rotation(db, now_ts=None):
    now = int(time.time()) if now_ts is None else int(now_ts)
    with db.transaction(immediate=True):
        status = rotation_status(db, now)
        if not status['enabled'] or status['waiting']:
            return {**status, 'changed': False}
        desired = status['pool_ids']
        import res_version_manager
        from draw_service import DrawService
        svc = DrawService.get_instance(db)
        if str(res_version_manager.get_current_version()) == '229':
            cfg = svc.load_pool_activity_cfg()
            desired = [p for p in desired if not str(p).startswith('503') and int(cfg.get(str(p), {}).get('theme', 0)) < 44]
        if not desired:
            raise ValueError('当前版本没有可用的轮换卡池')
        active = [int(r['pool_id']) for r in db.query('SELECT pool_id FROM active_draw_pool WHERE is_active=1 ORDER BY sort_order,pool_id')]
        old_rows = db.query('SELECT value FROM server_global_config WHERE key=?', (ROTATION_KEY,))
        old = json.loads(old_rows[0]['value']) if old_rows else {}
        changed = active != desired
        if changed:
            success, message = svc.set_active_pools(desired, now_ts=now)
            if not success:
                raise ValueError(message)
        if changed or old.get('signature') != status['signature'] or old.get('cycle_index') != status['cycle_index']:
            state = {**status, 'pool_ids': desired, 'applied_at': now, 'notify_required': True}
            db.execute('INSERT INTO server_global_config(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       (ROTATION_KEY, json.dumps(state, ensure_ascii=False)))
            status['applied_at'] = now
            status['notify_required'] = True
    return {**status, 'pool_ids': desired, 'changed': changed}


def rotation_notified(db, status):
    with db.transaction(immediate=True):
        rows = db.query('SELECT value FROM server_global_config WHERE key=?', (ROTATION_KEY,))
        if rows:
            state = json.loads(rows[0]['value'])
            if state.get('signature') == status['signature'] and state.get('cycle_index') == status['cycle_index']:
                state['notify_required'] = False
                db.execute('UPDATE server_global_config SET value=? WHERE key=?', (json.dumps(state, ensure_ascii=False), ROTATION_KEY))


def get_status(db, now_ts=None):
    now = int(time.time()) if now_ts is None else int(now_ts)
    settings = get_settings(db, now)
    start, end = period_window(db, now)
    poly_start, poly_end = polyhedron_window(db, now)
    return {'settings': settings, 'now_ts': now, 'timezone': time.tzname[0],
            'utc_offset_seconds': -(time.altzone if time.localtime(now).tm_isdst > 0 else time.timezone),
            'battlepass': {'start_ts': start, 'end_ts': end, 'next_weekly_ts': min(end, get_weekly_mon_5am_ts(now) + 7 * DAY)},
            'polyhedron': {'start_ts': poly_start, 'end_ts': poly_end},
            'tasks': {'next_daily_ts': get_daily_5am_ts(now) + DAY, 'next_weekly_ts': get_weekly_mon_5am_ts(now) + 7 * DAY},
            'rotation': rotation_status(db, now)}
