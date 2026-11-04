"""Daily or custom shop restocking with a durable global cycle marker."""
import datetime
import hashlib
import json
import re
import time

from game_cycles import integer
from lazy_timer import get_daily_5am_ts, get_weekly_mon_5am_ts

KEY = 'gm.shop_restock.v1'
STATE_KEY = 'gm.shop_restock.state.v1'
DAY = 86400


def next_clock(clock, now):
    hour, minute = map(int, clock.split(':'))
    current = datetime.datetime.fromtimestamp(now)
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target.timestamp() <= now:
        target += datetime.timedelta(days=1)
    return int(target.timestamp())


def get_settings(db, now_ts=None):
    now = int(time.time()) if now_ts is None else int(now_ts)
    rows = db.query('SELECT value FROM server_global_config WHERE key=?', (KEY,))
    if not rows:
        cfg = {'enabled': True, 'interval_days': 1, 'time': '05:00',
               'shop_ids': [], 'start_ts': next_clock('05:00', now), 'updated_at': now}
        db.execute('INSERT INTO server_global_config(key,value) VALUES(?,?) ON CONFLICT(key) DO NOTHING',
                   (KEY, json.dumps(cfg)))
        rows = db.query('SELECT value FROM server_global_config WHERE key=?', (KEY,))
    return json.loads(rows[0]['value'])


def signature(cfg):
    return hashlib.sha256(json.dumps({k: v for k, v in cfg.items() if k != 'updated_at'}, sort_keys=True).encode()).hexdigest()


def state(db):
    rows = db.query('SELECT value FROM server_global_config WHERE key=?', (STATE_KEY,))
    return json.loads(rows[0]['value']) if rows else {}


def window(cfg, saved, now):
    length = int(cfg['interval_days']) * DAY
    index = max(-1, (int(now) - int(cfg['start_ts'])) // length)
    if saved.get('signature') == signature(cfg):
        index = max(index, int(saved.get('index', -1)))
    return index, int(cfg['start_ts']) + (index + 1) * length


def covers(cfg, shop_id):
    return bool(cfg['enabled']) and (not cfg['shop_ids'] or int(shop_id) in cfg['shop_ids'])


def managed(cfg, shop_id, cycle):
    return covers(cfg, shop_id) and int(cycle) in (2, 3, 4)


def cycle_for(db, shop_id, goods_id):
    rows = db.query('SELECT refresh_cycle FROM shop_goods WHERE goods_id=?', (goods_id,))
    return int(rows[0]['refresh_cycle'] or 0) if rows else (4 if int(shop_id) == 2 else 0)


def save_settings(db, incoming, now_ts=None):
    now = int(time.time()) if now_ts is None else int(now_ts)
    allowed = {'enabled', 'interval_days', 'time', 'shop_ids', 'start_ts'}
    if not isinstance(incoming, dict) or set(incoming) - allowed:
        raise ValueError('补货配置格式无效')
    with db.transaction(immediate=True):
        old = get_settings(db, now)
        cfg = {**old, **incoming}
        if not isinstance(cfg['enabled'], bool):
            raise ValueError('补货启用状态须为布尔值')
        cfg['interval_days'] = integer(cfg['interval_days'], '补货间隔天数', 1, 365)
        if not isinstance(cfg['time'], str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', cfg['time']):
            raise ValueError('补货时刻须为 HH:MM')
        if not isinstance(cfg['shop_ids'], list) or len(cfg['shop_ids']) > 200:
            raise ValueError('商店范围须为数组，最多 200 项')
        ids = [integer(sid, '商店编号', 1) for sid in cfg['shop_ids']]
        available = {int(r['shop_id']) for r in db.query('SELECT DISTINCT shop_id FROM shop_goods')} | {2}
        if len(ids) != len(set(ids)) or set(ids) - available:
            raise ValueError('商店范围含重复或不存在的商店')
        cfg['shop_ids'] = sorted(ids)
        changed = any(cfg[k] != old[k] for k in ('enabled', 'interval_days', 'time', 'shop_ids'))
        if 'start_ts' in incoming:
            cfg['start_ts'] = integer(incoming['start_ts'], '首次补货时间', 1, 4294967295)
            dt = datetime.datetime.fromtimestamp(cfg['start_ts'])
            if dt.strftime('%H:%M') != cfg['time']:
                raise ValueError('首次补货时间须与补货时刻一致')
            changed = changed or cfg['start_ts'] != old['start_ts']
        elif changed:
            cfg['start_ts'] = next_clock(cfg['time'], now)
        if changed:
            cfg['updated_at'] = now
            # Adoption never replenishes stock merely because settings were saved.
            current_index, _ = window(cfg, {}, now)
            saved = {'signature': signature(cfg), 'index': current_index,
                     'last_due_ts': 0, 'manual_ts': 0, 'notify_required': False}
            db.execute('INSERT INTO server_global_config(key,value) VALUES(?,?) '
                       'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (STATE_KEY, json.dumps(saved)))
        db.execute('UPDATE server_global_config SET value=? WHERE key=?', (json.dumps(cfg), KEY))
    return get_status(db, now)


def get_status(db, now_ts=None):
    now = int(time.time()) if now_ts is None else int(now_ts)
    cfg, saved = get_settings(db, now), state(db)
    _, next_ts = window(cfg, saved, now)
    return {'settings': cfg, 'next_restock_ts': next_ts if cfg['enabled'] else 0,
            'last_restock_ts': max(int(saved.get('last_due_ts', 0)), int(saved.get('manual_ts', 0))),
            'timezone': time.tzname[0],
            'utc_offset_seconds': -(time.altzone if time.localtime(now).tm_isdst > 0 else time.timezone)}


def daily_window(db, uid, now):
    cfg = get_settings(db, now)
    if not covers(cfg, 2):
        return get_daily_5am_ts(now), get_daily_5am_ts(now) + DAY
    saved = state(db)
    _, next_ts = window(cfg, saved, now)
    marker = max(int(saved.get('last_due_ts', 0)), int(saved.get('manual_ts', 0)))
    if not marker:
        rows = db.query('SELECT last_refresh_ts FROM user_daily_shop WHERE uid=?', (uid,))
        marker = int(rows[0]['last_refresh_ts'] or 0) if rows else get_daily_5am_ts(now)
    return marker, next_ts


def native_next(cycle, now):
    if cycle in (0, 1):
        return 0
    if cycle == 4:
        return get_daily_5am_ts(now) + DAY
    if cycle == 3:
        return get_weekly_mon_5am_ts(now) + 7 * DAY
    if cycle == 2:
        dt = datetime.datetime.fromtimestamp(now)
        target = dt.replace(day=1, hour=5, minute=0, second=0, microsecond=0)
        if target.timestamp() <= now:
            target = target.replace(year=target.year + 1, month=1) if target.month == 12 else target.replace(month=target.month + 1)
        return int(target.timestamp())
    return now + cycle * DAY


def normalize(cfg, saved, shop_id, cycle, bought, refresh_ts, now):
    bought, refresh_ts = int(bought or 0), int(refresh_ts or 0)
    if cycle == 1:
        return bought, 0
    if managed(cfg, shop_id, cycle):
        # Old purchases already expired before adoption remain expired.
        if refresh_ts > 0 and refresh_ts <= max(int(cfg['updated_at']), int(saved.get('last_due_ts', 0))):
            bought = 0
        return bought, window(cfg, saved, now)[1]
    return (0, 0) if refresh_ts > 0 and refresh_ts <= now else (bought, refresh_ts)


def normalize_payload(db, payload, now, uid):
    cfg, saved = get_settings(db, now), state(db)
    cycles = {int(r['goods_id']): int(r['refresh_cycle'] or 0)
              for r in db.query('SELECT goods_id,refresh_cycle FROM shop_goods')}
    purchases = {(int(r['shop_id']), int(r['goods_id'])): r for r in db.query(
        'SELECT shop_id,goods_id,buy_times,next_refresh_timestamp FROM store_purchase WHERE uid=?', (uid,))}
    shops = payload.get('shop_item_list') or [payload.get('changed_shop_info', {})]
    for shop in shops:
        sid = int(shop.get('shop_id') or 0)
        for item in shop.get('goods_list', []):
            cycle = cycles.get(int(item['goods_id']), 4 if sid == 2 else 0)
            purchase = purchases.get((sid, int(item['goods_id'])), {})
            item['buy_times'], item['next_refresh_timestamp'] = normalize(
                cfg, saved, sid, cycle, purchase.get('buy_times'),
                purchase.get('next_refresh_timestamp', item.get('next_refresh_timestamp')), now)
    return payload


def reset_native_cycle(db, uid, cycle, now, force=False):
    cfg = get_settings(db, now)
    for row in db.query('''SELECT p.shop_id,p.goods_id,g.refresh_cycle FROM store_purchase p
                          LEFT JOIN shop_goods g ON g.goods_id=p.goods_id WHERE p.uid=?''', (uid,)):
        sid = int(row['shop_id'])
        native_cycle = int(row['refresh_cycle']) if row['refresh_cycle'] is not None else (4 if sid == 2 else 0)
        if native_cycle == cycle and (force or not managed(cfg, sid, cycle)):
            db.execute('DELETE FROM store_purchase WHERE uid=? AND shop_id=? AND goods_id=?', (uid, sid, row['goods_id']))


def expire_native(db, uid, now):
    cfg = get_settings(db, now)
    for row in db.query('''SELECT p.shop_id,p.goods_id,p.next_refresh_timestamp,g.refresh_cycle FROM store_purchase p
                          LEFT JOIN shop_goods g ON g.goods_id=p.goods_id
                          WHERE p.uid=? AND p.next_refresh_timestamp>0 AND p.next_refresh_timestamp<=?''', (uid, now)):
        sid = int(row['shop_id'])
        cycle = int(row['refresh_cycle']) if row['refresh_cycle'] is not None else (4 if sid == 2 else 0)
        if cycle == 1:
            continue
        if not managed(cfg, sid, cycle) or int(row['next_refresh_timestamp']) <= int(cfg['updated_at']):
            db.execute('DELETE FROM store_purchase WHERE uid=? AND shop_id=? AND goods_id=?', (uid, sid, row['goods_id']))


def apply_due(db, now_ts=None, force=False):
    now = int(time.time()) if now_ts is None else int(now_ts)
    with db.transaction(immediate=True):
        cfg, old = get_settings(db, now), state(db)
        sig = signature(cfg)
        index, _ = window(cfg, old, now)
        pending = old if old.get('signature') == sig else {}
        if not force and (not cfg['enabled'] or index < 0 or int(pending.get('index', -1)) >= index):
            return {'changed': False, 'notify_required': bool(pending.get('notify_required')),
                    'signature': sig, 'index': index, 'deleted': 0, 'shelves': 0}
        saved = {'signature': sig, 'index': index, 'notify_required': True,
                 'last_due_ts': int(cfg['start_ts']) + index * cfg['interval_days'] * DAY if index >= 0 else 0,
                 'manual_ts': now if force else int(pending.get('manual_ts', 0))}
        db.execute('INSERT INTO server_global_config(key,value) VALUES(?,?) '
                   'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (STATE_KEY, json.dumps(saved)))
        deleted = 0
        for row in db.query('''SELECT p.uid,p.shop_id,p.goods_id,g.refresh_cycle FROM store_purchase p
                              LEFT JOIN shop_goods g ON g.goods_id=p.goods_id'''):
            sid = int(row['shop_id'])
            cycle = int(row['refresh_cycle']) if row['refresh_cycle'] is not None else (4 if sid == 2 else 0)
            if cycle in (2, 3, 4) and (not cfg['shop_ids'] or sid in cfg['shop_ids']):
                db.execute('DELETE FROM store_purchase WHERE uid=? AND shop_id=? AND goods_id=?',
                           (row['uid'], sid, row['goods_id']))
                deleted += 1
        shelves = 0
        if not cfg['shop_ids'] or 2 in cfg['shop_ids']:
            from shop_service import ShopService
            service = ShopService.get_instance(db=db)
            for row in db.query('SELECT uid FROM user_daily_shop'):
                uid = int(row['uid'])
                rolled = service.roll_daily_shop_goods(uid, force_new=True, now_ts=now)
                stored = db.query('SELECT goods_list_json,last_refresh_ts FROM user_daily_shop WHERE uid=?', (uid,))
                if not stored or int(stored[0]['last_refresh_ts']) != now or json.loads(stored[0]['goods_list_json']) != rolled:
                    raise ValueError('补货货架保存失败，下一次检查将重试')
                db.execute('UPDATE user_daily_shop SET refresh_times=0 WHERE uid=?', (uid,))
                shelves += 1
    return {'changed': True, 'notify_required': True, 'signature': sig, 'index': index,
            'deleted': deleted, 'shelves': shelves}


def notify(db, result):
    if not result['notify_required']:
        return
    from server_net import get_online_uids
    from shop_service import ShopService
    from codec import encode
    from middleware import DownFrame
    import gm_actions
    service = ShopService.get_instance(db=db)
    for uid in get_online_uids():
        payload = encode('sc_20009', service.get_shop_data_payload(uid))
        if not payload:
            raise ValueError('补货状态无法生成客户端同步帧')
        gm_actions.sync(uid, [DownFrame(20009, payload)])
    with db.transaction(immediate=True):
        saved = state(db)
        if saved.get('signature') == result['signature'] and saved.get('index') == result['index']:
            saved['notify_required'] = False
            db.execute('UPDATE server_global_config SET value=? WHERE key=?', (json.dumps(saved), STATE_KEY))
