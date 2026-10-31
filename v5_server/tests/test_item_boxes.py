"""Random gift/intel boxes must award items of the configured type and rarity."""
from contextlib import ExitStack
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from test_seed import get_seed_path

SEED = get_seed_path()
SANDBOX = tempfile.TemporaryDirectory(prefix='ag-item-box-tests-')
BOOT = Path(SANDBOX.name) / 'bootstrap.db'
shutil.copyfile(SEED, BOOT)
os.environ['ACCOUNT_DB_PATH'] = str(BOOT)

from account_db import AccountDB
from client_rule_loader import load_rules
from codec import decode, encode
from core import Core, OperationError
import event_bus
from gm_actions import context
from inventory_service import InventoryService
import inventory_service
from middleware import CSRequest, DownFrame
from operation_notifications import checkpoint_notifications
import operations

UID, OTHER = 2174928301, 264084870
BOXES = {30002: (3, 5), 30003: (3, 4), 30004: (3, 3), 30011: (10, 4)}
TABLES = ('users', 'currency', 'material', 'hero', 'hero_piece', 'equip', 'servant',
          'claim_ledger', 'mail', 'trust_displace_counter')


class ItemBoxTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=SANDBOX.name)
        self.addCleanup(self.folder.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(event_bus.EventBus, '_handlers', {}))
        self.stack.enter_context(patch('event_bus.bus.emit'))
        self.stack.enter_context(patch('game_cycles.apply_rotation'))
        self.path = Path(self.folder.name) / 'player.db'
        shutil.copyfile(SEED, self.path)
        self.db = AccountDB(str(self.path))
        self.addCleanup(self.db.close)
        self.ctx = context(self.db)
        self.ctx.uid = UID
        self.ctx.log = lambda *a, **kw: None
        self.pools = {box: [r['id'] for r in self.db.query(
            'SELECT id FROM item_catalog WHERE type=? AND rare=? ORDER BY id', param)]
                      for box, param in BOXES.items()}
        self.db.execute('UPDATE hero_piece SET num=0 WHERE uid=?', (UID,))
        for iid in (*BOXES, 22001, 30091):
            self.db.set_material(UID, iid, 100)
        self.other = self.state(OTHER)

    def tearDown(self):
        self.assertEqual(self.state(OTHER), self.other)
        self.assertEqual(self.db.query('PRAGMA integrity_check')[0]['integrity_check'], 'ok')
        with sqlite3.connect('file:' + str(self.path) + '?mode=ro', uri=True) as saved:
            saved.row_factory = sqlite3.Row
            state = {table: sorted([dict(r) for r in saved.execute(
                'SELECT * FROM ' + table + ' WHERE uid=?', (UID,))],
                key=lambda row: json.dumps(row, sort_keys=True)) for table in TABLES}
        self.assertEqual(state, self.state())

    def state(self, uid=UID):
        return {table: sorted(self.db.query('SELECT * FROM ' + table + ' WHERE uid=?', (uid,)),
                              key=lambda row: json.dumps(row, sort_keys=True)) for table in TABLES}

    def balance(self, iid):
        return InventoryService.get_item_balance(self.db, UID, iid)[1]

    @staticmethod
    def entry(iid, count=1, choices=()):
        return {'item_info': {'id': iid, 'num': count, 'time_valid': 0},
                'use_list': list(choices)}

    def use(self, entries):
        response = Core()._run_operation(operations.UseItemOp(), CSRequest(
            17012, encode('cs_17012', {'use_item_list': entries}), uid=UID), self.ctx)
        reply = decode(next(f['payload'] for f in response.frames if f['sc'] == 17013), 'sc_17013')
        return reply, response.frames

    def successful_rolls(self, box, rolls):
        before_box = self.balance(box)
        before_items = {iid: self.balance(iid) for iid in set(rolls)}
        currencies = {iid: self.balance(iid) for iid in (3, 10)}
        with patch('inventory_service.random.choice', side_effect=rolls) as choose:
            reply, frames = self.use([self.entry(box, len(rolls))])
        self.assertEqual(reply['result'], 0)
        self.assertEqual(choose.call_count, len(rolls))
        for call in choose.call_args_list:
            self.assertEqual(list(call.args[0]), self.pools[box])
        expected = {iid: rolls.count(iid) for iid in set(rolls)}
        self.assertEqual({r['id']: r['num'] for r in reply['drop_list']}, expected)
        self.assertEqual(self.balance(box), before_box - len(rolls))
        self.assertEqual({iid: self.balance(iid) - amount for iid, amount in before_items.items()}, expected)
        self.assertEqual({iid: self.balance(iid) for iid in currencies}, currencies)
        self.assertIn(17023, [f['sc'] for f in frames])
        return reply

    def rejected(self, entries, code=None):
        before = self.state()
        self.ctx.touched_items.add(38)
        self.ctx.append_frame(DownFrame(10051, b'\x08\x01'))
        checkpoint = checkpoint_notifications(self.ctx)
        reply, frames = self.use(entries)
        self.assertNotEqual(reply['result'], 0)
        if code is not None:
            self.assertEqual(reply['result'], code)
        self.assertFalse(reply.get('drop_list'))
        self.assertEqual(self.state(), before)
        after = checkpoint_notifications(self.ctx)
        for name in checkpoint.keys() - {'pending_frames'}:
            self.assertEqual(after[name], checkpoint[name])
        # Core transfers prior pending frames into the prepared error response.
        self.assertFalse(self.ctx.pending_frames)
        self.assertEqual(sum(f['sc'] == 10051 and f['payload'] == b'\x08\x01'
                             for f in frames), 1)
        self.assertNotIn(53003, [f['sc'] for f in frames])
        return reply

    def test_gift_box_produces_one_gift_per_box_instead_of_secret_diamonds(self):
        self.successful_rolls(30011, [50001, 50002, 50001, 30014])

    def test_full_gift_pool_has_visible_generic_and_personal_gifts(self):
        self.assertEqual(len(self.pools[30011]), 65)
        self.assertIn(30014, self.pools[30011])
        self.assertIn(50066, self.pools[30011])
        self.successful_rolls(30011, self.pools[30011])

    def test_legacy_s_intel_awards_matching_hero_pieces(self):
        self.assertEqual(len(self.pools[30002]), 61)
        self.successful_rolls(30002, self.pools[30002])

    def test_legacy_a_intel_awards_matching_hero_pieces(self):
        self.assertEqual(len(self.pools[30003]), 21)
        self.successful_rolls(30003, self.pools[30003])

    def test_legacy_b_intel_awards_matching_hero_pieces(self):
        self.assertEqual(len(self.pools[30004]), 2)
        self.successful_rolls(30004, self.pools[30004])

    def test_real_random_rolls_are_eligible_and_preserve_total_quantity(self):
        before_currency = {iid: self.balance(iid) for iid in (3, 10)}
        reply, _ = self.use([self.entry(30011, 20)])
        self.assertEqual(reply['result'], 0)
        self.assertEqual(sum(r['num'] for r in reply['drop_list']), 20)
        self.assertTrue(all(r['id'] in self.pools[30011] for r in reply['drop_list']))
        self.assertEqual(self.balance(30011), 80)
        self.assertEqual({iid: self.balance(iid) for iid in before_currency}, before_currency)

    def test_random_boxes_cannot_be_turned_into_selection_boxes(self):
        self.rejected([self.entry(30011, choices=(1,))], 2)

    def test_bad_later_random_box_restores_a_valid_earlier_item(self):
        self.rejected([self.entry(22001), self.entry(30011, choices=(1,))], 2)

    def test_missing_box_usage_config_rejects_without_consuming(self):
        cfg = deepcopy(inventory_service._get_item_usable_cfg())
        del cfg['30011']
        with patch('inventory_service._get_item_usable_cfg', return_value=cfg):
            self.rejected([self.entry(30011)], 406)

    def test_missing_pool_file_restores_the_entire_batch(self):
        with patch('client_rule_loader.load_rules', side_effect=OperationError(406, 'missing rules')):
            self.rejected([self.entry(22001), self.entry(30011)], 406)

    def test_empty_or_invalid_pool_rejects_without_consuming(self):
        for invalid in ([], [10, 10], [0], ['50001']):
            with self.subTest(pool=invalid):
                rules = deepcopy(load_rules('item_random_box_rules.json'))
                rules['boxes']['30011']['item_ids'] = invalid
                with patch('client_rule_loader.load_rules', return_value=rules):
                    self.rejected([self.entry(30011)], 406)

    def test_pool_parameters_must_match_current_box_configuration(self):
        cfg = deepcopy(inventory_service._get_item_usable_cfg())
        cfg['30011']['param'] = [10, 5]
        with patch('inventory_service._get_item_usable_cfg', return_value=cfg):
            self.rejected([self.entry(30011)], 406)

    def test_duplicate_demands_respect_total_stock(self):
        self.db.set_material(UID, 30011, 3)
        self.rejected([self.entry(30011, 2), self.entry(30011, 2)], 3)

    def test_mixed_random_fixed_and_selection_boxes_keep_their_own_rewards(self):
        before = {iid: self.balance(iid) for iid in (2, 10, 40303, 50001)}
        with patch('inventory_service.random.choice', return_value=50001):
            reply, _ = self.use([self.entry(30011, 2), self.entry(22001), self.entry(30091, choices=(2,))])
        self.assertEqual(reply['result'], 0)
        self.assertEqual({r['id']: r['num'] for r in reply['drop_list']}, {50001: 2, 2: 1000, 40303: 10})
        self.assertEqual({iid: self.balance(iid) - n for iid, n in before.items()},
                         {2: 1000, 10: 0, 40303: 10, 50001: 2})

    def failing_grant(self):
        original, writes = InventoryService.grant_items, []

        def fail(*args, **kwargs):
            result = original(*args, **kwargs)
            writes.append(self.state())
            self.ctx.append_frame(DownFrame(53003, b'\x08\x01'))
            raise RuntimeError('failure after real random gift award')

        return fail, writes

    def test_failed_real_award_restores_boxes_gifts_and_notifications(self):
        before = self.state()
        fail, writes = self.failing_grant()
        with patch.object(InventoryService, 'grant_items', side_effect=fail):
            self.rejected([self.entry(30011, 3)])
        self.assertEqual(len(writes), 1)
        self.assertNotEqual(writes[0], before)

    def test_caught_nested_failure_preserves_only_the_callers_writes(self):
        marker = self.balance(38)
        self.ctx.touched_items.add(38)
        checkpoint = checkpoint_notifications(self.ctx)
        fail, writes = self.failing_grant()
        with self.db.transaction():
            self.db.set_currency(UID, 38, marker + 1)
            caller_state = self.state()
            with patch.object(InventoryService, 'grant_items', side_effect=fail):
                with self.assertRaises(RuntimeError):
                    InventoryService.use_items(self.ctx, UID, [self.entry(30011, 3)])
            self.assertEqual(self.state(), caller_state)
        self.assertEqual(self.balance(38), marker + 1)
        self.assertEqual(checkpoint_notifications(self.ctx), checkpoint)
        self.assertEqual(len(writes), 1)

    def test_failed_required_reply_restores_a_successful_roll(self):
        original = self.ctx.codec_encode

        def fail_reply(proto, data):
            if proto == 'sc_17013' and data.get('result') == 0:
                raise RuntimeError('random box receipt failure')
            return original(proto, data)

        with patch.object(self.ctx, 'codec_encode', side_effect=fail_reply):
            self.rejected([self.entry(30011, 3)])


if __name__ == '__main__':
    unittest.main()
