"""Level-based personal GM actions, authenticated with the game account."""
import json
import time
from functools import lru_cache
from pathlib import Path

from flask import Blueprint, g, jsonify, request

from auth_service import AuthError
from game_cycles import integer
import gm_actions
from inventory_service import InventoryService, ItemType

KEY = 'gm.self_permissions.v1'
DEFAULTS = {'enabled': True, 'currency_level': 10, 'material_level': 20,
            'hero_level': 40, 'equipment_level': 60}
LEVEL_KEYS = {'currency': 'currency_level', 'materials': 'material_level',
              'heroes': 'hero_level', 'equipment': 'equipment_level'}
LABELS = {'currency': '货币奖励', 'materials': '养成材料',
          'heroes': '角色解锁与培养', 'equipment': '刻印与钥从'}
LIMITS = {'currency': 1000000, 'materials': 10000, 'heroes': 1, 'equipment': 200}


@lru_cache(maxsize=1)
def hero_names():
    path = Path(__file__).parent / 'data/heroes_catalog.json'
    rows = json.loads(path.read_text()) if path.exists() else []
    return {int(row['id']): row.get('official_name') or row.get('name') for row in rows}


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS gm_self_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,uid INTEGER NOT NULL,
        action TEXT NOT NULL,item_id INTEGER NOT NULL,amount INTEGER NOT NULL,
        created_at INTEGER NOT NULL)''')
    db.execute('INSERT INTO server_global_config(key,value) VALUES(?,?) ON CONFLICT(key) DO NOTHING',
               (KEY, json.dumps(DEFAULTS)))


def get_policy(db):
    rows = db.query('SELECT value FROM server_global_config WHERE key=?', (KEY,))
    return {**DEFAULTS, **(json.loads(rows[0]['value']) if rows else {})}


def save_policy(db, incoming):
    if not isinstance(incoming, dict) or set(incoming) - set(DEFAULTS):
        raise ValueError('子 GM 权限配置格式无效')
    with db.transaction(immediate=True):
        policy = {**get_policy(db), **incoming}
        if not isinstance(policy['enabled'], bool):
            raise ValueError('启用状态必须为布尔值')
        levels = []
        for key in LEVEL_KEYS.values():
            policy[key] = integer(policy[key], '权限等级', 1, 200)
            levels.append(policy[key])
        if levels != sorted(levels):
            raise ValueError('货币、材料、角色、装备的权限等级须依次递增或相同')
        db.execute('INSERT INTO server_global_config(key,value) VALUES(?,?) '
                   'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                   (KEY, json.dumps(policy)))
    return policy


def item_capability(db, item_id, admin=False):
    if item_id in (52001, 52002, 52003) or (item_id == 12 and not admin):
        raise ValueError('该资源不能由个人子 GM 发放')
    kind = gm_actions.require_item(db, item_id, 1)
    if kind == ItemType.CURRENCY:
        return 'currency'
    if kind in (ItemType.MATERIAL, ItemType.HERO_PIECE, ItemType.CANTEEN_INGREDIENTS):
        return 'materials'
    if kind == ItemType.HERO:
        return 'heroes'
    if kind in (ItemType.EQUIP, ItemType.SERVANT):
        return 'equipment'
    raise ValueError('该物品不属于个人奖励或养成物品')


def install(app, db, auth):
    initialize(db)
    bp = Blueprint('sub_gm', __name__)

    def ok(data=None):
        return jsonify(code=0, msg='成功', data=data)

    def account():
        return g.account

    def game(uid):
        rows = db.query('SELECT uid,nick,level FROM users WHERE uid=?', (uid,))
        if not rows:
            raise AuthError('游戏存档不存在', 404)
        return rows[0]

    def allowed(owner, capability):
        policy = get_policy(db)
        level = int(game(owner['uid'])['level'] or 1)
        return bool(owner['is_admin']) or (policy['enabled'] and level >= policy[LEVEL_KEYS[capability]])

    def require_capability(owner, capability):
        if not allowed(owner, capability):
            policy = get_policy(db)
            if not policy['enabled']:
                raise AuthError('个人子 GM 功能当前已关闭', 403)
            raise AuthError(f"{LABELS[capability]}需要游戏账号达到 Lv.{policy[LEVEL_KEYS[capability]]}", 403)

    def values(keys):
        if (request.content_length or 0) > 16384:
            raise AuthError('请求过大', 413)
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) != set(keys):
            raise ValueError('请求参数无效，子 GM 只能操作自己的游戏账号')
        return data

    def guard(owner, capability, action, item_id, amount):
        uid = int(owner['uid'])
        def authorize():
            token = request.headers.get('Authorization', '')
            current = auth.verify(token[7:] if token.startswith('Bearer ') else '', {'web'}, uid=uid)
            require_capability(current, capability)
            # This record commits with the reward and rolls back on grant failures.
            db.execute('INSERT INTO gm_self_audit(uid,action,item_id,amount,created_at) VALUES(?,?,?,?,?)',
                       (uid, action, item_id, amount, int(time.time())))
        return authorize

    @bp.get('/me')
    def me():
        from server_net import game_presence
        owner = account()
        uid = int(owner['uid'])
        policy = get_policy(db)
        hero_ids = sorted(gm_actions.allowed_heroes())
        names = {int(row['id']): row['name'] for row in db.query('SELECT id,name FROM item_catalog WHERE id<10000')}
        names.update(hero_names())
        owned = {int(row['id']): row for row in db.query('SELECT id,level,unlock FROM hero WHERE uid=?', (uid,))}
        heroes = [{'id': hid, 'name': names.get(hid) or str(hid),
                   'owned': bool(owned.get(hid, {}).get('unlock')),
                   'level': int(owned.get(hid, {}).get('level') or 0)} for hid in hero_ids]
        return ok({'account': auth.public(owner), 'game': game(uid), 'enabled': policy['enabled'],
                   'online': bool(game_presence().get(uid, {}).get('online')),
                   'permissions': {key: {'label': LABELS[key], 'level': policy[value],
                                        'allowed': allowed(owner, key)} for key, value in LEVEL_KEYS.items()},
                   'limits': LIMITS, 'heroes': heroes,
                   'history': db.query('SELECT action,item_id,amount,created_at FROM gm_self_audit '
                                       'WHERE uid=? ORDER BY id DESC LIMIT 20', (uid,))})

    @bp.get('/items')
    def items():
        owner = account()
        category = request.args.get('category', 'currency')
        if category not in LEVEL_KEYS:
            raise ValueError('物品分类无效')
        require_capability(owner, category)
        search = request.args.get('search', '').strip().lower()
        if len(search) > 100:
            raise ValueError('搜索内容过长')
        result = []
        for row in db.query('SELECT id,name FROM item_catalog ORDER BY id'):
            if search and search not in f"{row['id']} {row['name']}".lower():
                continue
            try:
                kind = item_capability(db, int(row['id']), bool(owner['is_admin']))
            except ValueError:
                continue
            if kind == category:
                result.append({'id': int(row['id']), 'name': row['name'] or str(row['id'])})
                if len(result) == 100:
                    break
        return ok({'items': result})

    @bp.post('/grant')
    def grant():
        owner = account()
        data = values(('item_id', 'count'))
        iid = integer(data['item_id'], '物品编号', 1)
        category = item_capability(db, iid, bool(owner['is_admin']))
        count = integer(data['count'], '发放数量', 1, LIMITS[category])
        return ok(gm_actions.grant(db, int(owner['uid']), iid, count,
                                  guard=guard(owner, category, 'grant', iid, count)))

    @bp.post('/hero/unlock')
    def unlock():
        owner = account()
        hid = integer(values(('hero_id',))['hero_id'], '角色编号', 1)
        return ok(gm_actions.unlock(db, int(owner['uid']), hero_id=hid,
                                   guard=guard(owner, 'heroes', 'unlock', hid, 1)))

    @bp.post('/hero/graduate')
    def graduate():
        owner = account()
        hid = integer(values(('hero_id',))['hero_id'], '角色编号', 1)
        return ok(gm_actions.graduate(db, int(owner['uid']), hid,
                                     guard=guard(owner, 'heroes', 'graduate', hid, 1)))

    @bp.errorhandler(ValueError)
    def invalid(error):
        return jsonify(code=400, msg=str(error), data=None), 400

    @bp.errorhandler(AuthError)
    def unauthorized(error):
        return jsonify(code=error.status, msg=str(error), data=None), error.status

    @app.get('/api/gm/sub-gm/policy')
    def get_sub_gm_policy():
        return ok(get_policy(db))

    @app.post('/api/gm/sub-gm/policy')
    def post_sub_gm_policy():
        try:
            return ok(save_policy(db, request.get_json(silent=True)))
        except ValueError as error:
            raise AuthError(str(error)) from error

    app.register_blueprint(bp, url_prefix='/api/sub-gm')
