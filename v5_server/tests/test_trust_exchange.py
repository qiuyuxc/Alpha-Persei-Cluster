"""A167: client-derived costs, aggregated limits, and complete exchange rollback."""
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

SEED = Path(os.environ['GM_TEST_DB_SEED']).resolve()
SANDBOX = tempfile.TemporaryDirectory(prefix='ag-trust-exchange-tests-')
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
from middleware import CSRequest
import operations
from trust_service import TrustService

UID, OTHER, SOURCE, SECOND, TARGET = 2174928301, 264084870, 50001, 50002, 30012
NOW, DAY = 1793275200, 86400
TABLES = ('users', 'currency', 'material', 'hero', 'trust_displace_counter')


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=SANDBOX.name)
        self.addCleanup(self.folder.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.clock = self.stack.enter_context(patch('time.time', return_value=NOW))
        self.stack.enter_context(patch.object(event_bus.EventBus, '_handlers', {}))
        self.stack.enter_context(patch('event_bus.bus.emit'))
        self.stack.enter_context(patch('game_cycles.apply_rotation'))
        self.stack.enter_context(patch.object(TrustService, '_instance', None))
        self.path = Path(self.folder.name) / 'player.db'
        shutil.copyfile(SEED, self.path)
        self.db = AccountDB(str(self.path))
        self.addCleanup(self.db.close)
        self.ctx = context(self.db)
        self.ctx.uid = UID
        self.ctx.log = lambda *a, **kw: None
        self.service = TrustService.get_instance()
        self.rules = load_rules('trust_exchange_rules.json')
        for iid in (SOURCE, SECOND):
            self.db.set_material(UID, iid, 1000)
        for iid in (30012, 30013, 30014, 30015):
            self.db.set_material(UID, iid, 0)
        self.db.execute('DELETE FROM trust_displace_counter WHERE uid=?', (UID,))
        self.other = self.state(OTHER)

    def tearDown(self):
        self.assertEqual(self.state(OTHER), self.other)
        self.assertEqual(self.db.query('PRAGMA integrity_check')[0]['integrity_check'], 'ok')
        with sqlite3.connect('file:' + str(self.path) + '?mode=ro', uri=True) as saved:
            saved.row_factory = sqlite3.Row
            actual = {table: sorted([dict(r) for r in saved.execute(
                'SELECT * FROM ' + table + ' WHERE uid=?', (UID,))],
                key=lambda row: json.dumps(row, sort_keys=True)) for table in TABLES}
        self.assertEqual(actual, self.state())

    def state(self, uid=UID):
        return {table: sorted(self.db.query('SELECT * FROM ' + table + ' WHERE uid=?', (uid,)),
                              key=lambda row: json.dumps(row, sort_keys=True)) for table in TABLES}

    def balance(self, iid):
        return InventoryService.get_item_balance(self.db, UID, iid)[1]

    def used(self, iid=TARGET, uid=UID):
        return self.service._get_displace_used(self.db, uid, self.service._displace_day_key()).get(iid, 0)

    def counter(self, amount, iid=TARGET, uid=UID):
        self.db.execute('INSERT INTO trust_displace_counter(uid,item_id,day_key,use) VALUES(?,?,?,?) '
                        'ON CONFLICT(uid,item_id,day_key) DO UPDATE SET use=excluded.use',
                        (uid, iid, self.service._displace_day_key(), amount))

    def request(self, costs=None, rewards=None, ctx=None):
        ctx = ctx or self.ctx
        ctx.reset_operation_state()
        data = {'use_item_list': [{'id': SOURCE, 'num': 4}] if costs is None else costs,
                'reward_item_list': [{'id': TARGET, 'num': 1}] if rewards is None else rewards}
        response = Core()._run_operation(operations.ExChangeTrustItemOp(),
            CSRequest(73010, encode('cs_73010', data), uid=UID), ctx)
        return decode(next(f['payload'] for f in response.frames if f['sc'] == 73011), 'sc_73011')

    def rejected(self, costs=None, rewards=None, code=None):
        before = self.state()
        reply = self.request(costs, rewards)
        self.assertNotEqual(reply['result'], 0)
        if code is not None:
            self.assertEqual(reply['result'], code)
        self.assertEqual(self.state(), before)
        self.assertFalse(self.ctx.touched_items)

    def test_empty_cost_cannot_award_a_gift(self):
        self.rejected(costs=[], code=2)

    def test_empty_reward_cannot_consume_selected_gifts(self):
        self.rejected(rewards=[], code=2)

    def test_currency_reward_gift_and_unknown_ID_are_invalid_costs(self):
        for iid in (1, 30013, 99999999):
            with self.subTest(iid=iid):
                self.rejected(costs=[{'id': iid, 'num': 4}], code=2)

    def test_currency_archive_cost_gift_and_unknown_ID_are_invalid_targets(self):
        for iid in (1, SOURCE, 99999999):
            with self.subTest(iid=iid):
                self.rejected(rewards=[{'id': iid, 'num': 1}], code=2)

    def test_under_and_over_reported_costs_both_reject_without_spending(self):
        for amount in (3, 5):
            with self.subTest(amount=amount):
                self.rejected(costs=[{'id': SOURCE, 'num': amount}], code=2)

    def test_all_four_real_client_exchange_ratios(self):
        self.assertEqual(len(self.rules['cost_item_ids']), 64)
        for iid, cost in ((30012, 4), (30013, 8), (30014, 16), (30015, 32)):
            with self.subTest(iid=iid):
                before = self.balance(SOURCE)
                reply = self.request([{'id': SOURCE, 'num': cost}], [{'id': iid, 'num': 1}])
                self.assertEqual(reply['result'], 0)
                self.assertEqual(self.balance(SOURCE), before - cost)
                self.assertEqual(self.balance(iid), 1)
                self.assertEqual(self.used(iid), 1)

    def test_mixed_and_duplicate_cost_entries_have_one_combined_budget(self):
        reply = self.request([{'id': SOURCE, 'num': 1}, {'id': SECOND, 'num': 1}, {'id': SOURCE, 'num': 2}])
        self.assertEqual(reply['result'], 0)
        self.assertEqual((self.balance(SOURCE), self.balance(SECOND), self.balance(TARGET)), (997, 999, 1))

    def test_duplicate_target_entries_below_limit_award_the_combined_quantity(self):
        reply = self.request([{'id': SOURCE, 'num': 12}], [{'id': TARGET, 'num': 1}, {'id': TARGET, 'num': 2}])
        self.assertEqual(reply['result'], 0)
        self.assertEqual((self.balance(SOURCE), self.balance(TARGET), self.used()), (988, 3, 3))

    def test_split_targets_cannot_exceed_the_total_daily_limit(self):
        self.counter(29)
        self.rejected([{'id': SOURCE, 'num': 8}], [{'id': TARGET, 'num': 1}, {'id': TARGET, 'num': 1}], 2)

    def test_one_remaining_exchange_is_allowed_and_the_next_one_is_rejected(self):
        self.counter(29)
        self.assertEqual(self.request()['result'], 0)
        self.assertEqual(self.used(), 30)
        self.rejected(code=2)

    def test_multiple_targets_use_the_sum_of_their_client_costs(self):
        reply = self.request([{'id': SOURCE, 'num': 12}], [{'id': 30012, 'num': 1}, {'id': 30013, 'num': 1}])
        self.assertEqual(reply['result'], 0)
        self.assertEqual((self.balance(SOURCE), self.balance(30012), self.balance(30013)), (988, 1, 1))
        self.assertEqual((self.used(30012), self.used(30013)), (1, 1))

    def test_other_UIDs_daily_counter_does_not_affect_this_players_exchange(self):
        self.counter(30, uid=OTHER)
        self.other = self.state(OTHER)
        self.assertEqual(self.request()['result'], 0)
        self.assertEqual((self.used(), self.used(uid=OTHER)), (1, 30))

    def test_new_game_day_has_a_new_counter_without_deleting_history(self):
        self.counter(30)
        old_day = self.service._displace_day_key()
        self.clock.return_value = NOW + DAY
        self.assertEqual(self.request()['result'], 0)
        self.assertEqual(self.used(), 1)
        self.assertEqual(self.db.query('SELECT use FROM trust_displace_counter WHERE uid=? AND item_id=? AND day_key=?',
                                       (UID, TARGET, old_day))[0]['use'], 30)

    def test_duplicate_cost_shortage_direct_call_restores_the_first_debit(self):
        self.db.set_material(UID, SOURCE, 3)
        before = self.state()
        with self.assertRaises(OperationError) as caught:
            self.service.exchange_trust_item(self.ctx, UID,
                [{'id': SOURCE, 'num': 2}, {'id': SOURCE, 'num': 2}], [{'id': TARGET, 'num': 1}])
        self.assertEqual(caught.exception.code, 3)
        self.assertEqual(self.state(), before)
        self.assertFalse(self.ctx.touched_items)

    def test_nonpositive_boolean_and_fractional_direct_quantities_reject(self):
        for amount in (0, -1, True, 4.5):
            with self.subTest(amount=amount):
                before = self.state()
                with self.assertRaises(OperationError) as caught:
                    self.service.exchange_trust_item(self.ctx, UID,
                        [{'id': SOURCE, 'num': amount}], [{'id': TARGET, 'num': 1}])
                self.assertEqual(caught.exception.code, 2)
                self.assertEqual(self.state(), before)

    def test_direct_real_award_failure_restores_source_reward_and_notifications(self):
        before = self.state()
        original = inventory_service.grant_item
        seen = []

        def fail(*args, **kwargs):
            original(*args, **kwargs)
            seen.append(self.balance(TARGET))
            raise RuntimeError('injected after real exchange award')

        with patch.object(inventory_service, 'grant_item', side_effect=fail):
            with self.assertRaises(RuntimeError):
                self.service.exchange_trust_item(self.ctx, UID,
                    [{'id': SOURCE, 'num': 4}], [{'id': TARGET, 'num': 1}])
        self.assertEqual(seen, [1])
        self.assertEqual(self.state(), before)
        self.assertFalse(self.ctx.touched_items)

    def counter_fault(self):
        original = self.db.execute
        seen = []

        def fail(sql, *args, **kwargs):
            result = original(sql, *args, **kwargs)
            if sql.startswith('INSERT INTO trust_displace_counter'):
                seen.append((self.balance(SOURCE), self.balance(TARGET), self.used()))
                raise RuntimeError('injected after real counter write')
            return result

        return fail, seen

    def test_counter_write_failure_restores_all_three_parts_of_the_exchange(self):
        before = self.state()
        fail, seen = self.counter_fault()
        with patch.object(self.db, 'execute', side_effect=fail):
            reply = self.request()
        self.assertEqual(seen, [(996, 1, 1)])
        self.assertNotEqual(reply['result'], 0)
        self.assertEqual(self.state(), before)

    def test_caught_nested_exchange_failure_preserves_only_outer_caller_state(self):
        with self.db.transaction():
            self.db.set_currency(UID, 38, self.balance(38) + 1)
            caller = self.state()
            fail, seen = self.counter_fault()
            with patch.object(self.db, 'execute', side_effect=fail):
                try:
                    self.service.exchange_trust_item(self.ctx, UID,
                        [{'id': SOURCE, 'num': 4}], [{'id': TARGET, 'num': 1}])
                except RuntimeError:
                    pass
                else:
                    self.fail('counter failure was suppressed')
            self.assertEqual(seen, [(996, 1, 1)])
            self.assertEqual(self.state(), caller)
            self.assertFalse(self.ctx.touched_items)

    def test_required_reply_failure_restores_exchange_then_retry_awards_once(self):
        before = self.state()
        original = self.ctx.codec_encode

        def fail(name, data):
            if name == 'sc_73011':
                raise ValueError('exchange reply encoding fault')
            return original(name, data)

        with patch.object(self.ctx, 'codec_encode', fail):
            reply = self.request()
        self.assertNotEqual(reply['result'], 0)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.request()['result'], 0)
        self.assertEqual((self.balance(SOURCE), self.balance(TARGET), self.used()), (996, 1, 1))

    def test_missing_and_malformed_rule_exports_reject_without_asset_changes(self):
        for rule in (OperationError(406, 'missing rules'), {'schema_version': 1, 'cost_item_ids': [], 'target_items': {}}):
            with self.subTest(rule=type(rule).__name__):
                patcher = (patch('client_rule_loader.load_rules', side_effect=rule) if isinstance(rule, Exception)
                           else patch('client_rule_loader.load_rules', return_value=rule))
                with patcher:
                    self.rejected(code=406)

    def test_concurrent_last_allowed_exchange_cannot_pass_the_daily_limit_twice(self):
        self.counter(29)
        barrier = threading.Barrier(2)

        def exchange():
            ctx = context(self.db)
            ctx.uid = UID
            ctx.log = lambda *a, **kw: None
            try:
                barrier.wait(timeout=10)
                return self.request(ctx=ctx)
            finally:
                self.db.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: exchange(), range(2)))
        self.assertEqual(sum(r['result'] == 0 for r in results), 1, results)
        self.assertEqual((self.balance(SOURCE), self.balance(TARGET), self.used()), (996, 1, 30))
        self.rejected(code=2)


if __name__ == '__main__':
    unittest.main()
