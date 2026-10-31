"""A53/A81: real item-use packets, one award writer, and failed-write rollback."""
import builtins
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_seed import get_seed_path

SEED = get_seed_path()
SANDBOX = tempfile.TemporaryDirectory(prefix='ag-asset-atomicity-tests-')
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

UID = 2174928301
OTHER = 264084870
TABLES = ('users', 'currency', 'material', 'hero', 'hero_piece', 'equip',
          'servant', 'chat_sticker', 'user_scene', 'player_skin_unlocked',
          'claim_ledger', 'mail', 'mythic_final_progress', 'mythic_progress_reward',
          'mythic_progress_partition')


class AssetAtomicityTests(unittest.TestCase):
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
        self.db.set_currency(UID, 31, 100)
        self.db.set_currency(UID, 32, 0)
        for iid in (22001, 22002, 30091, 41101):
            self.db.set_material(UID, iid, 4)
        self.other = self.state(OTHER)
        self.assertTrue(self.other['currency'])
        self.assertTrue(self.other['hero'])

    def tearDown(self):
        self.assertEqual(self.state(OTHER), self.other)
        current = self.state()
        self.assertEqual(self.db.query('PRAGMA integrity_check')[0]['integrity_check'], 'ok')
        with sqlite3.connect('file:' + str(self.path) + '?mode=ro', uri=True) as saved:
            saved.row_factory = sqlite3.Row
            persisted = {table: sorted([dict(r) for r in saved.execute(
                'SELECT * FROM ' + table + ' WHERE uid=?', (UID,))],
                key=lambda row: json.dumps(row, sort_keys=True)) for table in TABLES}
        self.assertEqual(persisted, current)

    def state(self, uid=UID):
        return {table: sorted(self.db.query('SELECT * FROM ' + table + ' WHERE uid=?', (uid,)),
                              key=lambda row: json.dumps(row, sort_keys=True)) for table in TABLES}

    def balance(self, iid):
        return InventoryService.get_item_balance(self.db, UID, iid)[1]

    @staticmethod
    def entry(iid, count, choices=()):
        return {'item_info': {'id': iid, 'num': count, 'time_valid': 0},
                'use_list': list(choices)}

    def request(self, op_class, data, ctx=None):
        ctx = ctx or self.ctx
        response = Core()._run_operation(op_class(), CSRequest(
            op_class.cmd, encode('cs_' + str(op_class.cmd), data), uid=UID), ctx)
        replies = [f for f in response.frames if f['sc'] == op_class.sc]
        self.assertEqual(len(replies), 1)
        return decode(replies[0]['payload'], 'sc_' + str(op_class.sc)), response.frames

    def use(self, entries):
        return self.request(operations.UseItemOp, {'use_item_list': entries})

    def preexisting_notifications(self):
        self.ctx.touched_items.add(38)
        self.ctx.append_frame(DownFrame(10051, b'\x08\x01'))
        return checkpoint_notifications(self.ctx)

    def fail_after_real_grant(self, function):
        original = function
        writes = []

        def fail(*args, **kwargs):
            original(*args, **kwargs)
            writes.append(self.state())
            self.ctx.append_frame(DownFrame(53003, b'\x08\x01'))
            raise RuntimeError('injected after real asset write')

        return fail, writes

    def test_duplicate_item_demands_reject_without_consuming_first_entry(self):
        self.db.set_material(UID, 22001, 3)
        before = self.state()
        reply, frames = self.use([self.entry(22001, 2), self.entry(22001, 2)])
        self.assertEqual(reply['result'], 3)
        self.assertEqual(self.state(), before)
        self.assertFalse(reply.get('drop_list'))
        self.assertFalse(self.ctx.touched_items)

    def test_duplicate_items_with_sufficient_total_award_the_full_batch(self):
        before_gold = self.balance(2)
        reply, _ = self.use([self.entry(22001, 2), self.entry(22001, 2)])
        self.assertEqual(reply['result'], 0)
        self.assertEqual(self.balance(22001), 0)
        self.assertEqual(self.balance(2) - before_gold, 4000)
        self.assertEqual({x['id']: x['num'] for x in reply['drop_list']}, {2: 4000})

    def test_mixed_item_rewards_merge_without_changing_their_costs(self):
        before_gold = self.balance(2)
        reply, _ = self.use([self.entry(22001, 2), self.entry(22002, 1)])
        self.assertEqual(reply['result'], 0)
        self.assertEqual((self.balance(22001), self.balance(22002)), (2, 3))
        self.assertEqual(self.balance(2) - before_gold, 4000)

    def test_insufficient_later_item_rejects_before_any_consumption(self):
        self.db.set_material(UID, 22002, 0)
        before = self.state()
        reply, _ = self.use([self.entry(22001, 1), self.entry(22002, 1)])
        self.assertEqual(reply['result'], 3)
        self.assertEqual(self.state(), before)

    def test_duplicate_selection_boxes_keep_each_entrys_selected_reward(self):
        before = {iid: self.balance(iid) for iid in (40302, 40303)}
        reply, _ = self.use([self.entry(30091, 1, (1,)), self.entry(30091, 1, (2,))])
        self.assertEqual(reply['result'], 0)
        self.assertEqual(self.balance(30091), 2)
        self.assertEqual({iid: self.balance(iid) - num for iid, num in before.items()},
                         {40302: 10, 40303: 10})
        self.assertEqual({x['id']: x['num'] for x in reply['drop_list']}, {40302: 10, 40303: 10})

    def second_debit_failure(self):
        original = InventoryService.cost_item
        writes = []

        def debit(ctx, uid, iid, count=1):
            if iid == 22002:
                return False
            result = original(ctx, uid, iid, count)
            writes.append(self.balance(iid))
            return result

        return debit, writes

    def test_debit_failure_during_execution_rolls_back_the_Core_batch(self):
        before = self.state()
        debit, writes = self.second_debit_failure()
        with patch.object(InventoryService, 'cost_item', side_effect=debit):
            reply, _ = self.use([self.entry(22001, 1), self.entry(22002, 1)])
        self.assertEqual(writes, [3])
        self.assertEqual(reply['result'], 3)
        self.assertEqual(self.state(), before)
        self.assertFalse(self.ctx.touched_items)

    def test_direct_item_use_rolls_back_a_later_debit_failure(self):
        before = self.state()
        checkpoint = self.preexisting_notifications()
        debit, writes = self.second_debit_failure()
        with patch.object(InventoryService, 'cost_item', side_effect=debit):
            with self.assertRaises(OperationError) as caught:
                InventoryService.use_items(self.ctx, UID, [self.entry(22001, 1), self.entry(22002, 1)])
        self.assertEqual(caught.exception.code, 3)
        self.assertEqual(writes, [3])
        self.assertEqual(self.state(), before)
        self.assertEqual(checkpoint_notifications(self.ctx), checkpoint)

    def test_Core_grant_failure_restores_consumption_rewards_and_notifications(self):
        before = self.state()
        fail, writes = self.fail_after_real_grant(InventoryService.grant_items)
        with patch.object(InventoryService, 'grant_items', side_effect=fail):
            reply, frames = self.use([self.entry(22001, 1)])
        self.assertEqual(len(writes), 1)
        self.assertNotEqual(writes[0], before)
        self.assertNotEqual(reply['result'], 0)
        self.assertEqual(self.state(), before)
        self.assertFalse(self.ctx.touched_items)
        self.assertNotIn(53003, [f['sc'] for f in frames])

    def test_direct_item_use_restores_real_writes_and_prior_notifications(self):
        before = self.state()
        checkpoint = self.preexisting_notifications()
        fail, writes = self.fail_after_real_grant(InventoryService.grant_items)
        with patch.object(InventoryService, 'grant_items', side_effect=fail):
            with self.assertRaises(RuntimeError):
                InventoryService.use_items(self.ctx, UID, [self.entry(22001, 1)])
        self.assertEqual(len(writes), 1)
        self.assertNotEqual(writes[0], before)
        self.assertEqual(self.state(), before)
        self.assertEqual(checkpoint_notifications(self.ctx), checkpoint)

    def test_caught_nested_item_use_failure_preserves_only_the_callers_writes(self):
        marker = self.balance(38)
        checkpoint = self.preexisting_notifications()
        fail, writes = self.fail_after_real_grant(InventoryService.grant_items)
        with self.db.transaction():
            self.db.set_currency(UID, 38, marker + 1)
            caller_state = self.state()
            with patch.object(InventoryService, 'grant_items', side_effect=fail):
                try:
                    InventoryService.use_items(self.ctx, UID, [self.entry(22001, 1)])
                except RuntimeError:
                    pass
                else:
                    self.fail('item use suppressed its award failure')
            self.assertEqual(self.state(), caller_state)
            self.assertEqual(checkpoint_notifications(self.ctx), checkpoint)
        self.assertEqual(self.balance(38), marker + 1)
        self.assertEqual(self.balance(22001), 4)
        self.assertEqual(len(writes), 1)

    def test_beacon_reset_failure_restores_consumed_beacon_and_partial_writes(self):
        self.db.execute("""INSERT INTO mythic_final_progress
            (uid,now_difficulty,can_choose_list,receive_reward_list,clear_list,challenge_info_json,update_ts)
            VALUES(?,1,'[1]','[1]','[1]','[]',1) ON CONFLICT(uid) DO UPDATE SET
            receive_reward_list='[1]',clear_list='[1]'""", (UID,))
        before = self.state()
        original = self.db.execute
        writes = []

        def fail_reset(sql, *args, **kwargs):
            if sql.startswith('DELETE FROM mythic_progress_reward'):
                writes.append((self.balance(41101), self.db.query(
                    'SELECT clear_list FROM mythic_final_progress WHERE uid=?', (UID,))[0]['clear_list']))
                raise sqlite3.OperationalError('injected after real beacon reset write')
            return original(sql, *args, **kwargs)

        with patch.object(self.db, 'execute', side_effect=fail_reset):
            reply, _ = self.use([self.entry(41101, 1)])
        self.assertEqual(writes, [(3, '[]')])
        self.assertNotEqual(reply['result'], 0)
        self.assertEqual(self.state(), before)

    def test_database_only_item_use_keeps_its_public_result_and_reward(self):
        before_gold = self.balance(2)
        result = InventoryService.use_items(self.db, UID, [self.entry(22001, 1)])
        self.assertEqual(result['result'], 0)
        self.assertEqual(self.balance(22001), 3)
        self.assertEqual(self.balance(2) - before_gold, 1000)

    def test_concurrent_use_and_retry_consume_one_available_box_once(self):
        self.db.set_material(UID, 22001, 1)
        before_gold = self.balance(2)
        barrier = threading.Barrier(2)

        def use_box():
            ctx = context(self.db)
            ctx.uid = UID
            ctx.log = lambda *a, **kw: None
            try:
                barrier.wait(timeout=10)
                return self.request(operations.UseItemOp, {'use_item_list': [self.entry(22001, 1)]}, ctx)[0]
            finally:
                self.db.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: use_box(), range(2)))
        self.assertTrue(any(result['result'] == 0 for result in results), results)
        self.assertEqual(self.balance(22001), 0)
        self.assertEqual(self.balance(2) - before_gold, 1000)
        retry, _ = self.use([self.entry(22001, 1)])
        self.assertEqual(retry['result'], 3)
        self.assertEqual(self.balance(2) - before_gold, 1000)

    def test_normal_public_currency_exchange_awards_once(self):
        before = self.balance(1)
        reply, _ = self.request(operations.CurrencyBuyDiamondOp, {'num': 10})
        self.assertEqual(reply['result'], 0)
        self.assertEqual(self.balance(31), 90)
        self.assertEqual(self.balance(1) - before, 10)

    def test_public_award_failure_rolls_back_cost_and_retries_without_double_award(self):
        before = self.state()
        original = inventory_service.grant_item
        written = []

        def fail(*args, **kwargs):
            original(*args, **kwargs)
            written.append(self.balance(1))
            raise RuntimeError('injected after currency award')

        with patch.object(inventory_service, 'grant_item', side_effect=fail):
            reply, _ = self.request(operations.CurrencyBuyDiamondOp, {'num': 10})
        self.assertEqual(written, [next(r['num'] for r in before['currency'] if r['id'] == 1) + 10])
        self.assertNotEqual(reply['result'], 0)
        self.assertEqual(self.state(), before)
        self.assertFalse(self.ctx.touched_items)
        retry, _ = self.request(operations.CurrencyBuyDiamondOp, {'num': 10})
        self.assertEqual(retry['result'], 0)
        self.assertEqual(self.balance(31), 90)
        self.assertEqual(self.balance(1), written[0])

    def test_direct_award_failure_restores_assets_and_existing_notifications(self):
        before = self.state()
        checkpoint = self.preexisting_notifications()
        fail, writes = self.fail_after_real_grant(inventory_service.grant_item)
        with patch.object(inventory_service, 'grant_item', side_effect=fail):
            with self.assertRaises(RuntimeError):
                operations._item_add(self.ctx, UID, 38, 7)
        self.assertEqual(len(writes), 1)
        self.assertNotEqual(writes[0], before)
        self.assertEqual(self.state(), before)
        self.assertEqual(checkpoint_notifications(self.ctx), checkpoint)

    def test_caught_nested_award_failure_preserves_only_the_callers_writes(self):
        marker = self.balance(2)
        checkpoint = self.preexisting_notifications()
        fail, writes = self.fail_after_real_grant(inventory_service.grant_item)
        with self.db.transaction():
            self.db.set_currency(UID, 2, marker + 1)
            caller_state = self.state()
            with patch.object(inventory_service, 'grant_item', side_effect=fail):
                try:
                    operations._item_add(self.ctx, UID, 38, 7)
                except RuntimeError:
                    pass
                else:
                    self.fail('award failure was hidden by a compatibility grant')
            self.assertEqual(self.state(), caller_state)
            self.assertEqual(checkpoint_notifications(self.ctx), checkpoint)
        self.assertEqual(len(writes), 1)
        self.assertEqual(self.balance(2), marker + 1)

    def test_real_equipment_instance_write_failure_does_not_duplicate_instances(self):
        before = self.state()
        prefab = int(next(iter(inventory_service._get_equip_map())))
        fail, writes = self.fail_after_real_grant(inventory_service.grant_item)
        with patch.object(inventory_service, 'grant_item', side_effect=fail):
            with self.assertRaises(RuntimeError):
                operations._item_add(self.ctx, UID, prefab, 1)
        self.assertEqual(len(writes[0]['equip']), len(before['equip']) + 1)
        self.assertEqual(self.state(), before)
        self.assertFalse(self.ctx.pending_frames)

    def test_explicit_legacy_exchange_remains_available(self):
        before = self.balance(1)
        with patch.object(operations, 'USE_NEW_INVENTORY_SERVICE', False):
            reply, _ = self.request(operations.CurrencyBuyDiamondOp, {'num': 10})
        self.assertEqual(reply['result'], 0)
        self.assertEqual(self.balance(31), 90)
        self.assertEqual(self.balance(1) - before, 10)

    def inventory_import_failure(self, missing):
        original = builtins.__import__

        def import_module(name, *args, **kwargs):
            if name == 'inventory_service':
                raise ModuleNotFoundError('injected missing ' + missing, name=missing)
            return original(name, *args, **kwargs)

        return patch('builtins.__import__', side_effect=import_module)

    def test_absent_inventory_module_uses_the_legacy_writer_once(self):
        before = self.balance(38)
        with self.inventory_import_failure('inventory_service'):
            operations._item_add(self.ctx, UID, 38, 7)
        self.assertEqual(self.balance(38) - before, 7)

    def test_missing_inventory_dependency_propagates_without_a_legacy_award(self):
        before = self.state()
        with self.inventory_import_failure('inventory_dependency'):
            with self.assertRaises(ModuleNotFoundError):
                operations._item_add(self.ctx, UID, 38, 7)
        self.assertEqual(self.state(), before)

    def test_broken_inventory_api_does_not_silently_award_via_legacy_SQL(self):
        before = self.state()
        with patch.object(inventory_service, 'grant_item', side_effect=AttributeError('injected broken API')):
            with self.assertRaises(AttributeError):
                operations._item_add(self.ctx, UID, 38, 7)
        self.assertEqual(self.state(), before)


if __name__ == '__main__':
    unittest.main()
