"""A52/A58/A152: recovery clocks, hero rewards and explicit loading settings."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch
import zlib

SEED = Path(os.environ['GM_TEST_DB_SEED']).resolve()
SANDBOX = tempfile.TemporaryDirectory(prefix='ag-edge-state-tests-')
BOOT = Path(SANDBOX.name) / 'bootstrap.db'
shutil.copyfile(SEED, BOOT)
os.environ['ACCOUNT_DB_PATH'] = str(BOOT)

from account_db import AccountDB
from codec import decode, encode
from core import Core
import event_bus
import fatigue_service
import generator
from gm_actions import context
from inventory_service import InventoryService
import login
from middleware import CSRequest
import operations

UID, OTHER = 2174928301, 264084870
NOW = 1793275200
TABLES = ('users', 'currency', 'material', 'hero', 'hero_piece', 'loading_set')


class EdgeStateTests(unittest.TestCase):
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
        self.other = self.state(OTHER)

    def state(self, uid=UID):
        return {table: sorted(self.db.query('SELECT * FROM ' + table + ' WHERE uid=?', (uid,)),
                              key=lambda row: json.dumps(row, sort_keys=True)) for table in TABLES}

    def tearDown(self):
        self.assertEqual(self.state(OTHER), self.other)
        self.assertEqual(self.db.query('PRAGMA integrity_check')[0]['integrity_check'], 'ok')
        with sqlite3.connect('file:' + str(self.path) + '?mode=ro', uri=True) as saved:
            saved.row_factory = sqlite3.Row
            state = {table: sorted([dict(r) for r in saved.execute(
                'SELECT * FROM ' + table + ' WHERE uid=?', (UID,))],
                key=lambda row: json.dumps(row, sort_keys=True)) for table in TABLES}
        self.assertEqual(state, self.state())

    def request(self, operation, data):
        response = Core()._run_operation(operation(), CSRequest(operation.cmd,
            encode('cs_' + str(operation.cmd), data), uid=UID), self.ctx)
        replies = [f for f in response.frames if f['sc'] == operation.sc]
        self.assertEqual(len(replies), 1)
        return decode(replies[0]['payload'], 'sc_' + str(operation.sc)), response.frames

    def set_fatigue(self, num, last):
        self.db.execute('UPDATE currency SET num=?,last_fatigue_recover_time=?,update_ts=1 '
                        'WHERE uid=? AND id=4', (num, last, UID))

    def fatigue(self):
        return self.db.query('SELECT num,last_fatigue_recover_time,update_ts FROM currency '
                             'WHERE uid=? AND id=4', (UID,))[0]

    def tick(self, now=NOW):
        conn = Mock()
        with patch('fatigue_service.time.time', return_value=now):
            pushed = fatigue_service.check_and_push_fatigue(conn, UID, self.db, encode)
        frames = [(args[0], decode(args[1], 'sc_' + str(args[0])))
                  for args, _ in conn.send_push.call_args_list]
        return pushed, frames

    def test_clock_rollback_persists_the_corrected_anchor_without_granting_fatigue(self):
        self.set_fatigue(50, NOW + 86400)
        self.assertEqual(fatigue_service.settle_fatigue(self.db, UID, NOW), (50, NOW))
        self.assertEqual(self.fatigue(), {'num': 50, 'last_fatigue_recover_time': NOW, 'update_ts': NOW})
        self.assertEqual(fatigue_service.settle_fatigue(self.db, UID, NOW), (50, NOW))

    def test_recovery_resumes_one_interval_after_a_clock_rollback(self):
        self.set_fatigue(50, NOW + 86400)
        fatigue_service.settle_fatigue(self.db, UID, NOW)
        self.assertEqual(fatigue_service.settle_fatigue(self.db, UID, NOW + 359), (50, NOW))
        self.assertEqual(fatigue_service.settle_fatigue(self.db, UID, NOW + 360), (51, NOW + 360))

    def test_online_ticker_corrects_a_future_anchor_and_pushes_only_the_clock(self):
        self.set_fatigue(50, NOW + 86400)
        self.assertEqual(self.tick(), (True, [(17025, {'last_fatigue_recover_time': NOW})]))
        self.assertEqual(self.fatigue()['last_fatigue_recover_time'], NOW)
        self.assertEqual(self.tick(), (False, []))
        self.assertEqual(self.tick(NOW + 360), (True, [
            (17023, {'normal_items': [{'id': 4, 'num': 51}]}),
            (17025, {'last_fatigue_recover_time': NOW + 360})]))

    def test_normal_recovery_retains_partial_seconds(self):
        self.set_fatigue(50, NOW - 725)
        self.assertEqual(fatigue_service.settle_fatigue(self.db, UID, NOW), (52, NOW - 5))
        self.assertEqual(fatigue_service.settle_fatigue(self.db, UID, NOW + 354), (52, NOW - 5))

    def test_full_or_overflow_fatigue_never_gets_clock_rollback_rewards(self):
        for num in (240, 500):
            with self.subTest(num=num):
                self.set_fatigue(num, NOW + 86400)
                self.assertEqual(fatigue_service.settle_fatigue(self.db, UID, NOW), (num, 0))
                self.assertEqual(self.tick(), (False, []))

    def test_failed_clock_write_does_not_send_a_successful_ticker_refresh(self):
        self.set_fatigue(50, NOW + 86400)
        before = self.fatigue()
        with patch.object(self.db, 'execute', side_effect=sqlite3.OperationalError('write failed')):
            self.assertEqual(self.tick(), (False, []))
        self.assertEqual(self.fatigue(), before)

    @staticmethod
    def box(count=1, choice=1):
        return {'item_info': {'id': 30021, 'num': count, 'time_valid': 0}, 'use_list': [choice]}

    def use_heroes(self, entries, locked=(1011,)):
        self.db.set_material(UID, 30021, 10)
        for hero in locked:
            self.db.execute('UPDATE hero SET unlock=0 WHERE uid=? AND id=?', (UID, hero))
        before = {hero: InventoryService.get_item_balance(self.db, UID, 10000 + hero)[1]
                  for hero in (1011, 1019)}
        reply, frames = self.request(operations.UseItemOp, {'use_item_list': entries})
        return reply, frames, before

    def test_two_identical_new_heroes_display_one_unlock_and_one_conversion(self):
        reply, frames, before = self.use_heroes([self.box(2)])
        self.assertEqual(reply['result'], 0)
        self.assertEqual({r['id']: r['num'] for r in reply['drop_list']}, {1011: 1, 11011: 18})
        self.assertEqual(InventoryService.get_item_balance(self.db, UID, 11011)[1] - before[1011], 18)
        self.assertEqual(InventoryService.get_item_balance(self.db, UID, 1011)[1], 1)
        self.assertEqual(InventoryService.get_item_balance(self.db, UID, 30021)[1], 8)
        self.assertLess([f['sc'] for f in frames].index(14007), [f['sc'] for f in frames].index(17013))

    def test_multiple_entries_keep_the_unlock_while_combining_duplicate_pieces(self):
        reply, _, before = self.use_heroes([self.box(), self.box(2)])
        self.assertEqual({r['id']: r['num'] for r in reply['drop_list']}, {1011: 1, 11011: 36})
        self.assertEqual(InventoryService.get_item_balance(self.db, UID, 11011)[1] - before[1011], 36)

    def test_owned_heroes_display_only_the_actual_converted_pieces(self):
        reply, _, before = self.use_heroes([self.box(3)], locked=())
        self.assertEqual({r['id']: r['num'] for r in reply['drop_list']}, {11011: 54})
        self.assertEqual(InventoryService.get_item_balance(self.db, UID, 11011)[1] - before[1011], 54)

    def test_different_new_heroes_keep_each_unlock_and_conversion(self):
        reply, _, before = self.use_heroes([self.box(2), self.box(2, 2)], locked=(1011, 1019))
        self.assertEqual({r['id']: r['num'] for r in reply['drop_list']},
                         {1011: 1, 11011: 18, 1019: 1, 11019: 18})
        for hero in (1011, 1019):
            self.assertEqual(InventoryService.get_item_balance(self.db, UID, 10000 + hero)[1]
                             - before[hero], 18)

    def test_reward_refresh_failure_rolls_back_unlock_conversion_and_box_cost(self):
        self.db.set_material(UID, 30021, 10)
        self.db.execute('UPDATE hero SET unlock=0 WHERE uid=? AND id=1011', (UID,))
        before = self.state()
        with patch('hero_codec.build_single_hero_frame', side_effect=RuntimeError('encode failed')):
            reply, frames = self.request(operations.UseItemOp, {'use_item_list': [self.box(2)]})
        self.assertNotEqual(reply['result'], 0)
        self.assertFalse(reply.get('drop_list'))
        self.assertEqual(self.state(), before)
        self.assertNotIn(14007, [f['sc'] for f in frames])

    def loading_ids(self):
        return decode(generator.gen_payload(52021, uid=UID, db=self.db), 'sc_52021').get('id_list', [])

    def test_saving_empty_loading_settings_stays_empty_after_read_and_reopen(self):
        reply, _ = self.request(operations.SaveLoadingSetOp, {'all_id_list': []})
        self.assertEqual(reply['result'], 0)
        self.assertEqual(self.loading_ids(), [])
        with sqlite3.connect('file:' + str(self.path) + '?mode=ro', uri=True) as saved:
            self.assertEqual(saved.execute('SELECT id_list FROM loading_set WHERE uid=?', (UID,)).fetchone()[0], '[]')

    def test_removing_the_last_loading_picture_keeps_the_explicit_empty_list(self):
        self.request(operations.SaveLoadingSetOp, {'all_id_list': [2109401]})
        reply, _ = self.request(operations.ChangeLoadingSetOp, {'type': 0, 'id': 2109401})
        self.assertEqual(reply['result'], 0)
        self.assertEqual(self.loading_ids(), [])

    def test_nonempty_loading_selection_keeps_its_saved_order(self):
        ids = [2109403, 2109401]
        self.request(operations.SaveLoadingSetOp, {'all_id_list': ids})
        self.assertEqual(self.loading_ids(), ids)

    def test_missing_legacy_settings_still_use_the_existing_default(self):
        self.db.execute('DELETE FROM loading_set WHERE uid=?', (UID,))
        self.assertEqual(self.loading_ids(), [2109401, 2109403, 2109402])

    def test_missing_fresh_settings_do_not_inherit_legacy_pictures(self):
        self.db.execute('DELETE FROM loading_set WHERE uid=?', (UID,))
        with patch('generator.is_fresh', return_value=True):
            self.assertEqual(self.loading_ids(), [])

    def test_corrupt_saved_json_uses_the_previous_default_without_rewriting_the_row(self):
        self.db.execute('UPDATE loading_set SET id_list=? WHERE uid=?', ('{broken', UID))
        self.assertEqual(self.loading_ids(), [2109401, 2109403, 2109402])
        self.assertEqual(self.db.query('SELECT id_list FROM loading_set WHERE uid=?', (UID,))[0]['id_list'], '{broken')

    def test_login_sends_an_explicit_empty_loading_frame_instead_of_recorded_pictures(self):
        self.request(operations.SaveLoadingSetOp, {'all_id_list': []})
        rows = login.load_login_push(self.db)
        loading_row = next(row for row in rows if row[1] == 52021)
        self.assertEqual(loading_row[4], 1)
        sock = Mock()
        with patch('login.load_login_push', return_value=[loading_row]), \
                patch('lazy_timer.lazy_timer.pulse'):
            login.send_login_flood(sock, self.db, UID, generator, frame_delay=0)
        packet = sock.sendall.call_args_list[0].args[0]
        self.assertEqual(struct.unpack('>H', packet[:2])[0], len(packet) - 2)
        payload = zlib.decompress(packet[11:]) if packet[2] else packet[11:]
        self.assertEqual(decode(payload, 'sc_52021').get('id_list', []), [])


if __name__ == '__main__':
    unittest.main()
