"""Build disposable regression saves from public configuration and synthetic state.

The numeric fixture UIDs retain compatibility with the existing test suite. No
account database, recording, token, password, or network service is an input.
"""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import zlib
from unittest.mock import patch

SERVER = Path(__file__).resolve().parent
UID, OTHER, ADMIN = 2174928301, 264084870, 10001
EPOCH = 1780000000
VERSION = 1
_sandbox = None
_seed = None


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def get_seed_path():
    """One generated seed per Python process, or an explicitly supplied fixture."""
    supplied = os.environ.get('GM_TEST_DB_SEED')
    if supplied:
        path = Path(supplied).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError('GM_TEST_DB_SEED is not a file: ' + str(path))
        return path
    global _sandbox, _seed
    if _seed is None:
        _sandbox = tempfile.TemporaryDirectory(prefix='apc-generated-seed-')
        _seed = Path(_sandbox.name) / 'test-seed.db'
        generate_seed(_seed)
    return _seed


def generate_seed(output, server=SERVER):
    """Generate in a child process so importing tests cannot change DB defaults.

    An existing destination is always refused, including an existing generated
    database. The manifest is written beside the database as .manifest.json.
    """
    output = Path(output).expanduser().absolute()
    server = Path(server).resolve()
    manifest = output.with_name(output.name + '.manifest.json')
    if output.exists() or manifest.exists():
        raise FileExistsError('Destination already exists: ' + str(output))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.apc-seed-', dir=output.parent) as tmp:
        staging = Path(tmp) / 'seed.db'
        env = dict(os.environ, ACCOUNT_DB_PATH=str(staging), PYTHONDONTWRITEBYTECODE='1')
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                                 '--build', str(staging), '--server', str(server)],
                                cwd=server, env=env, text=True, capture_output=True)
        if result.returncode:
            raise RuntimeError('Seed generation failed:\n' + result.stdout + result.stderr)
        # Exclusive publication prevents a concurrent process from replacing a save.
        os.link(staging, output)
        try:
            os.link(staging.with_name('seed.db.manifest.json'), manifest)
        except BaseException:
            output.unlink()
            raise
    return json.loads(manifest.read_text(encoding='utf-8'))


class SeedBuilder:
    def __init__(self, server, output):
        self.server, self.output = server, output
        self.inputs = {}
        os.environ['ACCOUNT_DB_PATH'] = str(output)
        sys.path.insert(0, str(server))
        sys.dont_write_bytecode = True

        def isolated(event, args):
            if event in ('socket.connect', 'socket.getaddrinfo'):
                raise RuntimeError('Seed generation is offline')
            if event == 'sqlite3.connect':
                name = os.fsdecode(args[0])
                if name != ':memory:' and Path(name).resolve() != output.resolve():
                    raise RuntimeError('Only the new seed may be opened: ' + name)
        sys.addaudithook(isolated)
        from account_db import AccountDB
        self.db = AccountDB(str(output))
        for name in ('db_schema.sql', 'account_db.py', 'codec.py', 'decode_schema.py',
                     'lua_proto_messages.json', 'schema_default.py', 'data/ai_characters_roster.json'):
            self.source(name)

    def source(self, name):
        path = self.server / name
        self.inputs[name] = digest(path)
        return path

    def data(self, name):
        return json.loads(self.source(name).read_text(encoding='utf-8'))

    def insert(self, table, **row):
        fields = ','.join('"' + key + '"' for key in row)
        self.db.execute('INSERT OR REPLACE INTO "' + table + '" (' + fields +
                        ') VALUES (' + ','.join('?' for _ in row) + ')',
                        tuple(json.dumps(value, ensure_ascii=False, sort_keys=True)
                              if isinstance(value, (dict, list)) else value for value in row.values()))

    def catalog(self):
        self.heroes = {row['id']: row for row in self.data('data/heroes_catalog.json')}
        self.skills = self.data('hero_skills_cfg.json')
        names, races = self.data('hero_names_catalog.json'), self.data('hero_race_map.json')
        for hid in sorted(map(int, self.skills)):
            info = self.heroes.get(hid, {})
            self.insert('hero_cfg', hero_id=hid, unlock_star=info.get('unlock_star', 300),
                        name=names.get(str(hid), {}).get('name', str(hid))
                        if isinstance(names.get(str(hid)), dict) else names.get(str(hid), str(hid)),
                        race=races.get(str(hid), 1))
        categories = {1: 'currency', 2: 'hero', 3: 'hero_piece', 7: 'equip', 9: 'servant', 17: 'guild'}
        catalog = {row['id']: dict(id=row['id'], name=row['name'], type=row['type'], rare=row['rare'],
                                  category=categories.get(row['type'], 'material'))
                   for row in self.data('static_web/extracted_assets/items/items_catalog.json')}
        def item(iid, kind=4, rare=3, category='material', name=None):
            catalog.setdefault(int(iid), dict(id=int(iid), name=name or 'Fixture item ' + str(iid),
                                              type=kind, rare=rare, category=category))
        for iid in range(1, 100):
            item(iid, 1, category='currency')
        for iid in (101, 53029, 53111):
            item(iid, 1, category='currency')
        for iid in self.data('item_usable_cfg.json'):
            item(iid)
        self.servants = self.data('data/weapon_servants_catalog.json')
        for iid, row in self.servants.items():
            item(iid, 9, row['star'], 'servant', row['name'])
        for prefab in self.data('data/hero_details_catalog.json')['equip_prefabs'].values():
            item(prefab['id'], 7, prefab['star'], 'equip', prefab['suit_name'])
        for hid, row in self.heroes.items():
            item(hid, 2, row.get('rare', 3) + 2, 'hero', row['official_name'])
        self.boxes = self.data('item_random_box_rules.json')['boxes']
        for box in self.boxes.values():
            for iid in box['item_ids']:
                item(iid, *box['param'], category='hero_piece' if box['param'][0] == 3 else 'material')
        for iid in (*range(40101, 40105), *range(40201, 40204), *range(40301, 40304),
                    *range(40411, 40466), *range(40501, 40504), *range(41301, 41304)):
            item(iid)
        for iid in (50001, 50002):
            item(iid, 10, 4)
        for iid in (52001, 52002, 52003):
            item(iid, 17, category='guild')
        self.items = catalog
        for row in sorted(catalog.values(), key=lambda row: row['id']):
            self.insert('item_catalog', **row)

    def growth(self):
        levels = self.data('trial_system_data.json')['level_cfgs']
        for level in sorted(map(int, levels)):
            total = int(levels[str(level)]['hero_exp'])
            following = levels.get(str(level + 1), levels[str(level)])
            self.insert('game_level_setting', level=level, hero_exp_sum=total,
                        hero_exp_need=int(following['hero_exp']) - total,
                        fatigue_upgrade_reward=30, battlepass_level_exp=1000)
        # Deliberate synthetic rules exercise all account and character gates.
        # The first rule is the existing regression example: 500 gold + 16 chips.
        for race in range(1, 10):
            for step, cap in enumerate((20, 40, 50, 60, 70, 80)):
                self.insert('hero_break_cfg', id=race * 100 + step, race=race,
                            break_times=step, user_level=(20, 30, 40, 50, 60, 70)[step],
                            max_level=cap, cost_gold=500 * (step + 1),
                            cost=[[40411, 16 * (step + 1)]])
        for hid in sorted(map(int, self.skills)):
            for level in range(1, 16):
                self.insert('hero_skill_element_cfg', id=hid * 100 + level,
                            hero_id=hid, level=level,
                            cost={str(slot): [[40301, (level + 10) * 10], [40302, (level + 4) * 2]]
                                  for slot in range(1, 6)})

    def tasks(self):
        phases = self.data('assignment_phase.json')
        overrides = {
            6002: (402, 1, [[22, 20]], []),
            6004: (402, 5, [[22, 20]], []),
            120001: (2001, 1, [[40, 50], [52001, 5], [52002, 20], [52003, 50]], []),
            120002: (2002, 1, [[40, 50], [52001, 5], [52002, 20], [52003, 50]], []),
            120003: (402, 20, [[40, 200], [52001, 15], [52002, 100], [52003, 150]], []),
            120004: (402, 50, [[40, 200], [52001, 15], [52002, 100], [52003, 150]], []),
            120005: (600, 1, [[40, 200], [52001, 15], [52002, 100], [52003, 150]], [[1], 0]),
            120006: (2021, 1, [[40, 200], [52001, 15], [52002, 100], [52003, 150]], []),
            32900: (500, 7, [[1, 1]], [3, 1]),
        }
        for tid in sorted(map(int, phases)):
            cond, need, rewards, params = overrides.get(tid, (0, 1, [[1, 1]], []))
            self.insert('task_cfg', task_id=tid, name='Fixture task ' + str(tid),
                        task_type=phases[str(tid)]['type'], condition=cond,
                        need=need, reward_json=rewards, additional_parameter=params)
        for tid in range(50101001, 50101005):
            self.insert('task_cfg', task_id=tid, name='Fixture resident task',
                        task_type=101, condition=422001, need=1, activity_id=4243101,
                        reward_json=[[1, 1]])

    def pools(self):
        expansions = self.data('data/expansion_pools_catalog.json')
        metadata = {int(row['pool_id']): row for row in expansions}
        up_cfg = self.data('pool_up_config.json')
        pool_ids = {10001, 10002, 4050301, 4050601, 5020301, 5020601}
        pool_ids.update(int(row['pool_id']) for row in expansions)
        for pid in sorted(pool_ids):
            group = metadata.get(pid, {}).get('pool_group',
                'weapon_servant_70' if pid == 10002 else 'hero_standard_70' if pid == 10001 else 'hero_precision_70')
            kind = {'weapon_servant_70': 2, 'hero_standard_70': 1,
                    'hero_precision_70': 3, 'hero_precision_90': 6}[group]
            ticket = 19 if kind == 2 else 5 if kind == 1 else 38
            cfg = dict(name=metadata.get(pid, {}).get('pool_name', 'Fixture pool ' + str(pid)),
                       pool_type=kind, cost_once=[ticket, 1], cost_ten_times=[ticket, 10])
            cfg.update(up_cfg.get(str(pid), {}))
            self.insert('draw_pool', pool_id=pid, name=cfg['name'],
                        pool_type=cfg['pool_type'], ssr_rate=1.6, detail_json=cfg)
        for pid, cfg in self.data('choice_pool_defaults.json').items():
            self.insert('draw_pool', pool_id=int(pid), name=cfg['name'],
                        pool_type=cfg['pool_type'], ssr_rate=1.6, detail_json=cfg)

    def shops(self):
        self.shop_rows = {}
        for rows in self.data('daily_shop_pool.json').values():
            for entry in rows:
                gid = entry['goods_id']
                row = dict(goods_id=gid, shop_id=2, item_id=entry['give_id'],
                           name='Fixture goods ' + str(gid), cost_id=entry['cost_id'],
                           cost=entry['cost'], cheap_cost=entry['cheap_cost'],
                           limit_num=entry['limit_num'], refresh_cycle=4,
                           detail_json=dict(entry, id=gid, description=entry['give_id'],
                                            give=entry['give_num']))
                self.shop_rows[gid] = row
        recharge = self.data('recharge_shop_catalog.json')
        # Synthetic shelf identities refer to public item/pack definitions. Include
        # an obsolete popup so resource-compatibility rejection is exercised too.
        for gid, sid, description in ((3009003, 3, 30041), (3009004, 3, 30060),
                                       (3001410, 3, 20001), (4000001, 4, 20001),
                                       (5000007, 5, 31505)):
            self.shop_rows[gid] = dict(goods_id=gid, shop_id=sid, item_id=description,
                name=recharge[str(description)]['name'], cost_id=1, cost=0, limit_num=1,
                refresh_cycle=4, detail_json=dict(id=gid, description=description, give=1,
                                                 cost_id=1, cost=0, limit_num=1, refresh_cycle=4))
        for row in sorted(self.shop_rows.values(), key=lambda row: row['goods_id']):
            self.insert('shop_goods', **row)
        for sid in (1, 2, 3, 4, 5):
            self.insert('shop', shop_id=sid, name='Fixture shop ' + str(sid))

    def players(self):
        from enchant_slots_util import default_slot_list
        self.source('enchant_slots_util.py')
        # Use the public authentication DDL without generating password hashes or
        # bootstrap/session tokens. These tables are also read by mail scheduling.
        auth = ast.parse(self.source('auth_service.py').read_text(encoding='utf-8'))
        auth_class = next(n for n in auth.body if isinstance(n, ast.ClassDef) and n.name == 'AuthService')
        init = next(n for n in auth_class.body if isinstance(n, ast.FunctionDef) and n.name == '__init__')
        ddl = next(n.iter for n in init.body if isinstance(n, ast.For) and isinstance(n.target, ast.Name) and n.target.id == 'sql')
        for sql in ast.literal_eval(ddl):
            self.db.execute(sql)
        user_levels = self.data('user_level_setting.json')
        bp_id = int(next(iter(self.data('battlepass_cfg.json')['entries'])))
        for uid, name, level in ((UID, 'Synthetic player', 80), (OTHER, 'Synthetic observer', 13),
                                 (ADMIN, 'Synthetic administrator', 1)):
            total_exp = sum(user_levels[str(i)] for i in range(1, level))
            self.insert('users', uid=uid, account='fixture-' + str(uid), nick=name,
                        level=level, exp=total_exp, portrait=1084, register_ts=EPOCH)
            self.insert('game_user', uid=uid, board_hero=1084, cur_portrait=1084,
                        show_hero=[1084], update_ts=EPOCH)
            if uid == ADMIN:
                continue
            # A returning player must have an earlier pulse. A missing watermark
            # would initialize at the request time and bypass the cycle boundary.
            self.insert('user_timer_watermark', uid=uid, last_pulse_ts=EPOCH,
                        last_heartbeat_ts=EPOCH, is_online=0, update_ts=EPOCH)
            for row in self.items.values():
                iid = row['id']
                if row['category'] == 'currency':
                    self.insert('currency', uid=uid, id=iid, num=2900 if iid == 45 else 10000,
                                last_fatigue_recover_time=EPOCH if iid == 4 else 0, update_ts=EPOCH)
                elif row['category'] in ('material', 'hero_piece'):
                    self.insert('material', uid=uid, id=iid, num=100, update_ts=EPOCH)
                    if row['type'] == 3:
                        self.insert('hero_piece', uid=uid, hero_id=iid - 10000, num=100)
            for hid in sorted(map(int, self.skills)):
                columns = self.db.query('PRAGMA table_info(hero)')
                hero = {r['name']: ([] if r['type'] == 'TEXT' else 0) for r in columns}
                hero.update(uid=uid, id=hid, level=1, star=300, unlock=1,
                            name='Fixture hero ' + str(hid), skill_list=[[sid, 1] for sid in self.skills[str(hid)]],
                            equip_slot={}, chip_state={}, trust_mood=1, update_ts=EPOCH)
                self.insert('hero', **hero)
            self.insert('equip', uid=uid, id=800000001, prefab_id=510001,
                        exp=0, hero_id=0, is_lock=0, now_break_level=0,
                        enchant_slots=default_slot_list(), race=0, race_preview=0,
                        race_hero=0, race_faction=0, update_ts=EPOCH, is_init=0)
            self.insert('servant', uid=uid, id=700000001, prefab_id=2310001,
                        name=self.servants['2310001']['name'], race=1, starlevel=3,
                        stage=1, owned=1, is_locked=0, update_ts=EPOCH)
            for pid, group, up in ((10001, 'hero_standard_70', 0), (5030601, 'hero_precision_90', 1044)):
                self.insert('draw_state', uid=uid, pool_id=pid, since_ssr=7,
                            total_draws=12, up_id=up, is_up_guaranteed=0, pool_group=group)
            self.insert('loading_set', uid=uid, id_list=[2109401, 2109403, 2109402], update_ts=EPOCH)
            self.db.execute('INSERT INTO task(uid,task_id,name,task_type) '
                            'SELECT ?,task_id,name,task_type FROM task_cfg WHERE task_type<>210', (uid,))
            for sid in (4300101, 4400101):
                self.insert('sign', uid=uid, activity_id=sid, sign_count=2, last_sign_ts=EPOCH)
            self.insert('battlepass', uid=uid, battlepass_list_id=2, pay_level=1,
                        weekly_gain_exp=123, receive_info=[{'id': bp_id, 'is_pay': 0}], update_ts=EPOCH)
            self.insert('polyhedron_meta', uid=uid, activity_id=4307301, game_state=1,
                        reset_times=2, update_ts=EPOCH)
            self.insert('polyhedron_policy_claimed', uid=uid, activity_id=4307301, level=1, update_ts=EPOCH)
            self.insert('newbie_activity', uid=uid, trigger_time=EPOCH, max_phase=1, version_id=3)
            self.insert('regression', uid=uid, trigger_time=EPOCH, end_ts=EPOCH + 60 * 86400,
                        return_vs_id=110, state_version=1, find_time=1)
            self.insert('backhome_canteen_meta', uid=uid, canteen_id=4, update_ts=EPOCH)
            self.insert('accumulate_sign', uid=uid, version=2, open_sign=1)
            self.insert('autochess_meta', uid=uid)
            self.insert('player_card', uid=uid, kind='game_icon', item_id=12001, obtained=1)

    def login(self):
        from codec import encode
        from schema_default import default_obj
        import generator
        self.source('generator.py')
        tree = ast.parse((self.server / 'generator.py').read_text(encoding='utf-8'))
        commands = set()
        for func in tree.body:
            if isinstance(func, ast.FunctionDef) and func.name == 'gen_payload':
                for cmp in ast.walk(func):
                    if isinstance(cmp, ast.Compare) and isinstance(cmp.left, ast.Name) and cmp.left.id == 'cmd':
                        for expr in cmp.comparators:
                            commands.update(n.value for n in ast.walk(expr)
                                            if isinstance(n, ast.Constant) and type(n.value) is int)
        commands.update(map(int, re.findall(r'\bsc_(\d+)', self.source('db_schema.sql').read_text(encoding='utf-8'))))
        commands.update((10039, 12099, 12097, 28837, 68155, 40031, 40033, 40035, 48001, 11071))
        commands.discard(10201)
        # A configuration-derived protocol schedule, never captured login bytes.
        schedule = sorted(commands)[:191] + [10201]
        catalog = [dict(activity_id=aid, start_time=EPOCH, stop_time=2440962000,
                        state=1, theme=1, template=template, sub_activity_id_list=children)
                   for aid, template, children in ((3, 3, [180103]), (180103, 18, []),
                                                   (3705601, 56, []), (301701, 17, []))]
        stages = self.data('valid_chapter_stage_ids.json')
        residents = [dict(id=sid, clear_times=5, star_list=[1, 1, 1]) for sid in stages if sid >= 5000000]
        shop_cfg, shop_state = [], []
        for sid in (1, 2, 3, 4, 5):
            rows = [r for r in self.shop_rows.values() if r['shop_id'] == sid]
            shop_cfg.append(dict(shop_id=sid, goods_list=[default_obj('goods_cfg', dict(
                r['detail_json'], goods_id=r['goods_id'])) for r in rows]))
            shop_state.append(dict(shop_id=sid, goods_list=[dict(goods_id=r['goods_id'], buy_times=0,
                                                               next_refresh_timestamp=0) for r in rows]))
        social = self.data('social_catalog.json')
        for key, entry in social['prepose'].items():
            self.insert('guild_boss_prebuff', club_id=0, level_id=int(key),
                        stage_id=entry['stage_id'], score=0, update_ts=EPOCH)
        special = {
            11001: {'activity_list': catalog},
            24009: {'user_chapter_list': residents},
            20007: {'shop_item_cfg_list': shop_cfg},
            20009: {'shop_item_list': shop_state},
            32009: {'game_icon': [{'id': 12001, 'lasted_time': 0, 'obtain_time': 0}]},
            52021: {'id_list': [2109401, 2109403, 2109402]},
            33001: {'info': {'boss_tempalte_id': social['bosses']['1']['boss_list'][0],
                            'difficulty': 3, 'total_damage': 999},
                    'pre_info': [dict(level_id=int(k), stage_id=v['stage_id'], score=777)
                                 for k, v in social['prepose'].items()]},
        }
        for seq, cmd in enumerate(schedule, 1):
            obj = default_obj('sc_' + str(cmd), special.get(cmd))
            payload = encode('sc_' + str(cmd), obj)
            if len(payload) > 1024:
                payload = zlib.compress(payload)
            header = generator.build_frame(cmd, payload)[:11]
            self.insert('login_push', seq=seq, cmd=cmd, header=header, payload=payload,
                        dynamic=int(cmd == 52021), note='Synthetic protocol fixture', payload_json=obj)
        for uid in (UID, OTHER):
            for entry in catalog:
                self.insert('activity', uid=uid, **entry)

    def finish(self):
        self.db.execute('PRAGMA user_version=' + str(VERSION))
        tables = [r['name'] for r in self.db.query("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        counts = {name: self.db.query('SELECT COUNT(*) AS n FROM "' + name + '"')[0]['n'] for name in tables}
        logical = hashlib.sha256()
        for name in tables:
            logical.update((name + '\n').encode('utf-8'))
            records = [json.dumps({key: {'blob_hex': value.hex()} if isinstance(value, bytes) else value
                                   for key, value in row.items()}, ensure_ascii=False, sort_keys=True)
                       for row in self.db.query('SELECT * FROM "' + name + '"')]
            for record in sorted(records):
                logical.update((record + '\n').encode('utf-8'))
        if self.db.query('PRAGMA integrity_check')[0]['integrity_check'] != 'ok':
            raise RuntimeError('Generated database failed integrity_check')
        self.db.execute('VACUUM')
        self.db.close()
        manifest = dict(format_version=VERSION, synthetic=True, epoch=EPOCH,
                        fixture_uids=[UID, OTHER, ADMIN], database_sha256=digest(self.output),
                        logical_sha256=logical.hexdigest(),
                        generator_sha256=digest(__file__),
                        table_counts=counts, inputs=dict(sorted(self.inputs.items())))
        self.output.with_name(self.output.name + '.manifest.json').write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='New SQLite fixture file (never overwritten)')
    parser.add_argument('--server', type=Path, default=SERVER, help='Public v5_server source directory')
    parser.add_argument('--build', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.build:
        manifest = args.build.with_name(args.build.name + '.manifest.json')
        if any(path.exists() or path.is_symlink() for path in (args.build, manifest)):
            parser.error('Destination already exists: ' + str(args.build))
        with patch('time.time', return_value=EPOCH):
            builder = SeedBuilder(args.server.resolve(), args.build.resolve())
            with builder.db.transaction():
                for step in (builder.catalog, builder.growth, builder.tasks, builder.pools,
                             builder.shops, builder.players, builder.login):
                    step()
            builder.finish()
    elif args.output:
        result = generate_seed(args.output, args.server)
        print(json.dumps(dict(database=str(args.output), sha256=result['database_sha256'],
                              tables=len(result['table_counts'])), ensure_ascii=False))
    else:
        parser.error('--output is required')


if __name__ == '__main__':
    main()
