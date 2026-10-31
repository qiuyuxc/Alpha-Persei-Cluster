"""Recharge shelves must only expose popup routes present in the client bundle."""
from contextlib import ExitStack
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from test_seed import get_seed_path

SEED = get_seed_path()
SANDBOX = tempfile.TemporaryDirectory(prefix='ag-shop-resource-tests-')
BOOTSTRAP = Path(SANDBOX.name) / 'bootstrap.db'
shutil.copyfile(SEED, BOOTSTRAP)
os.environ['ACCOUNT_DB_PATH'] = str(BOOTSTRAP)

from account_db import AccountDB
from codec import decode
from core import OperationError
import generator
from gm_actions import context
from recharge_service import RechargeService
from shop_service import ShopService

UID = 2174928301
NOW = int(time.time())
OLD_BOX = 3009003
CURRENT_BOX = 3009004


class ShopResourceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=SANDBOX.name)
        self.path = Path(self.folder.name) / 'player.db'
        shutil.copyfile(SEED, self.path)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('time.time', return_value=NOW))
        self.stack.enter_context(patch('event_bus.bus.emit'))
        for name in ('_instance', '_SHOP_BASE_TEMPLATE', '_SHOP_CFG_BASE_TEMPLATE'):
            self.stack.enter_context(patch.object(ShopService, name, None))
        self.db = AccountDB(str(self.path))
        self.shop = ShopService.get_instance(self.db)
        self.ctx = context(self.db)

    def tearDown(self):
        self.stack.close()
        self.db.close()
        self.folder.cleanup()

    def shelf(self, payload, key, shop_id=3):
        shops = payload[key] if key != 'changed_shop_info' else [payload[key]]
        return next(s['goods_list'] for s in shops if s['shop_id'] == shop_id)

    def shelves(self):
        return (
            self.shelf(self.shop.get_shop_cfg_payload(), 'shop_item_cfg_list'),
            self.shelf(self.shop.get_shop_data_payload(UID), 'shop_item_list'),
            self.shelf(self.shop.get_single_shop_payload(UID, 3), 'changed_shop_info'),
        )

    def player_state(self):
        return {
            table: sorted(self.db.query('SELECT * FROM ' + table + ' WHERE uid=?', (UID,)),
                          key=lambda row: json.dumps(row, sort_keys=True))
            for table in ('currency', 'material', 'store_purchase', 'user_recharge')
        }

    def reset_purchase(self, gid):
        self.db.execute('DELETE FROM store_purchase WHERE uid=? AND goods_id=?', (UID, gid))

    def test_missing_popup_removed_from_config_login_and_refresh(self):
        self.db.upsert('store_purchase', UID, {
            'shop_id': 3, 'goods_id': OLD_BOX, 'buy_times': 1,
            'next_refresh_timestamp': NOW + 86400, 'update_ts': NOW,
        }, keys=('uid', 'shop_id', 'goods_id'))
        purchases = self.db.query('SELECT * FROM store_purchase WHERE uid=?', (UID,))
        shelves = self.shelves()
        ids = [{g['goods_id'] for g in shelf} for shelf in shelves]
        expected = {r['goods_id'] for r in self.db.query('SELECT goods_id FROM shop_goods WHERE shop_id=3')}
        self.assertEqual(ids[0], expected - {OLD_BOX})
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(ids[0], ids[2])
        self.assertIn(CURRENT_BOX, ids[0])
        self.assertEqual(self.db.query('SELECT * FROM store_purchase WHERE uid=?', (UID,)), purchases)
        for sid in (4, 5):
            self.assertTrue(self.shelf(self.shop.get_shop_data_payload(UID), 'shop_item_list', sid))

    def test_binary_login_packets_use_the_compatible_shelf(self):
        for cmd, key in ((20007, 'shop_item_cfg_list'), (20009, 'shop_item_list')):
            with self.subTest(cmd=cmd):
                payload = generator.gen_payload(cmd, uid=UID, db=self.db, res_version='321')
                shelf = self.shelf(decode(payload, 'sc_' + str(cmd)), key)
                ids = {g['goods_id'] for g in shelf}
                self.assertNotIn(OLD_BOX, ids)
                self.assertIn(CURRENT_BOX, ids)

    def test_new_skus_cannot_reintroduce_obsolete_popup_descriptions(self):
        original = self.db.query('SELECT * FROM shop_goods WHERE goods_id=?', (OLD_BOX,))[0]
        for gid, description in ((3099991, 30041), (3099992, 30043), (3099993, 30060)):
            row = dict(original, goods_id=gid, item_id=description)
            detail = json.loads(row['detail_json'])
            detail['description'] = description
            row['detail_json'] = json.dumps(detail)
            columns = list(row)
            self.db.execute('INSERT INTO shop_goods (' + ','.join(columns) + ') VALUES (' +
                            ','.join('?' for _ in columns) + ')', tuple(row[c] for c in columns))
        for shelf in self.shelves():
            ids = {g['goods_id'] for g in shelf}
            self.assertNotIn(3099991, ids)
            self.assertNotIn(3099992, ids)
            self.assertIn(3099993, ids)

    def test_empty_dynamic_shelf_does_not_restore_captured_goods(self):
        self.db.execute('DELETE FROM shop_goods WHERE shop_id=3')
        for shelf in self.shelves():
            self.assertEqual(shelf, [])

    def test_capture_and_returned_payloads_are_independent(self):
        captured = deepcopy(self.shop._get_shop_cfg_base_template())
        payload = self.shop.get_shop_cfg_payload()
        self.shelf(payload, 'shop_item_cfg_list').clear()
        self.assertEqual(self.shop._get_shop_cfg_base_template(), captured)
        self.assertIn(CURRENT_BOX, {g['goods_id'] for g in self.shelf(
            self.shop.get_shop_cfg_payload(), 'shop_item_cfg_list')})
        detail = json.loads(self.db.query('SELECT detail_json FROM shop_goods WHERE goods_id=?', (OLD_BOX,))[0]['detail_json'])
        detail['description'] = 34641
        self.db.execute('UPDATE shop_goods SET item_id=34641, detail_json=? WHERE goods_id=?',
                        (json.dumps(detail), OLD_BOX))
        for shelf in self.shelves():
            self.assertIn(OLD_BOX, {g['goods_id'] for g in shelf})

    def test_cached_old_purchase_rejected_without_asset_or_history_changes(self):
        before = self.player_state()
        self.ctx.touched_items.add(1)
        with self.assertRaises(OperationError) as caught:
            self.shop.buy_goods(self.ctx, UID, 3, [{'buy_id': OLD_BOX, 'buy_num': 1}])
        self.assertEqual(caught.exception.code, 406)
        self.assertEqual(self.player_state(), before)
        self.assertEqual(self.ctx.touched_items, {1})

    def test_mixed_basket_rolls_back_prior_grants_on_obsolete_product(self):
        self.reset_purchase(3001410)
        before = self.player_state()
        self.ctx.touched_items.add(1)
        with self.assertRaises(OperationError) as caught:
            self.shop.buy_goods(self.ctx, UID, 3, [
                {'buy_id': 3001410, 'buy_num': 1}, {'buy_id': OLD_BOX, 'buy_num': 1},
            ])
        self.assertEqual(caught.exception.code, 406)
        self.assertEqual(self.player_state(), before)
        self.assertEqual(self.ctx.touched_items, {1})

    def test_regular_combo_pack_still_grants_its_contents(self):
        self.reset_purchase(5000007)
        before = self.db.get_item_num(UID, 2)
        frames = self.shop.buy_goods(self.ctx, UID, 5, [{'buy_id': 5000007, 'buy_num': 1}])
        response = decode(next(f.payload for f in frames if f.cmd == 20013), 'sc_20013')
        self.assertEqual(response['result'], 0)
        self.assertIn({'id': 2, 'num': 2000}, response['give_items'])
        self.assertEqual(self.db.get_item_num(UID, 2), before + 2000)
        record = self.db.query('SELECT buy_times FROM store_purchase WHERE uid=? AND goods_id=5000007', (UID,))
        self.assertEqual(record[0]['buy_times'], 1)

    def test_paid_order_rejects_obsolete_box_before_recharge_mutation(self):
        before = self.player_state()
        with self.assertRaises(OperationError) as caught:
            RechargeService(self.db).handle_pay_order(self.db, UID, goods_id=1,
                                                     shop_id=3, buy_id=OLD_BOX, log=lambda *args: None)
        self.assertEqual(caught.exception.code, 406)
        self.assertEqual(self.player_state(), before)

    def test_standard_recharge_still_succeeds(self):
        service = RechargeService(self.db)
        before = self.db.get_item_num(UID, 30)
        result = service.handle_pay_order(self.db, UID, goods_id=1, log=lambda *args: None)
        self.assertEqual(result['result'], 0)
        self.assertTrue(result['reward'])
        self.assertGreater(self.db.get_item_num(UID, 30), before)


if __name__ == '__main__':
    unittest.main()
