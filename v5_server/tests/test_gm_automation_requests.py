"""Personal GM authority, live player control, and scheduled gameplay updates."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import unittest
from unittest.mock import patch

from test_seed import get_seed_path

SEED = get_seed_path()
WORK = tempfile.TemporaryDirectory(prefix='ag-gm-automation-')
BOOT = Path(WORK.name) / 'bootstrap.db'
shutil.copyfile(SEED, BOOT)
os.environ['ACCOUNT_DB_PATH'] = str(BOOT)

from flask import Flask
from account_db import AccountDB
from auth_service import AuthError, get_service
import auth_api
from codec import decode
from core import Connection
from draw_service import DrawService
import game_cycles
import generator
import gm_actions
import gm_api
from gm_reader import GMReader
from shop_service import ShopService
import shop_restock
import server_net
import sub_gm_api
import task_listener

OWNER = 2174928301
NOW = 1793678400
PASSWORD = 'OnlyLocalTests59'


class AutomationRequests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.folder = tempfile.TemporaryDirectory(dir=WORK.name)
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'player.db'
        shutil.copyfile(SEED, self.path)
        self.db = AccountDB(str(self.path))
        self.addCleanup(self.db.close)
        self.stack.enter_context(patch('time.time', return_value=NOW))
        self.stack.enter_context(patch('res_version_manager.get_current_version', return_value='321'))
        self.stack.enter_context(patch.object(DrawService, '_instance', None))
        self.stack.enter_context(patch.object(ShopService, '_instance', None))
        self.stack.enter_context(patch.dict(server_net._online_game_conns, {}, clear=True))
        self.stack.enter_context(patch.dict(server_net._game_conn_controls, {}, clear=True))
        self.stack.enter_context(patch('gm_actions.sync', return_value={'clients': 0, 'requires_relogin': True}))
        self.stack.enter_context(patch('gm_api.get_db', return_value=self.db))
        self.stack.enter_context(patch('gm_api.get_reader', side_effect=lambda: GMReader(str(self.path))))
        self.auth = get_service(self.db)
        self.owner = self.auth.create('900011', PASSWORD, self.auth.bootstrap_token(OWNER))
        self.player = self.auth.create('900012', PASSWORD)
        self.uid = self.player['uid']
        self.db.upsert('accumulate_sign', self.uid, {'version': 2, 'open_sign': 0, 'login_days': 0}, keys=('uid',))
        self.owner_token = self.auth.issue(OWNER, 'web')
        self.token = self.auth.issue(self.uid, 'web')
        self.app = Flask(__name__)
        auth_api.install(self.app, self.db)
        self.app.register_blueprint(gm_api.gm_bp, url_prefix='/api/gm')
        self.client = self.app.test_client()
        self.owner_state = self.saved(OWNER)

    def tearDown(self):
        self.assertEqual(self.saved(OWNER), self.owner_state)
        self.assertEqual(self.db.query('PRAGMA integrity_check')[0]['integrity_check'], 'ok')

    def saved(self, uid):
        return {table: self.db.query('SELECT * FROM ' + table + ' WHERE uid=? ORDER BY rowid', (uid,))
                for table in ('users', 'hero', 'equip', 'servant', 'currency', 'material', 'chapter')}

    def level(self, value):
        self.db.execute('UPDATE users SET level=? WHERE uid=?', (value, self.uid))

    def request(self, path, body=None, owner=False):
        headers = {'Authorization': 'Bearer ' + (self.owner_token if owner else self.token)}
        return self.client.get(path, headers=headers) if body is None else self.client.post(path, json=body, headers=headers)

    def personal(self, name, body):
        return self.request('/api/sub-gm/' + name, body)

    def balance(self, iid=1):
        rows = self.db.query('SELECT num FROM currency WHERE uid=? AND id=?', (self.uid, iid))
        return int(rows[0]['num']) if rows else 0

    def connection(self, uid):
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        conn = Connection(uid=uid)
        conn.initialized = True
        def close():
            left.shutdown(socket.SHUT_RDWR)
            left.close()
        server_net.bind_game_conn(conn, close, 'local-test')
        server_net.register_game_conn(conn, uid)
        self.addCleanup(server_net.unregister_game_conn, conn)
        return conn, right

    def test_regular_account_has_own_profile_but_cannot_manage_other_players(self):
        me = self.request('/api/sub-gm/me').get_json()['data']
        self.assertEqual(me['game']['uid'], self.uid)
        self.assertFalse(any(value['allowed'] for value in me['permissions'].values()))
        for path, body in [('/api/gm/accounts', None), ('/api/gm/accounts/kick', {'uid': OWNER}),
                           ('/api/gm/sub-gm/policy', {'currency_level': 1})]:
            self.assertEqual(self.request(path, body).status_code, 403)

    def test_level_is_rechecked_with_existing_web_session_and_reward_is_personal(self):
        before = self.balance()
        self.assertEqual(self.personal('grant', {'item_id': 1, 'count': 7}).status_code, 403)
        self.level(10)
        response = self.personal('grant', {'item_id': 1, 'count': 7})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()['data']['uid'], self.uid)
        self.assertEqual(self.balance(), before + 7)
        self.assertEqual(self.db.query('SELECT uid,amount FROM gm_self_audit'), [{'uid': self.uid, 'amount': 7}])

    def test_target_spoofing_exp_and_invalid_amounts_do_not_change_assets(self):
        self.level(60)
        before = self.saved(self.uid)
        for body in [{'uid': OWNER, 'item_id': 1, 'count': 1}, {'item_id': 12, 'count': 1000000},
                     *[{'item_id': 1, 'count': amount} for amount in (0, -1, True, 1.5, 1000001)]]:
            self.assertEqual(self.personal('grant', body).status_code, 400)
        self.assertEqual(self.saved(self.uid), before)
        self.assertFalse(self.db.query('SELECT * FROM gm_self_audit'))

    def test_material_and_hero_permissions_follow_thresholds(self):
        self.level(19)
        self.assertEqual(self.personal('grant', {'item_id': 40103, 'count': 3}).status_code, 403)
        self.level(20)
        response = self.personal('grant', {'item_id': 40103, 'count': 3})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.personal('hero/unlock', {'hero_id': 1039}).status_code, 403)
        self.level(40)
        response = self.personal('hero/unlock', {'hero_id': 1039})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.db.query('SELECT unlock FROM hero WHERE uid=? AND id=1039', (self.uid,))[0]['unlock'], 1)

    def test_permission_edit_takes_effect_and_failed_grant_rolls_back_audit(self):
        self.level(10)
        response = self.request('/api/gm/sub-gm/policy', {'currency_level': 15}, owner=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.personal('grant', {'item_id': 1, 'count': 1}).status_code, 403)
        self.level(15)
        before = self.balance()
        with patch('gm_actions.asset_frames', side_effect=ValueError('injected encoding failure')):
            self.assertEqual(self.personal('grant', {'item_id': 1, 'count': 1}).status_code, 400)
        self.assertEqual(self.balance(), before)
        self.assertFalse(self.db.query('SELECT * FROM gm_self_audit'))

    def test_game_kick_closes_socket_revokes_game_tokens_and_retains_web_login(self):
        conn, peer = self.connection(self.uid)
        sdk = self.auth.issue(self.uid, 'sdk')
        self.assertTrue(server_net.game_presence()[self.uid]['online'])
        result = self.request('/api/gm/accounts/kick', {'uid': self.uid}, owner=True)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_json()['data']['closed_connections'], 1)
        self.assertEqual(peer.recv(1), b'')
        self.assertFalse(conn.initialized)
        self.assertNotIn(self.uid, server_net.get_online_uids())
        with self.assertRaises(AuthError): self.auth.verify(sdk, {'sdk'})
        self.assertEqual(self.auth.verify(self.token, {'web'})['uid'], self.uid)

    def test_kick_own_admin_game_keeps_control_panel_session(self):
        _, peer = self.connection(OWNER)
        response = self.request('/api/gm/accounts/kick', {'uid': OWNER}, owner=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(peer.recv(1), b'')
        self.assertEqual(self.request('/api/gm/accounts', owner=True).status_code, 200)

    def test_ban_disconnects_and_revokes_web_and_game_sessions(self):
        _, peer = self.connection(self.uid)
        response = self.request('/api/gm/accounts/disabled', {'uid': self.uid, 'disabled': True}, owner=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(peer.recv(1), b'')
        self.assertEqual(self.request('/api/sub-gm/me').status_code, 401)
        self.assertEqual(self.request('/api/gm/accounts/disabled', {'uid': self.uid, 'disabled': False}, owner=True).status_code, 200)
        self.assertEqual(self.auth.login('900012', PASSWORD)['uid'], self.uid)

    def test_live_presence_ignores_stale_database_online_watermark(self):
        self.db.execute('UPDATE user_timer_watermark SET is_online=1 WHERE uid=?', (self.uid,))
        accounts = self.request('/api/gm/accounts', owner=True).get_json()['data']
        self.assertFalse(next(row for row in accounts if row['uid'] == self.uid)['online'])
        conn, _ = self.connection(self.uid)
        conn.uid = OWNER
        server_net.register_game_conn(conn, OWNER)
        self.assertNotIn(self.uid, server_net.game_presence())
        self.assertIn(OWNER, server_net.game_presence())

    def random_config(self):
        cfg = game_cycles.get_settings(self.db, NOW)['rotation']
        return game_cycles.save_settings(self.db, {'rotation': {
            'enabled': True, 'mode': 'random', 'random_group_count': 1,
            'start_ts': NOW, 'duration_days': 7,
            'random_pool_ids': [4050301, 4050601, 4080301, 4080601]}}, NOW)['rotation']

    def test_random_rotation_is_stable_then_changes_group_and_keeps_permanent(self):
        cfg = self.random_config()
        first = game_cycles.apply_rotation(self.db, NOW)
        self.assertEqual(game_cycles.apply_rotation(self.db, NOW + 100)['pool_ids'], first['pool_ids'])
        second = game_cycles.apply_rotation(self.db, NOW + 7 * 86400)
        self.assertNotEqual(second['random_group_keys'], first['random_group_keys'])
        self.assertTrue(set(cfg['fixed_pool_ids']) <= set(second['pool_ids']))
        self.assertEqual(len(set(second['pool_ids']) - set(cfg['fixed_pool_ids'])), 2)
        self.assertEqual(game_cycles.apply_rotation(self.db, NOW)['pool_ids'], second['pool_ids'])

    def test_random_choice_survives_database_reopen_and_excludes_selectable(self):
        cfg = self.random_config()
        first = game_cycles.apply_rotation(self.db, NOW)
        reopened = AccountDB(str(self.path))
        try:
            self.assertEqual(game_cycles.rotation_status(reopened, NOW + 1)['random_group_keys'], first['random_group_keys'])
        finally: reopened.close()
        candidates = game_cycles.random_rotation_groups(self.db, {**cfg, 'random_pool_ids': []})
        ids = {pid for group in candidates for pid in group['pool_ids']}
        self.assertFalse(ids & {10000, 10001, 10002, 4080101, 5000303, 5030301, 5030601})

    def test_custom_restock_obeys_time_keeps_permanent_and_does_not_replay(self):
        service = ShopService.get_instance(db=self.db)
        cfg = shop_restock.get_settings(self.db, NOW)
        status = shop_restock.save_settings(self.db, {'time': '07:30', 'interval_days': 1, 'shop_ids': [4]}, NOW)
        due = status['next_restock_ts']
        goods = self.db.query('SELECT goods_id,refresh_cycle FROM shop_goods WHERE shop_id=4')
        daily = next(row['goods_id'] for row in goods if row['refresh_cycle'] == 4)
        permanent = 9999401
        fixture = self.db.query('SELECT * FROM shop_goods WHERE goods_id=?', (daily,))[0]
        fixture.update(goods_id=permanent, refresh_cycle=1, name='测试永久商品')
        columns = list(fixture)
        self.db.execute('INSERT INTO shop_goods(' + ','.join(columns) + ') VALUES(' + ','.join('?' for _ in columns) + ')', tuple(fixture.values()))
        for gid in (daily, permanent):
            self.db.upsert('store_purchase', self.uid, {'shop_id': 4, 'goods_id': gid, 'buy_times': 1,
                           'next_refresh_timestamp': due - 3600}, keys=('uid','shop_id','goods_id'))
        self.assertFalse(shop_restock.apply_due(self.db, due - 1)['changed'])
        service.on_daily_reset_5am(gm_actions.context(self.db), self.uid, now_ts=due - 3600)
        self.assertEqual(len(self.db.query('SELECT * FROM store_purchase WHERE uid=? AND goods_id=?', (self.uid, daily))), 1)
        result = shop_restock.apply_due(self.db, due)
        self.assertTrue(result['changed'])
        self.assertFalse(self.db.query('SELECT * FROM store_purchase WHERE uid=? AND goods_id=?', (self.uid, daily)))
        self.assertTrue(self.db.query('SELECT * FROM store_purchase WHERE uid=? AND goods_id=?', (self.uid, permanent)))
        self.assertFalse(shop_restock.apply_due(self.db, due + 1)['changed'])
        self.assertFalse(shop_restock.apply_due(self.db, NOW)['changed'])

    def test_restock_settings_do_not_reset_stock_and_payload_uses_custom_timestamp(self):
        status = shop_restock.get_status(self.db, NOW)
        gid = self.db.query('SELECT goods_id FROM shop_goods WHERE shop_id=4 AND refresh_cycle=4 LIMIT 1')[0]['goods_id']
        self.db.upsert('store_purchase', self.uid, {'shop_id':4,'goods_id':gid,'buy_times':1,
                       'next_refresh_timestamp':status['next_restock_ts']}, keys=('uid','shop_id','goods_id'))
        saved = shop_restock.save_settings(self.db, {'time':'09:00','shop_ids':[4]}, NOW)
        self.assertEqual(self.db.query('SELECT buy_times FROM store_purchase WHERE uid=? AND goods_id=?',(self.uid,gid))[0]['buy_times'],1)
        data = ShopService.get_instance(db=self.db).get_single_shop_payload(self.uid, 4)
        item = next(row for row in data['changed_shop_info']['goods_list'] if row['goods_id']==gid)
        self.assertEqual(item['next_refresh_timestamp'],saved['next_restock_ts'])

    def test_invalid_schedule_is_rejected_without_changing_settings(self):
        before = shop_restock.get_settings(self.db, NOW)
        for incoming in ({'time':'25:00'},{'interval_days':0},{'shop_ids':[4,4]},{'enabled':1}):
            with self.assertRaises(ValueError):shop_restock.save_settings(self.db,incoming,NOW)
            self.assertEqual(shop_restock.get_settings(self.db,NOW),before)

    def test_equipment_requires_level_sixty_and_adds_only_personal_instance(self):
        from inventory_service import _get_equip_map
        prefab = next(int(value) for value in _get_equip_map()
                      if self.db.query('SELECT id FROM item_catalog WHERE id=?', (int(value),)))
        self.level(59)
        self.assertEqual(self.personal('grant', {'item_id': prefab, 'count': 1}).status_code, 403)
        self.level(60)
        before = len(self.db.query('SELECT id FROM equip WHERE uid=?', (self.uid,)))
        response = self.personal('grant', {'item_id': prefab, 'count': 1})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(len(self.db.query('SELECT id FROM equip WHERE uid=?', (self.uid,))), before + 1)

    def test_personal_hero_graduate_updates_owned_hero(self):
        self.level(40)
        response = self.personal('hero/graduate', {'hero_id': 1084})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.db.query('SELECT level FROM hero WHERE uid=? AND id=1084', (self.uid,))[0]['level'], 80)

    def test_shop_outside_custom_scope_retains_native_daily_countdown(self):
        from lazy_timer import get_daily_5am_ts
        shop_restock.save_settings(self.db, {'shop_ids': [4]}, NOW)
        payload = ShopService.get_instance(db=self.db).get_single_shop_payload(self.uid, 2)
        goods = payload['changed_shop_info']['goods_list']
        self.assertEqual(len(goods), 13)
        self.assertTrue(all(item['next_refresh_timestamp'] == get_daily_5am_ts(NOW) + 86400 for item in goods))

    def test_failed_restock_rolls_back_purchase_reset_and_cycle_marker(self):
        service = ShopService.get_instance(db=self.db)
        service.roll_daily_shop_goods(self.uid)
        gid = self.db.query('SELECT goods_id FROM shop_goods WHERE refresh_cycle=4 LIMIT 1')[0]['goods_id']
        sid = self.db.query('SELECT shop_id FROM shop_goods WHERE goods_id=?', (gid,))[0]['shop_id']
        due = shop_restock.get_status(self.db, NOW)['next_restock_ts']
        self.db.upsert('store_purchase', self.uid, {'shop_id': sid, 'goods_id': gid, 'buy_times': 1,
                       'next_refresh_timestamp': due}, keys=('uid','shop_id','goods_id'))
        before = self.db.query('SELECT * FROM store_purchase WHERE uid=?', (self.uid,))
        state = shop_restock.state(self.db)
        with patch.object(service, 'roll_daily_shop_goods', side_effect=ValueError('injected shelf failure')):
            with self.assertRaises(ValueError): shop_restock.apply_due(self.db, due)
        self.assertEqual(self.db.query('SELECT * FROM store_purchase WHERE uid=?', (self.uid,)), before)
        self.assertEqual(shop_restock.state(self.db), state)

    def test_non_milestone_legacy_popup_flag_is_suppressed_without_losing_rewards(self):
        self.db.execute('UPDATE accumulate_sign SET version=2,login_days=132,open_sign=1 WHERE uid=?',(self.uid,))
        awards = self.db.query('SELECT * FROM accumulate_sign_award WHERE uid=?',(self.uid,))
        data = decode(generator.gen_payload(17027,uid=self.uid,db=self.db),'sc_17027')
        self.assertEqual(data['open_sign'],0)
        self.assertEqual(data['login_days'],132)
        self.assertEqual(self.db.query('SELECT * FROM accumulate_sign_award WHERE uid=?',(self.uid,)),awards)

    def test_login_milestone_pops_once_and_closing_view_prevents_sign_repetition(self):
        self.db.execute('UPDATE accumulate_sign SET version=2,login_days=159,open_sign=0 WHERE uid=?',(self.uid,))
        self.db.execute('UPDATE game_user SET last_login_task_cycle_ts=0 WHERE uid=?',(self.uid,))
        ctx = gm_actions.context(self.db);ctx.uid=self.uid
        task_listener.on_user_login(ctx,self.uid,is_first_login=True,now_ts=NOW)
        data=decode(generator.gen_payload(17027,uid=self.uid,db=self.db),'sc_17027')
        self.assertEqual((data['login_days'],data['open_sign']),(160,1))
        self.db.execute('UPDATE accumulate_sign SET open_sign=0 WHERE uid=?',(self.uid,))
        task_listener.on_user_login(ctx,self.uid,is_first_login=True,now_ts=NOW+1)
        data=decode(generator.gen_payload(17027,uid=self.uid,db=self.db),'sc_17027')
        self.assertEqual((data['login_days'],data['open_sign']),(160,0))
