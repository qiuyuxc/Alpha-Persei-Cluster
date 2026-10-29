"""A54/A55/A56: item-use selections, configured rewards and probability outcomes."""
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

SEED = Path(os.environ['GM_TEST_DB_SEED']).resolve()
SANDBOX = tempfile.TemporaryDirectory(prefix='ag-item-use-contract-tests-')
BOOT = Path(SANDBOX.name) / 'bootstrap.db'
shutil.copyfile(SEED, BOOT)
os.environ['ACCOUNT_DB_PATH'] = str(BOOT)

from account_db import AccountDB
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
TABLES = ('users', 'currency', 'material', 'hero', 'hero_piece', 'equip', 'servant',
          'chat_sticker', 'user_scene', 'player_skin_unlocked', 'claim_ledger', 'mail')


class ItemUseContractTests(unittest.TestCase):
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
        for iid in (20013, 22001, 22002, 30009, 30024, 30029, 30031, 30040,
                    30050, 30091, 30203, 30206):
            self.db.set_material(UID, iid, 10)
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
        return {'item_info': {'id': iid, 'num': count, 'time_valid': 0}, 'use_list': list(choices)}

    def use(self, entries):
        response = Core()._run_operation(operations.UseItemOp(), CSRequest(
            17012, encode('cs_17012', {'use_item_list': entries}), uid=UID), self.ctx)
        replies = [f for f in response.frames if f['sc'] == 17013]
        self.assertEqual(len(replies), 1)
        return decode(replies[0]['payload'], 'sc_17013'), response.frames

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
        self.assertFalse(self.ctx.pending_frames)
        self.assertEqual(sum(f['sc'] == 10051 and f['payload'] == b'\x08\x01'
                             for f in frames), 1)
        self.assertNotIn(53003, [f['sc'] for f in frames])

    def with_usage(self, iid, param):
        cfg = deepcopy(inventory_service._get_item_usable_cfg())
        cfg[str(iid)]['param'] = param
        return patch('inventory_service._get_item_usable_cfg', return_value=cfg)

    def with_drop(self, entry):
        cfg = deepcopy(inventory_service._get_drop_cfg())
        cfg['100010'] = entry
        return patch('inventory_service._get_drop_cfg', return_value=cfg)

    def test_selection_indices_zero_and_beyond_end_cannot_consume_a_box(self):
        for choice in (0, 9, 4294967295):
            with self.subTest(choice=choice):
                self.rejected([self.entry(30091, choices=(choice,))], 2)

    def test_missing_selection_is_not_silently_replaced_with_first_reward(self):
        self.rejected([self.entry(30091)], 2)

    def test_duplicate_selection_cannot_double_one_boxs_reward(self):
        self.rejected([self.entry(30091, choices=(1, 1))], 2)

    def test_multiple_different_selections_cannot_multiply_one_boxs_reward(self):
        self.rejected([self.entry(30091, choices=(1, 2))], 2)

    def test_duplicate_options_are_rejected_even_when_opening_multiple_boxes(self):
        self.rejected([self.entry(30091, 2, (1, 1))], 2)

    def test_each_entry_keeps_its_own_single_choice_and_quantity(self):
        before = {iid: self.balance(iid) for iid in (40302, 40303)}
        reply, frames = self.use([self.entry(30091, 2, (1,)), self.entry(30091, 3, (2,))])
        self.assertEqual(reply['result'], 0)
        self.assertEqual(self.balance(30091), 5)
        expected = {40302: 20, 40303: 30}
        self.assertEqual({r['id']: r['num'] for r in reply['drop_list']}, expected)
        self.assertEqual({iid: self.balance(iid) - n for iid, n in before.items()}, expected)
        self.assertLess([f['sc'] for f in frames].index(17023),
                        [f['sc'] for f in frames].index(17013))

    def test_hero_piece_selection_preserves_the_configured_multiplier(self):
        before = self.balance(11248)
        reply, _ = self.use([self.entry(30203, 2, (2,))])
        self.assertEqual(reply['result'], 0)
        self.assertEqual(reply['drop_list'], [{'id': 11248, 'num': 20, 'time_valid': 0}])
        self.assertEqual(self.balance(11248) - before, 20)
        self.assertEqual(self.balance(30203), 8)

    def test_other_selection_subtypes_use_the_matching_clients_single_index(self):
        for box, reward, quantity in ((30056, 41201, 30), (30029, 1158, 1),
                                      (30021, 1019, 1), (30047, 11150, 5)):
            with self.subTest(box=box):
                self.db.set_material(UID, box, 10)
                if reward in (1158, 1019):
                    self.db.execute('UPDATE hero SET unlock=0 WHERE uid=? AND id=?', (UID, reward))
                before = self.balance(reward)
                reply, frames = self.use([self.entry(box, choices=(2,))])
                self.assertEqual(reply['result'], 0)
                self.assertEqual(reply['drop_list'], [{'id': reward, 'num': quantity, 'time_valid': 0}])
                self.assertEqual(self.balance(reward) - before, quantity)
                self.assertEqual(self.balance(box), 9)
                if reward in (1158, 1019):
                    self.assertIn(14007, [f['sc'] for f in frames])

    def test_time_limited_coolant_resolves_its_normal_items_vitality_reward(self):
        import fatigue_service
        fatigue_service.set_fatigue(UID, 0, db=self.db, push=False)
        reply, frames = self.use([self.entry(20013, 2)])
        self.assertEqual(reply['result'], 0)
        self.assertEqual(reply.get('drop_list', []), [{'id': 4, 'num': 60, 'time_valid': 0}])
        self.assertEqual(self.balance(4), 60)
        self.assertEqual(self.balance(20013), 8)
        self.assertIn(17025, [f['sc'] for f in frames])

    def test_missing_coolant_reference_restores_the_entire_batch(self):
        with self.with_usage(20013, [999999]):
            self.rejected([self.entry(22001), self.entry(20013)], 406)

    def test_invalid_selection_in_later_entry_restores_valid_earlier_costs(self):
        self.rejected([self.entry(22001), self.entry(30091, choices=(9,))], 2)

    def test_missing_usage_config_cannot_consume_a_selection_box(self):
        cfg = deepcopy(inventory_service._get_item_usable_cfg())
        del cfg['30091']
        with patch('inventory_service._get_item_usable_cfg', return_value=cfg):
            self.rejected([self.entry(30091, choices=(1,))], 406)

    def test_empty_selection_pool_cannot_consume_the_ticket(self):
        with self.with_usage(30206, []):
            self.rejected([self.entry(30206, choices=(1,))], 406)

    def test_2026_selection_ticket_has_all_59_client_options_and_awards_the_last(self):
        self.db.execute('UPDATE hero SET unlock=0 WHERE uid=? AND id=?', (UID, 1166))
        self.assertEqual(len(inventory_service._get_item_usable_cfg()['30206']['param']), 59)
        reply, frames = self.use([self.entry(30206, choices=(59,))])
        self.assertEqual(reply['result'], 0)
        self.assertEqual(reply['drop_list'], [{'id': 1166, 'num': 1, 'time_valid': 0}])
        self.assertEqual(self.balance(1166), 1)
        self.assertEqual(self.balance(30206), 9)
        self.assertIn(14007, [f['sc'] for f in frames])

    def test_malformed_selection_rewards_restore_the_entire_batch(self):
        for param in (None, [], [[0, 1]], [[40302, 0]], [[40302]], [[40302, -1]],
                      [[40302, 10], ['40303', 10]], {'1': [40302, 10]}):
            with self.subTest(param=param), self.with_usage(30091, param):
                self.rejected([self.entry(22001), self.entry(30091, choices=(1,))], 406)

    def test_fixed_reward_configuration_must_exist_and_have_positive_pairs(self):
        for param in (None, [], [[2, 0]], [[0, 1000]], [[2]], [[2, 1000], []]):
            with self.subTest(param=param), self.with_usage(22001, param):
                self.rejected([self.entry(22002), self.entry(22001)], 406)

    def test_missing_drop_table_does_not_consume_a_weighted_box(self):
        cfg = deepcopy(inventory_service._get_drop_cfg())
        del cfg['100012']
        with patch('inventory_service._get_drop_cfg', return_value=cfg):
            self.rejected([self.entry(22001), self.entry(30050)], 406)

    def test_malformed_drop_tables_cannot_report_success_and_consume_boxes(self):
        entries = (None, {}, {'base': [], 'random': [], 'weight': [], 'weight_count': 0},
                   {'base': [], 'random': [[40, 1, 101]], 'weight': [], 'weight_count': 0},
                   {'base': [], 'random': [[40, 1, -1]], 'weight': [], 'weight_count': 0},
                   {'base': [], 'random': [[40, 0, 50]], 'weight': [], 'weight_count': 0},
                   {'base': 'broken', 'random': [[40, 1]], 'weight': [], 'weight_count': 0},
                   {'base': [], 'random': [], 'weight': [[40, 1, 0]], 'weight_count': 1},
                   {'base': [], 'random': [], 'weight': [[40, 1, -1]], 'weight_count': 1},
                   {'base': [], 'random': [], 'weight': [[40, 1, 1]], 'weight_count': -1})
        for entry in entries:
            with self.subTest(entry=entry), self.with_drop(entry):
                self.rejected([self.entry(22001), self.entry(30031)], 406)

    def test_drop_boxes_cannot_bypass_the_pool_with_a_selection_index(self):
        self.rejected([self.entry(30040, choices=(1,))], 2)

    def test_real_matrix_probability_misses_award_only_the_certain_reward(self):
        before = {iid: self.balance(iid) for iid in (40, 30007, 30006, 30005, 1)}
        with patch('inventory_service.random.randint', return_value=100):
            reply, _ = self.use([self.entry(30031, 2)])
        self.assertEqual(reply['result'], 0)
        self.assertEqual({r['id']: r['num'] for r in reply['drop_list']}, {40: 100})
        self.assertEqual({iid: self.balance(iid) - n for iid, n in before.items()},
                         {40: 100, 30007: 0, 30006: 0, 30005: 0, 1: 0})
        self.assertEqual(self.balance(30031), 8)

    def test_probability_boundary_hits_are_inclusive_and_each_box_rolls_independently(self):
        entry = {'base': [[2, 5, 20]], 'random': [[40302, 2, 30]], 'weight': [], 'weight_count': 0}
        with self.with_drop(entry), patch('inventory_service.random.randint',
                                         side_effect=[20, 30, 21, 31]) as roll:
            reply, _ = self.use([self.entry(30031, 2)])
        self.assertEqual(reply['result'], 0)
        self.assertEqual({r['id']: r['num'] for r in reply['drop_list']}, {2: 5, 40302: 2})
        self.assertEqual(roll.call_count, 4)

    def test_zero_probability_never_awards_and_two_field_rows_remain_certain(self):
        entry = {'base': [[2, 5, 0]], 'random': [[40302, 2, 0], [40303, 3]],
                 'weight': [], 'weight_count': 0}
        before = {iid: self.balance(iid) for iid in (2, 40302, 40303)}
        with self.with_drop(entry), patch('inventory_service.random.randint', return_value=1):
            reply, _ = self.use([self.entry(30031)])
        self.assertEqual(reply['result'], 0)
        self.assertEqual({r['id']: r['num'] for r in reply['drop_list']}, {40303: 3})
        self.assertEqual({iid: self.balance(iid) - n for iid, n in before.items()},
                         {2: 0, 40302: 0, 40303: 3})

    def test_valid_chance_only_box_can_have_an_empty_outcome(self):
        entry = {'base': [], 'random': [[40302, 2, 10]], 'weight': [], 'weight_count': 0}
        before = self.balance(40302)
        with self.with_drop(entry), patch('inventory_service.random.randint', return_value=11):
            reply, _ = self.use([self.entry(30031)])
        self.assertEqual(reply['result'], 0)
        self.assertFalse(reply.get('drop_list'))
        self.assertEqual(self.balance(40302), before)
        self.assertEqual(self.balance(30031), 9)

    def test_real_weighted_box_awards_one_configured_reward_per_box(self):
        before = self.balance(1)
        with patch('inventory_service.random.randint', side_effect=[1, 1070]):
            reply, _ = self.use([self.entry(30040, 2)])
        self.assertEqual(reply['result'], 0)
        self.assertEqual(reply['drop_list'], [{'id': 1, 'num': 732, 'time_valid': 0}])
        self.assertEqual(self.balance(1) - before, 732)
        self.assertEqual(self.balance(30040), 8)

    def test_zero_weight_count_is_not_replaced_with_one_draw(self):
        entry = {'base': [[40, 5]], 'random': [], 'weight': [[1, 66, 100]], 'weight_count': 0}
        with self.with_drop(entry), patch('inventory_service.random.randint') as roll:
            reply, _ = self.use([self.entry(30031)])
        self.assertEqual(reply['result'], 0)
        self.assertEqual(reply['drop_list'], [{'id': 40, 'num': 5, 'time_valid': 0}])
        roll.assert_not_called()

    def test_direct_invalid_selection_raises_without_committing_or_losing_notifications(self):
        before = self.state()
        checkpoint = checkpoint_notifications(self.ctx)
        with self.assertRaises(OperationError) as caught:
            InventoryService.use_item(self.ctx, UID, 30091, use_list=[9])
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(self.state(), before)
        self.assertEqual(checkpoint_notifications(self.ctx), checkpoint)

    def test_caught_nested_invalid_batch_preserves_only_callers_writes(self):
        marker = self.balance(38)
        self.ctx.touched_items.add(38)
        checkpoint = checkpoint_notifications(self.ctx)
        with self.db.transaction():
            self.db.set_currency(UID, 38, marker + 1)
            caller_state = self.state()
            with self.assertRaises(OperationError):
                InventoryService.use_items(self.ctx, UID, [self.entry(22001),
                                         self.entry(30091, choices=(1, 1))])
            self.assertEqual(self.state(), caller_state)
        self.assertEqual(self.balance(38), marker + 1)
        self.assertEqual(checkpoint_notifications(self.ctx), checkpoint)

    def test_real_award_failure_restores_selection_box_rewards_and_notifications(self):
        original = InventoryService.grant_items
        writes = []

        def fail(*args, **kwargs):
            result = original(*args, **kwargs)
            writes.append(self.balance(40302))
            self.ctx.append_frame(DownFrame(53003, b'\x08\x01'))
            raise RuntimeError('failure after real selection reward write')

        before = self.balance(40302)
        with patch.object(InventoryService, 'grant_items', side_effect=fail):
            self.rejected([self.entry(30091, choices=(1,))])
        self.assertEqual(writes, [before + 10])

    def test_failed_required_reply_restores_probability_rewards_and_box_cost(self):
        original = self.ctx.codec_encode

        def fail_reply(proto, data):
            if proto == 'sc_17013' and data.get('result') == 0:
                raise RuntimeError('probability box receipt failure')
            return original(proto, data)

        with patch.object(self.ctx, 'codec_encode', side_effect=fail_reply), \
                patch('inventory_service.random.randint', return_value=100):
            self.rejected([self.entry(30031)])


if __name__ == '__main__':
    unittest.main()
