"""A01/A32/A35/A36/A37/A93/A248-A251/A253 on real packets and disposable saves."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
import datetime
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_seed import get_seed_path

SEED = get_seed_path()
SANDBOX = tempfile.TemporaryDirectory(prefix='ag-next-batch-tests-')
BOOT = Path(SANDBOX.name) / 'bootstrap.db'
shutil.copyfile(SEED, BOOT)
os.environ['ACCOUNT_DB_PATH'] = str(BOOT)

from account_db import AccountDB
from codec import encode, decode
from core import Core, Operation, OperationError
import core_verification_mode4_service as mode4
import event_bus
import generator
from gm_actions import context
from hero_service import HeroService
from inventory_service import InventoryService
from lazy_timer import get_daily_5am_ts
from middleware import CSRequest, DownFrame
import newbie_service
import operations
import regression_service
from shop_service import ShopService
import shop_pricing
import weekly_challenge_service

UID = 2174928301
OTHER = 264084870
HERO = 1011
GOODS = 2001001  # The real 800-gold sample, distinct from the old free SKU.
NOW = int(datetime.datetime(2026, 10, 28, 12).timestamp())
DAY = 86400
TABLES = ('users', 'currency', 'material', 'hero', 'hero_piece', 'servant',
          'store_purchase', 'user_daily_shop', 'newbie_activity', 'newbie_pt_reward',
          'newbie_level_reward', 'regression', 'task', 'claim_ledger',
          'core_verification_cl_progress', 'core_verification_cl_task_claimed')


class NextBatchTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=SANDBOX.name)
        self.addCleanup(self.folder.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('time.time', return_value=NOW))
        self.stack.enter_context(patch.object(event_bus.EventBus, '_handlers', {}))
        self.events = self.stack.enter_context(patch('event_bus.bus.emit'))
        self.stack.enter_context(patch.object(HeroService, '_instance', None))
        for field in ('_instance', '_SHOP_BASE_TEMPLATE', '_SHOP_CFG_BASE_TEMPLATE', '_DAILY_POOL_CACHE'):
            self.stack.enter_context(patch.object(ShopService, field, None))
        self.path = Path(self.folder.name) / 'player.db'
        shutil.copyfile(SEED, self.path)
        self.db = AccountDB(str(self.path))
        self.addCleanup(self.db.close)
        self.ctx = context(self.db)
        self.ctx.uid = UID
        self.ctx.log = lambda *args, **kwargs: None
        self.shop = ShopService.get_instance(self.db)
        self.engine = Core()
        self.curve = {row['level']: row['hero_exp_need']
                      for row in self.db.query('SELECT level,hero_exp_need FROM game_level_setting')}
        self.db.execute('UPDATE users SET level=100 WHERE uid=?', (UID,))
        for iid in (1, 2, 3, 30, 31, 32):
            self.db.set_currency(UID, iid, 1000000)
        self.db.set_currency(UID, 4, 0)
        for iid in (40101, 40104, 40201, 40202, 40203, 40301,
                    40411, 40412, 40413, 40414, 40415, 40501, 40502, 40503):
            self.db.set_material(UID, iid, 10000)
        self.hero(level=20, exp=self.exp_at(20), break_level=0, weapon_exp=0, weapon_break=0, unlock=1)
        self.db.execute('DELETE FROM store_purchase WHERE uid=? AND goods_id=?', (UID, GOODS))
        self.db.execute('''UPDATE newbie_activity SET trigger_time=?,completed_time=0,
            now_sign_times=0,last_sign_ts=0,version_id=3 WHERE uid=?''', (NOW - DAY, UID))
        self.db.execute('DELETE FROM newbie_pt_reward WHERE uid=?', (UID,))
        self.db.execute('DELETE FROM newbie_level_reward WHERE uid=?', (UID,))
        self.regression()
        self.other = self.state(OTHER)

    def exp_at(self, level):
        return sum(self.curve[i] for i in range(1, level))

    def hero(self, **fields):
        if fields:
            names = ','.join(key + '=?' for key in fields)
            self.db.execute('UPDATE hero SET ' + names + ' WHERE uid=? AND id=?', (*fields.values(), UID, HERO))
        return self.db.query('SELECT * FROM hero WHERE uid=? AND id=?', (UID, HERO))[0]

    def regression(self, **changes):
        fields = {'trigger_time': NOW - 2 * DAY, 'end_ts': NOW + 28 * DAY,
                  'return_vs_id': 110, 'state_version': 1, 'receive_sign_index': 3,
                  'received_sign_list': '[]', 'find_time': 1, 'find_task_claimed_ts': 0}
        fields.update(changes)
        names = ','.join(key + '=?' for key in fields)
        self.db.execute('UPDATE regression SET ' + names + ' WHERE uid=?', (*fields.values(), UID))
        return self.db.query('SELECT * FROM regression WHERE uid=?', (UID,))[0]

    def state(self, uid=UID):
        return {table: sorted(self.db.query('SELECT * FROM ' + table + ' WHERE uid=?', (uid,)),
                              key=lambda row: json.dumps(row, sort_keys=True)) for table in TABLES}

    def balance(self, item):
        return InventoryService.get_item_balance(self.db, UID, item)[1]

    def request(self, cmd, data, *, frames=False):
        cls = next(value for value in vars(operations).values()
                   if isinstance(value, type) and issubclass(value, Operation) and value.cmd == cmd)
        self.ctx.reset_operation_state()
        req = CSRequest(cmd, encode('cs_' + str(cmd), data), uid=UID)
        response = self.engine._run_operation(cls(), req, self.ctx)
        replies = [frame for frame in response.frames if frame['sc'] == cls.sc]
        self.assertEqual(len(replies), 1)
        self.assertEqual(self.state(OTHER), self.other)
        reply = decode(replies[0]['payload'], 'sc_' + str(cls.sc))
        return (reply, response.frames) if frames else reply

    def rejected(self, cmd, data, code=None):
        before = self.state()
        reply = self.request(cmd, data)
        if code is None:
            self.assertNotEqual(reply['result'], 0, reply)
        else:
            self.assertEqual(reply['result'], code, reply)
        self.assertEqual(self.state(), before)
        self.assertFalse(self.ctx.touched_items)
        return reply

    def shop_request(self, costs=None, *, count=1, buy_type=0):
        goods = {'buy_id': GOODS, 'buy_num': count, 'buy_type': buy_type}
        if costs is not None:
            goods['cost_items'] = costs
        return {'shop_id': 2, 'buy_goods_list': [goods]}

    def shop_config(self, **changes):
        row = self.db.query('SELECT * FROM shop_goods WHERE goods_id=?', (GOODS,))[0]
        detail = json.loads(row['detail_json'])
        detail.update(changes.pop('detail', {}))
        for key, value in changes.items():
            self.db.execute('UPDATE shop_goods SET ' + key + '=? WHERE goods_id=?', (value, GOODS))
        self.db.execute('UPDATE shop_goods SET detail_json=? WHERE goods_id=?', (json.dumps(detail), GOODS))

    def prove_stage(self, stage=3092091, activity=4243501, *, owner=UID, mode=4, score=0):
        self.db.record_core_verification_cl_stage_clear(owner, activity, mode, stage, score=score)

    def test_shop_zero_under_over_and_wrong_currency_leave_no_purchase(self):
        for cid, amount in ((2, 0), (2, 799), (2, 801), (1, 800)):
            with self.subTest(cid=cid, amount=amount):
                self.rejected(20012, self.shop_request([{'id': cid, 'num': amount}]), 2)

    def test_shop_real_gold_price_is_multiplied_and_duplicate_cost_entries_aggregate(self):
        gold, tickets = self.balance(2), self.balance(40102)
        reply = self.request(20012, self.shop_request([{'id': 2, 'num': 800}, {'id': 2, 'num': 800}], count=2))
        self.assertEqual(reply['result'], 0)
        self.assertEqual(self.balance(2), gold - 1600)
        self.assertEqual(self.balance(40102), tickets + 2)
        self.assertEqual(reply['cost_items'], [{'id': 2, 'num': 1600}])
        self.assertEqual(self.db.query('SELECT buy_times FROM store_purchase WHERE uid=? AND goods_id=?',
                                     (UID, GOODS))[0]['buy_times'], 2)

    def test_shop_without_declared_costs_still_charges_the_configured_price(self):
        before = self.balance(2)
        self.assertEqual(self.request(20012, self.shop_request())['result'], 0)
        self.assertEqual(self.balance(2), before - 800)

    def test_shop_zero_count_secondary_without_config_and_wrong_shop_are_rejected(self):
        self.rejected(20012, self.shop_request(count=0), 2)
        self.rejected(20012, self.shop_request(buy_type=1), 2)
        data = self.shop_request([{'id': 2, 'num': 800}])
        data['shop_id'] = 3
        self.rejected(20012, data, 406)

    def test_shop_secondary_currency_is_not_a_discount_switch(self):
        self.shop_config(cost=100, detail={'cost_id_2': 3, 'cost_2': 20})
        self.rejected(20012, self.shop_request([{'id': 2, 'num': 100}], buy_type=1), 2)
        before = self.balance(3)
        self.assertEqual(self.request(20012, self.shop_request([{'id': 3, 'num': 20}], buy_type=1))['result'], 0)
        self.assertEqual(self.balance(3), before - 20)

    def test_shop_discount_currency_and_exact_close_time(self):
        close = NOW + 60
        self.shop_config(cost=100, cheap_cost=75, discount=75, detail={
            'cheap_cost_id': 3, 'is_limit_time_discount': 1, 'cheap_open_time': NOW - 60,
            'cheap_close_time': close})
        self.rejected(20012, self.shop_request([{'id': 2, 'num': 75}]), 2)
        with patch('time.time', return_value=close - 1):
            self.assertEqual(self.request(20012, self.shop_request([{'id': 3, 'num': 75}]))['result'], 0)
        with patch('time.time', return_value=close):
            self.rejected(20012, self.shop_request([{'id': 3, 'num': 75}]), 2)
            self.assertEqual(self.request(20012, self.shop_request([{'id': 2, 'num': 100}]))['result'], 0)

    def test_shop_global_discount_uses_client_floor_rounding(self):
        rules = deepcopy(shop_pricing.load_rules('shop_price_rules.json'))
        rules['shops']['2'].update(discount=70, is_limit_buy_discount=1,
                                   cheap_open_time=NOW - 10, cheap_close_time=NOW + 10)
        self.shop_config(cost=101)
        with patch('shop_pricing.load_rules', return_value=rules):
            self.assertEqual(self.request(20012, self.shop_request([{'id': 2, 'num': 70}]))['result'], 0)

    def test_shop_mixed_payment_uses_android_bucket_and_reports_real_costs(self):
        self.shop_config(cost=100, cost_type=2, cost_id=31)
        self.ctx.client_platform = 'android'
        for cid, amount in ((1, 20), (32, 30), (31, 60), (30, 200)):
            self.db.set_currency(UID, cid, amount)
        reply = self.request(20012, self.shop_request([{'id': 1, 'num': 20}, {'id': 32, 'num': 80}]))
        self.assertEqual(reply['result'], 0)
        self.assertEqual({cid: self.balance(cid) for cid in (1, 32, 31, 30)}, {1: 0, 32: 0, 31: 10, 30: 200})
        self.assertEqual({item['id']: item['num'] for item in reply['cost_items']}, {1: 20, 32: 30, 31: 50})

    def test_shop_mixed_payment_supports_ios_and_excludes_android_funds(self):
        self.shop_config(cost=100, cost_type=2, cost_id=31)
        self.ctx.client_platform = 'ios'
        for cid, amount in ((1, 20), (32, 30), (30, 60), (31, 200)):
            self.db.set_currency(UID, cid, amount)
        reply = self.request(20012, self.shop_request([{'id': 1, 'num': 20}, {'id': 32, 'num': 80}]))
        self.assertEqual(reply['result'], 0)
        self.assertEqual(self.balance(30), 10)
        self.assertEqual(self.balance(31), 200)

    def test_shop_mixed_wrong_split_cross_platform_and_insufficient_current_funds(self):
        self.shop_config(cost=100, cost_type=2, cost_id=31)
        self.ctx.client_platform = 'android'
        for cid, amount in ((1, 20), (32, 30), (31, 0), (30, 1000)):
            self.db.set_currency(UID, cid, amount)
        self.rejected(20012, self.shop_request([{'id': 1, 'num': 19}, {'id': 32, 'num': 81}]), 2)
        self.rejected(20012, self.shop_request([{'id': 1, 'num': 20}, {'id': 30, 'num': 80}]), 2)
        self.rejected(20012, self.shop_request([{'id': 1, 'num': 20}, {'id': 32, 'num': 80}]), 12)

    def test_shop_configured_free_pack_remains_free(self):
        self.db.execute('DELETE FROM store_purchase WHERE uid=? AND goods_id=5000007', (UID,))
        before = self.balance(2)
        reply = self.request(20012, {'shop_id': 5, 'buy_goods_list': [{'buy_id': 5000007, 'buy_num': 1}]})
        self.assertEqual(reply['result'], 0)
        self.assertFalse(reply.get('cost_items'))
        self.assertEqual(self.balance(2), before + 2000)

    def test_shop_late_invalid_item_rolls_back_inside_a_caught_outer_transaction(self):
        before = self.state()
        self.ctx.touched_items.add(1)
        prior = DownFrame(17025, b'prior')
        self.ctx.pending_frames.append(prior)
        with self.db.transaction():
            with self.assertRaises(OperationError):
                self.shop.buy_goods(self.ctx, UID, 2, [
                    {'buy_id': GOODS, 'buy_num': 1, 'cost_items': [{'id': 2, 'num': 800}]},
                    {'buy_id': GOODS, 'buy_num': 1, 'cost_items': [{'id': 2, 'num': 0}]}])
            self.assertEqual(self.state(), before)
        self.assertEqual(self.ctx.touched_items, {1})
        self.assertEqual(self.ctx.pending_frames, [prior])

    def test_newbie_sign_unlocks_configured_hero_once_with_receipt_before_activity_refresh(self):
        self.db.execute('UPDATE hero SET unlock=0 WHERE uid=? AND id=1037', (UID,))
        reply, frames = self.request(59012, {}, frames=True)
        self.assertEqual(reply, {'result': 0, 'reward_list': [{'id': 1037, 'num': 1}]})
        self.assertEqual(self.db.query('SELECT unlock FROM hero WHERE uid=? AND id=1037', (UID,))[0]['unlock'], 1)
        order = [frame['sc'] for frame in frames]
        self.assertLess(order.index(59013), order.index(59011))
        self.assertIn(14007, order)
        self.rejected(59012, {}, 2)

    def test_newbie_sign_game_day_opens_at_five_and_final_config_day_is_bounded(self):
        boundary = get_daily_5am_ts(NOW)
        self.db.execute('UPDATE newbie_activity SET now_sign_times=1,last_sign_ts=? WHERE uid=?', (boundary - 1, UID))
        reply = self.request(59012, {})
        self.assertEqual(reply['reward_list'], [{'id': 5, 'num': 10}])
        self.db.execute('UPDATE newbie_activity SET now_sign_times=14,last_sign_ts=0 WHERE uid=?', (UID,))
        self.rejected(59012, {}, 2)

    def test_newbie_missing_expired_future_and_unknown_version_states_cannot_award(self):
        for fields in ({'trigger_time': NOW + 1}, {'completed_time': NOW},
                       {'trigger_time': NOW - 60 * DAY}, {'version_id': 999}):
            with self.subTest(fields=fields):
                self.db.execute('UPDATE newbie_activity SET trigger_time=?,completed_time=0,version_id=3 WHERE uid=?',
                                (NOW - DAY, UID))
                self.db.execute('UPDATE newbie_activity SET ' + ','.join(key + '=?' for key in fields) + ' WHERE uid=?',
                                (*fields.values(), UID))
                self.rejected(59012, {})
                self.rejected(59004, {'level': 20})
                self.rejected(59014, {'id': 1})
        self.db.execute('DELETE FROM newbie_activity WHERE uid=?', (UID,))
        self.rejected(59012, {}, 406)

    def test_newbie_level_uses_configured_reward_and_permanent_claim_after_reopen(self):
        self.db.execute('UPDATE users SET level=19 WHERE uid=?', (UID,))
        self.rejected(59004, {'level': 20}, 2)
        self.rejected(59004, {'level': 9999}, 2)
        self.db.execute('UPDATE users SET level=20 WHERE uid=?', (UID,))
        before = self.balance(11084)
        reply = self.request(59004, {'level': 20})
        self.assertEqual(reply['reward_list'], [{'id': 11084, 'num': 12}])
        self.assertEqual(self.balance(11084), before + 12)
        saved = self.state()
        self.db.close()
        self.db = AccountDB(str(self.path))
        self.addCleanup(self.db.close)
        self.ctx.db = self.db
        self.assertEqual(self.state(), saved)
        self.rejected(59004, {'level': 20}, 2)

    def test_newbie_progress_counts_submitted_current_version_tasks(self):
        self.db.execute('UPDATE task SET complete_flag=0,claimed_ts=0 WHERE uid=? AND task_type IN (4,601,603)', (UID,))
        ids = [row['task_id'] for row in self.db.query('SELECT task_id FROM task_cfg WHERE task_type=603 ORDER BY task_id LIMIT 10')]
        for tid in ids:
            self.db.execute('''INSERT INTO task(uid,task_id,name,task_type,progress,complete_flag,expired_ts,claimed_ts)
                VALUES(?,?,'test',603,999,1,0,?) ON CONFLICT(uid,task_id) DO UPDATE SET
                task_type=603,progress=999,complete_flag=1,claimed_ts=excluded.claimed_ts''', (UID, tid, NOW))
        self.db.execute('UPDATE task SET complete_flag=0 WHERE uid=? AND task_id=?', (UID, ids[-1]))
        self.rejected(59014, {'id': 1}, 2)
        self.db.execute('UPDATE task SET complete_flag=1 WHERE uid=? AND task_id=?', (UID, ids[-1]))
        before = self.balance(11039)
        reply = self.request(59014, {'id': 1})
        self.assertEqual(reply['reward_list'], [{'id': 11039, 'num': 18}])
        self.assertEqual(self.balance(11039), before + 18)
        self.rejected(59014, {'id': 1}, 2)
        self.rejected(59014, {'id': 999999}, 2)

    def test_newbie_version_one_retains_its_fifteenth_sign_and_has_no_progress_tiers(self):
        self.db.execute('UPDATE newbie_activity SET version_id=1,now_sign_times=14,last_sign_ts=0 WHERE uid=?', (UID,))
        self.assertEqual(self.request(59012, {})['result'], 0)
        self.rejected(59012, {}, 2)
        self.rejected(59014, {'id': 1}, 2)

    def test_hero_break_requires_role_level_account_level_and_ownership(self):
        self.hero(level=19)
        self.rejected(14036, {'hero_id': HERO}, 2)
        self.hero(level=20)
        self.db.execute('UPDATE users SET level=19 WHERE uid=?', (UID,))
        self.rejected(14036, {'hero_id': HERO}, 2)
        self.db.execute('UPDATE users SET level=100 WHERE uid=?', (UID,))
        self.hero(unlock=0)
        self.rejected(14036, {'hero_id': HERO}, 406)
        self.rejected(14120, {'hero_id': HERO, 'level': 30, 'break_list': [1]}, 406)

    def test_hero_break_at_threshold_preserves_level_and_debits_one_configured_step(self):
        self.db.execute('UPDATE users SET level=20 WHERE uid=?', (UID,))
        gold, material = self.balance(2), self.balance(40411)
        reply = self.request(14036, {'hero_id': HERO})
        self.assertEqual(reply['result'], 0)
        self.assertEqual((self.hero()['level'], self.hero()['break_level']), (20, 1))
        self.assertEqual(self.balance(2), gold - 500)
        self.assertEqual(self.balance(40411), material - 16)

    def test_one_click_hero_cannot_break_without_exp_or_skip_account_gates(self):
        self.hero(level=1, exp=0)
        self.rejected(14120, {'hero_id': HERO, 'level': 30, 'break_list': [1]}, 2)
        self.hero(level=20, exp=self.exp_at(20))
        self.db.execute('UPDATE users SET level=20 WHERE uid=?', (UID,))
        self.rejected(14120, {'hero_id': HERO, 'level': 40, 'break_list': [1, 2],
                             'item_list': [{'id': 40104, 'num': 100}]}, 2)

    def test_one_click_hero_can_reach_break_gate_with_new_materials(self):
        self.hero(level=1, exp=0)
        gold = self.balance(2)
        reply = self.request(14120, {'hero_id': HERO, 'level': 30, 'break_list': [1],
                                    'item_list': [{'id': 40104, 'num': 100}]})
        self.assertEqual(reply['result'], 0)
        self.assertEqual((self.hero()['level'], self.hero()['exp'], self.hero()['break_level']),
                         (30, self.exp_at(30), 1))
        self.assertEqual(self.balance(2), gold - 500)

    def test_hero_missing_break_configuration_cannot_become_a_free_upgrade(self):
        self.db.execute('DELETE FROM hero_break_cfg WHERE race=(SELECT race FROM hero_cfg WHERE hero_id=?) AND break_times=0',
                        (HERO,))
        self.rejected(14036, {'hero_id': HERO}, 2)
        self.rejected(14120, {'hero_id': HERO, 'level': 30, 'break_list': [1],
                             'item_list': [{'id': 40104, 'num': 100}]}, 2)

    def test_weapon_rejects_non_exp_materials_in_normal_and_quick_entries(self):
        for cmd in (46016, 46034):
            with self.subTest(cmd=cmd):
                self.rejected(cmd, {'hero_id': HERO, 'material_list': [{'id': 40301, 'num': 1}]}, 2)

    def test_weapon_missing_and_locked_targets_cannot_spend_any_resources(self):
        for target in (999999999, HERO):
            if target == HERO:
                self.hero(unlock=0)
            for cmd, data in ((46016, {'material_list': [{'id': 40201, 'num': 1}]}),
                              (46018, {}), (46034, {'material_list': [{'id': 40201, 'num': 1}]})):
                with self.subTest(cmd=cmd, target=target):
                    self.rejected(cmd, {'hero_id': target, **data}, 406)

    def test_weapon_valid_material_cost_exp_and_hero_refresh_are_consistent(self):
        gold, material = self.balance(2), self.balance(40201)
        reply, frames = self.request(46016, {'hero_id': HERO,
                                           'material_list': [{'id': 40201, 'num': 2}]}, frames=True)
        self.assertEqual(reply['result'], 0)
        self.assertEqual(self.hero()['weapon_exp'], 200)
        self.assertEqual(self.balance(2), gold - 200)
        self.assertEqual(self.balance(40201), material - 2)
        order = [frame['sc'] for frame in frames]
        self.assertLess(order.index(46017), order.index(14007))

    def test_weapon_break_needs_exp_and_account_threshold_and_cannot_repeat(self):
        self.hero(weapon_exp=4799)
        self.rejected(46018, {'hero_id': HERO}, 2)
        self.hero(weapon_exp=4800)
        self.db.execute('UPDATE users SET level=29 WHERE uid=?', (UID,))
        self.rejected(46018, {'hero_id': HERO}, 2)
        self.db.execute('UPDATE users SET level=30 WHERE uid=?', (UID,))
        gold, material = self.balance(2), self.balance(40501)
        self.assertEqual(self.request(46018, {'hero_id': HERO})['result'], 0)
        self.assertEqual((self.hero()['weapon_exp'], self.hero()['weapon_break']), (4800, 1))
        self.assertEqual(self.balance(2), gold - 1000)
        self.assertEqual(self.balance(40501), material - 15)
        self.rejected(46018, {'hero_id': HERO}, 2)

    def test_weapon_quick_cannot_cross_multiple_gates_without_sufficient_exp(self):
        for data in ({'breakthrough_times': 1}, {'breakthrough_times': 5},
                     {'breakthrough_times': 2, 'material_list': [{'id': 40203, 'num': 6}]},
                     {'breakthrough_times': 1, 'target_level': 20,
                      'material_list': [{'id': 40201, 'num': 1}]}):
            with self.subTest(data=data):
                self.rejected(46034, {'hero_id': HERO, **data}, 2)

    def test_weapon_quick_checks_all_account_gates(self):
        self.db.execute('UPDATE users SET level=30 WHERE uid=?', (UID,))
        self.rejected(46034, {'hero_id': HERO, 'breakthrough_times': 2, 'target_level': 30,
                             'material_list': [{'id': 40203, 'num': 10}]}, 2)

    def test_weapon_quick_uses_client_curve_and_refunds_only_new_overflow(self):
        gold, top, mid = self.balance(2), self.balance(40203), self.balance(40202)
        reply = self.request(46034, {'hero_id': HERO, 'breakthrough_times': 2, 'target_level': 30,
                                    'material_list': [{'id': 40203, 'num': 10}]})
        self.assertEqual(reply['result'], 0)
        self.assertEqual((self.hero()['weapon_break'], self.hero()['weapon_exp']), (2, 9800))
        self.assertEqual(self.balance(2), gold - 12500)
        self.assertEqual(self.balance(40203), top - 10)
        self.assertEqual(self.balance(40202), mid + 1)
        self.assertEqual(reply['mat_list'], [{'id': 40202, 'num': 1}])

    def test_weapon_old_exp_above_cap_is_not_destroyed_or_refunded(self):
        self.hero(weapon_exp=4801)
        self.rejected(46016, {'hero_id': HERO, 'material_list': [{'id': 40201, 'num': 1}]}, 2)
        self.assertEqual(self.request(46018, {'hero_id': HERO})['result'], 0)
        self.assertEqual(self.hero()['weapon_exp'], 4801)
        self.rejected(46034, {'hero_id': HERO, 'target_level': 1,
                             'material_list': [{'id': 40201, 'num': 1}]}, 2)

    def test_weapon_partial_consumption_failure_rolls_back_for_a_direct_caller(self):
        self.db.set_currency(UID, 2, 0)
        before = self.state()
        with self.assertRaises(OperationError) as caught:
            HeroService.get_instance(self.db).enhance_weapon(self.ctx, UID, HERO, [{'id': 40201, 'num': 1}])
        self.assertEqual(caught.exception.code, 3)
        self.assertEqual(self.state(), before)
        self.assertFalse(self.ctx.touched_items)

    def test_regression_sign_saves_config_id_allows_backlog_and_rejects_duplicates(self):
        before = self.balance(38)
        first = self.request(62012, {'index': 1})
        self.assertEqual(first['item_list'], [{'id': 38, 'num': 1}])
        self.assertEqual(self.balance(38), before + 1)
        self.assertEqual(self.request(62012, {'index': 3})['item_list'], [{'id': 1, 'num': 100}])
        saved = self.db.query('SELECT * FROM regression WHERE uid=?', (UID,))[0]
        self.assertEqual(json.loads(saved['received_sign_list']), [23001, 23003])
        self.assertEqual(saved['receive_sign_index'], 1)
        self.rejected(62012, {'index': 1}, 2)
        self.rejected(62012, {'index': 4}, 2)
        self.rejected(62012, {'index': 9999}, 2)

    def test_regression_sign_missing_expired_and_low_level_instances_are_rejected(self):
        self.regression(end_ts=NOW)
        self.rejected(62012, {'index': 1}, 2)
        self.rejected(62016, {}, 2)
        self.regression()
        self.db.execute('UPDATE users SET level=29 WHERE uid=?', (UID,))
        self.rejected(62012, {'index': 1}, 2)
        self.db.execute('DELETE FROM regression WHERE uid=?', (UID,))
        self.rejected(62012, {'index': 1}, 2)
        self.rejected(62016, {}, 2)

    def test_regression_resource_find_decrements_quota_and_cannot_repeat(self):
        gold, fatigue = self.balance(2), self.balance(4)
        reply = self.request(62016, {})
        self.assertEqual(reply['item_list'], [{'id': 4, 'num': 240}, {'id': 2, 'num': 50000}])
        self.assertEqual(self.balance(2), gold + 50000)
        self.assertEqual(self.balance(4), fatigue + 240)
        self.assertEqual(self.db.query('SELECT find_time FROM regression WHERE uid=?', (UID,))[0]['find_time'], 0)
        self.rejected(62016, {}, 2)

    def test_regression_completed_current_task_credits_one_find_chance_only(self):
        self.regression(find_time=0)
        self.db.execute('''INSERT INTO task(uid,task_id,name,task_type,progress,complete_flag,expired_ts,claimed_ts)
            VALUES(?,60401,'test',210,500,1,0,?)''', (UID, NOW))
        self.assertEqual(self.request(62016, {})['result'], 0)
        self.assertEqual(regression_service.build_payload(self.db, UID)['find_time'], 0)
        self.db.execute('UPDATE task SET claimed_ts=? WHERE uid=? AND task_id=60401', (NOW + 1, UID))
        self.rejected(62016, {}, 2)

    def test_regression_unsubmitted_or_old_instance_task_cannot_create_quota(self):
        self.regression(find_time=0)
        self.db.execute('''INSERT INTO task(uid,task_id,name,task_type,progress,complete_flag,expired_ts,claimed_ts)
            VALUES(?,60401,'test',210,500,0,0,0)''', (UID,))
        self.rejected(62016, {}, 2)
        self.db.execute('UPDATE task SET complete_flag=1,claimed_ts=? WHERE uid=? AND task_id=60401',
                        (NOW - 3 * DAY, UID))
        self.rejected(62016, {}, 2)

    def test_regression_legacy_index_history_and_timestamp_quota_migrate_idempotently(self):
        self.regression(state_version=0, received_sign_list='[1,10002,23003,1]', find_time=NOW)
        first = decode(generator.gen_payload(62011, uid=UID, db=self.db), 'sc_62011')
        self.assertEqual(first['received_sign_list'], [23001, 23002, 23003])
        self.assertEqual(first['find_time'], 0)
        self.assertEqual(first['receive_sign_index'], 0)
        saved = self.state()
        self.assertEqual(decode(generator.gen_payload(62011, uid=UID, db=self.db), 'sc_62011'), first)
        self.assertEqual(self.state(), saved)
        self.rejected(62012, {'index': 1}, 2)
        self.rejected(62016, {}, 2)

    def test_regression_login_activation_requires_offline_proof_and_does_not_renew(self):
        self.regression(trigger_time=0, end_ts=0)
        self.db.update_timer_watermark(UID, last_logout_ts=NOW - 14 * DAY)
        first = regression_service.build_payload(self.db, UID, NOW)
        self.assertEqual(first['end_timestamps'], NOW + 30 * DAY)
        self.assertEqual(first['receive_sign_index'], 1)
        self.assertEqual(first['find_time'], 30)
        second = regression_service.build_payload(self.db, UID, NOW + DAY)
        self.assertEqual(second['end_timestamps'], first['end_timestamps'])
        self.assertEqual(second['receive_sign_index'], 2)
        expired = regression_service.build_payload(self.db, UID, NOW + 31 * DAY)
        self.assertEqual(expired['end_timestamps'], first['end_timestamps'])
        self.assertEqual(expired['receive_sign_index'], 0)
        self.assertEqual(expired['find_time'], 0)

    def test_regression_missing_offline_proof_cannot_synthesize_an_activity(self):
        self.db.execute('DELETE FROM regression WHERE uid=?', (UID,))
        self.db.update_timer_watermark(UID, last_logout_ts=0)
        before = self.state()
        payload = regression_service.build_payload(self.db, UID)
        self.assertEqual(payload['end_timestamps'], 0)
        self.assertEqual(payload['find_time'], 0)
        self.assertEqual(self.state(), before)

    def test_mode4_unknown_empty_wrong_activity_and_unfinished_batch_cannot_award(self):
        for activity, ids in ((999999, [50110]), (4243301, []), (4243301, [999999]),
                              (4343301, [50110]), (4243301, [50110])):
            with self.subTest(activity=activity, ids=ids):
                self.rejected(89804, {'activity_id': activity, 'assignment_id': ids})
        self.prove_stage()
        self.rejected(89804, {'activity_id': 4243301, 'assignment_id': [50110, 999999]}, 2)

    def test_mode4_progress_from_another_user_or_mode_is_not_eligibility(self):
        self.prove_stage(owner=OTHER)
        self.other = self.state(OTHER)
        self.rejected(89804, {'activity_id': 4243301, 'assignment_id': [50110]}, 2)
        self.prove_stage(mode=3)
        self.rejected(89804, {'activity_id': 4243301, 'assignment_id': [50110]}, 2)

    def test_mode4_aliases_share_permanent_claims_and_relogin_states(self):
        self.prove_stage()
        before = self.balance(1)
        self.assertEqual(self.request(89804, {'activity_id': 4243301, 'assignment_id': [50110, 50110]})
                         ['reward_list'], [{'id': 1, 'num': 80}])
        self.assertEqual(self.balance(1), before + 80)
        self.assertEqual(self.request(89804, {'activity_id': 4243501, 'assignment_id': [50110]})
                         .get('reward_list', []), [])
        self.assertEqual(self.balance(1), before + 80)
        mode, data, _ = weekly_challenge_service.get_core_verification_challenge_data(UID, self.db, 4243301)
        self.assertEqual(mode, 4)
        decoded = decode(encode('sc_89801', data), 'sc_89801')
        self.assertIn({'assignment_id': 50110, 'state': 2}, decoded['finish_assignment_list'])

    def test_mode4_score_threshold_and_mixed_claimed_tasks_award_only_new_entries(self):
        self.prove_stage(stage=3093091, score=14999)
        self.rejected(89804, {'activity_id': 4243301, 'assignment_id': [50114]}, 2)
        self.prove_stage(stage=3093091, score=15000)
        self.assertEqual(self.request(89804, {'activity_id': 4243301, 'assignment_id': [50114]})
                         ['reward_list'], [{'id': 40603, 'num': 60}, {'id': 2, 'num': 30000}])
        self.prove_stage()
        reply = self.request(89804, {'activity_id': 4243301, 'assignment_id': [50114, 50110]})
        self.assertEqual(reply['reward_list'], [{'id': 1, 'num': 80}])
        self.db.reset_core_verification_cl(UID, 4243501)
        self.assertEqual(self.request(89804, {'activity_id': 4243301, 'assignment_id': [50110]})['result'], 0)

    def test_failed_grants_restore_claims_balances_and_notifications(self):
        original = InventoryService.grant_items
        writes = []

        def fail_after_write(*args, **kwargs):
            before = self.state()
            original(*args, **kwargs)
            self.assertNotEqual(self.state(), before, 'failure injection must follow a real asset write')
            writes.append(True)
            raise RuntimeError('injected grant failure after SQL')

        self.prove_stage()
        cases = ((20012, self.shop_request([{'id': 2, 'num': 800}])), (59012, {}),
                 (59004, {'level': 20}), (62012, {'index': 1}), (62016, {}),
                 (89804, {'activity_id': 4243301, 'assignment_id': [50110]}))
        with patch.object(InventoryService, 'grant_items', side_effect=fail_after_write):
            for cmd, data in cases:
                with self.subTest(cmd=cmd):
                    count = len(writes)
                    self.rejected(cmd, data, 9)
                    self.assertEqual(len(writes), count + 1)

    def test_empty_necessary_receipts_roll_back_rewards_and_hero_costs(self):
        self.prove_stage()
        cases = ((59012, {}, 59013), (59004, {'level': 20}, 59005),
                 (62012, {'index': 1}, 62013), (62016, {}, 62017),
                 (89804, {'activity_id': 4243301, 'assignment_id': [50110]}, 89805),
                 (46016, {'hero_id': HERO, 'material_list': [{'id': 40201, 'num': 1}]}, 46017),
                 (14120, {'hero_id': HERO, 'level': 30, 'break_list': [1],
                          'item_list': [{'id': 40104, 'num': 100}]}, 14121))
        original = self.ctx.codec_encode
        for cmd, data, sc in cases:
            with self.subTest(cmd=cmd), patch.object(self.ctx, 'codec_encode',
                    side_effect=lambda name, value, expected='sc_' + str(sc):
                    None if name == expected else original(name, value)):
                self.rejected(cmd, data, 1)

    def test_missing_necessary_asset_updates_roll_back_shop_and_activity(self):
        original = generator.gen_payload

        def missing(cmd, *args, **kwargs):
            return None if cmd == 17023 else original(cmd, *args, **kwargs)

        with patch('generator.gen_payload', side_effect=missing):
            self.rejected(20012, self.shop_request([{'id': 2, 'num': 800}]))
            self.rejected(59004, {'level': 20}, 1)

    def race(self, call):
        peers = [AccountDB(str(self.path)), AccountDB(str(self.path))]
        barrier = threading.Barrier(2)

        def worker(db):
            ctx = context(db)
            ctx.uid = UID
            ctx.log = lambda *args, **kwargs: None
            try:
                barrier.wait(timeout=10)
                return call(ctx)
            except OperationError as exc:
                return exc.code
            finally:
                db.close()

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                return list(pool.map(worker, peers))
        finally:
            for db in peers:
                db.close()

    def test_concurrent_newbie_level_claims_award_once(self):
        before = self.balance(11084)
        result = self.race(lambda ctx: newbie_service.claim_level(ctx, UID, 20))
        self.assertEqual(sum(isinstance(value, list) for value in result), 1)
        self.assertIn(2, result)
        self.assertEqual(self.balance(11084), before + 12)

    def test_concurrent_mode4_alias_claims_award_once(self):
        self.prove_stage()
        before = self.balance(1)
        result = self.race(lambda ctx: mode4.claim_rewards(ctx, UID, 4243301, [50110]))
        self.assertEqual(sorted(len(value) for value in result), [0, 1])
        self.assertEqual(self.balance(1), before + 80)


if __name__ == '__main__':
    unittest.main()
