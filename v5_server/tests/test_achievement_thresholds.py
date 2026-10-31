"""A139: current thresholds, retained progress/claims, and completion timestamps."""
from contextlib import ExitStack
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
SANDBOX = tempfile.TemporaryDirectory(prefix='ag-achievement-threshold-tests-')
BOOT = Path(SANDBOX.name) / 'bootstrap.db'
shutil.copyfile(SEED, BOOT)
os.environ['ACCOUNT_DB_PATH'] = str(BOOT)

from account_db import AccountDB
import achievement_service as achievements
from codec import decode, encode
from core import Core, OperationError
import event_bus
from gm_actions import context
from inventory_service import InventoryService
from middleware import CSRequest

UID, OTHER, AID = 2174928301, 264084870, 10102
NOW, MAGIC = 1793275200, 1786411503
TABLES = ('users', 'currency', 'material', 'hero', 'hero_piece', 'achievement')


class ThresholdTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=SANDBOX.name)
        self.addCleanup(self.folder.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('time.time', return_value=NOW))
        self.stack.enter_context(patch.object(event_bus.EventBus, '_handlers', {}))
        self.stack.enter_context(patch('event_bus.bus.emit'))
        self.stack.enter_context(patch('game_cycles.apply_rotation'))
        self.stack.enter_context(patch.object(achievements.AchievementService, '_instance', None))
        self.path = Path(self.folder.name) / 'player.db'
        shutil.copyfile(SEED, self.path)
        self.db = AccountDB(str(self.path))
        self.addCleanup(self.db.close)
        self.ctx = context(self.db)
        self.ctx.uid = UID
        self.ctx.log = lambda *a, **kw: None
        self.service = achievements.AchievementService.get_instance(self.db)
        self.service.init_user_achievements(UID)
        self.cfg = achievements._CFG[AID]
        self.need = int(self.cfg['need'])
        self.assertEqual(self.need, 3)
        self.other = self.state(OTHER)

    def tearDown(self):
        self.assertEqual(self.state(OTHER), self.other)
        self.assertEqual(self.db.query('PRAGMA integrity_check')[0]['integrity_check'], 'ok')
        current = self.row()
        with sqlite3.connect('file:' + str(self.path) + '?mode=ro', uri=True) as reopened:
            reopened.row_factory = sqlite3.Row
            self.assertEqual(dict(reopened.execute('SELECT * FROM achievement WHERE uid=? AND achievement_id=?',
                                                    (UID, AID)).fetchone()), current)

    def state(self, uid=UID):
        return {table: sorted(self.db.query('SELECT * FROM ' + table + ' WHERE uid=?', (uid,)),
                              key=lambda row: json.dumps(row, sort_keys=True)) for table in TABLES}

    def row(self):
        return self.db.query('SELECT * FROM achievement WHERE uid=? AND achievement_id=?', (UID, AID))[0]

    def prepare(self, need, progress, claimed=0, timestamp=0):
        self.db.execute('UPDATE achievement SET need=?,progress=?,complete_flag=?,achieve_time=? WHERE uid=? AND achievement_id=?',
                        (need, progress, claimed, timestamp, UID, AID))

    def claim(self, ids=None):
        with patch.object(self.service, 'reconcile_achievements'):
            response = Core()._run_operation(achievements.AchievementRewardOp(),
                CSRequest(53004, encode('cs_53004', {'id': ids or [AID]}), uid=UID), self.ctx)
        return decode(next(f['payload'] for f in response.frames if f['sc'] == 53005), 'sc_53005')

    def rewards(self):
        return {int(pair[0]): InventoryService.get_item_balance(self.db, UID, int(pair[0]))[1]
                for pair in self.cfg['reward']}

    def test_initialization_raises_old_threshold_and_clears_unearned_completion_time(self):
        self.prepare(1, 1, timestamp=1234)
        self.service.init_user_achievements(UID)
        row = self.row()
        self.assertEqual((row['need'], row['progress'], row['complete_flag'], row['achieve_time']), (3, 1, 0, 0))

    def test_initialization_lowers_old_threshold_and_records_newly_ready_time(self):
        self.prepare(13, 3)
        self.service.init_user_achievements(UID)
        row = self.row()
        self.assertEqual((row['need'], row['progress'], row['achieve_time']), (3, 3, NOW))

    def test_claim_cannot_use_a_stale_lower_threshold_without_initialization(self):
        self.prepare(1, 1)
        before = self.state()
        reply = self.claim()
        self.assertEqual(reply['result'], 2)
        self.assertEqual(self.state(), before)

    def test_claim_uses_current_threshold_even_when_old_row_has_a_higher_need(self):
        self.prepare(13, 3)
        before = self.rewards()
        reply = self.claim()
        self.assertEqual(reply['result'], 0)
        self.assertEqual((self.row()['need'], self.row()['complete_flag'], self.row()['achieve_time']), (3, 1, NOW))
        for iid, num in self.cfg['reward']:
            self.assertEqual(InventoryService.get_item_balance(self.db, UID, iid)[1] - before[iid], num)

    def test_claimed_history_and_real_timestamp_survive_a_threshold_increase(self):
        self.prepare(1, 1, claimed=1, timestamp=1234)
        self.service.init_user_achievements(UID)
        before = self.state()
        self.assertEqual(self.row()['need'], 3)
        reply = self.claim()
        self.assertEqual(reply['result'], 0)
        self.assertFalse(reply.get('reward_list'))
        self.assertEqual(self.state(), before)
        self.assertEqual((self.row()['complete_flag'], self.row()['achieve_time']), (1, 1234))

    def test_existing_ready_progress_and_real_timestamp_are_preserved(self):
        self.prepare(13, 20, timestamp=1234)
        self.service.init_user_achievements(UID)
        self.assertEqual((self.row()['need'], self.row()['progress'], self.row()['achieve_time']), (3, 20, 1234))

    def test_login_does_not_turn_a_legacy_timestamp_into_unearned_completion(self):
        self.prepare(1, 1, timestamp=MAGIC)
        with patch.object(self.service, 'reconcile_achievements'):
            payload = decode(self.service.get_achievement_login_payload(UID), 'sc_53001')
        row = next(r for r in payload['achievement_list'] if r['id'] == AID)
        self.assertEqual((row['progress'], row['achieve_time'], row['complete_flag']), (1, 0, 0))

    def test_login_preserves_a_claimed_timestamp_instead_of_replacing_it_with_today(self):
        self.prepare(1, 1, claimed=1, timestamp=MAGIC)
        with patch.object(self.service, 'reconcile_achievements'):
            payload = decode(self.service.get_achievement_login_payload(UID), 'sc_53001')
        row = next(r for r in payload['achievement_list'] if r['id'] == AID)
        self.assertEqual((row['complete_flag'], row['achieve_time']), (1, MAGIC))

    def test_reconcile_cleanup_first_uses_the_current_threshold(self):
        self.prepare(1, 1, timestamp=1234)
        with patch.dict(achievements._CFG_BY_COND, {}, clear=True):
            self.service.reconcile_achievements(UID)
        self.assertEqual((self.row()['need'], self.row()['progress'], self.row()['achieve_time']), (3, 1, 0))

    def test_event_progress_updates_the_stored_threshold_and_completion_time(self):
        self.prepare(13, 2)
        condition = int(self.cfg['condition'])
        parameter = self.cfg.get('parameter')
        parameter = parameter if parameter and parameter != [0] else None
        with patch.dict(achievements._CFG_BY_COND, {condition: [self.cfg]}, clear=True):
            self.service.trigger_condition(UID, condition, delta=1, param=parameter, ctx=self.ctx)
        self.assertEqual((self.row()['need'], self.row()['progress'], self.row()['achieve_time']), (3, 3, NOW))

    def test_initialization_is_idempotent_after_threshold_sync(self):
        self.prepare(13, 3)
        self.service.init_user_achievements(UID)
        before = self.state()
        self.service.init_user_achievements(UID)
        self.assertEqual(self.state(), before)

    def test_unknown_old_achievement_row_is_not_rewritten(self):
        self.db.execute('INSERT INTO achievement(uid,achievement_id,need,progress,complete_flag,achieve_time) VALUES(?,?,?,?,?,?)',
                        (UID, 99999999, 77, 88, 1, 1234))
        self.service.init_user_achievements(UID)
        self.assertEqual(self.db.query('SELECT need,progress,complete_flag,achieve_time FROM achievement WHERE uid=? AND achievement_id=99999999', (UID,))[0],
                         {'need': 77, 'progress': 88, 'complete_flag': 1, 'achieve_time': 1234})

    def test_mixed_invalid_request_does_not_update_or_claim_its_ready_member(self):
        self.prepare(13, 3)
        before = self.state()
        reply = self.claim([AID, 99999999])
        self.assertEqual(reply['result'], 2)
        self.assertEqual(self.state(), before)

    def test_real_award_failure_restores_threshold_time_flags_and_assets(self):
        self.prepare(3, 3)
        before = self.state()
        original = InventoryService.grant_items
        written = []

        def fail(*args, **kwargs):
            original(*args, **kwargs)
            written.append(self.rewards())
            raise RuntimeError('injected after real achievement award')

        with patch.object(InventoryService, 'grant_items', side_effect=fail):
            reply = self.claim()
        self.assertEqual(reply['result'], 9)
        self.assertTrue(written)
        self.assertEqual(self.state(), before)
        self.assertFalse(self.ctx.touched_items)

    def test_caught_nested_claim_failure_preserves_only_the_callers_writes(self):
        self.prepare(3, 3)
        with self.db.transaction():
            self.db.set_currency(UID, 38, InventoryService.get_item_balance(self.db, UID, 38)[1] + 1)
            caller = self.state()
            with patch.object(InventoryService, 'grant_items', side_effect=RuntimeError('award fault')):
                try:
                    self.service.claim_achievements(UID, [AID], self.ctx)
                except RuntimeError:
                    pass
                else:
                    self.fail('award error was hidden')
            self.assertEqual(self.state(), caller)

    def test_missing_known_achievement_rows_are_initialized_with_current_config(self):
        self.db.execute('DELETE FROM achievement WHERE uid=? AND achievement_id=?', (UID, AID))
        self.service.init_user_achievements(UID)
        self.assertEqual((self.row()['need'], self.row()['progress'], self.row()['complete_flag']), (3, 0, 0))

    def test_stale_initialization_snapshot_cannot_overwrite_a_concurrent_claim_time(self):
        self.prepare(1, 1, timestamp=1234)
        original = self.db.query
        injected = []

        def query(sql, *args, **kwargs):
            rows = original(sql, *args, **kwargs)
            if not injected and sql.startswith('SELECT achievement_id,need,progress,complete_flag,achieve_time'):
                with sqlite3.connect(str(self.path)) as other:
                    other.execute('UPDATE achievement SET need=3,progress=3,complete_flag=1,achieve_time=5678 WHERE uid=? AND achievement_id=?', (UID, AID))
                injected.append(True)
            return rows

        with patch.object(self.db, 'query', side_effect=query):
            self.service.init_user_achievements(UID)
        self.assertEqual(injected, [True])
        self.assertEqual((self.row()['need'], self.row()['progress'], self.row()['complete_flag'], self.row()['achieve_time']),
                         (3, 3, 1, 5678))

    def test_event_snapshot_cannot_overwrite_a_concurrent_claim_time(self):
        self.prepare(3, 2)
        original = self.db.query
        injected = []

        def query(sql, *args, **kwargs):
            rows = original(sql, *args, **kwargs)
            if not injected and sql.startswith('SELECT achievement_id, progress, need, complete_flag, achieve_time'):
                with sqlite3.connect(str(self.path)) as other:
                    other.execute('UPDATE achievement SET progress=3,complete_flag=1,achieve_time=5678 WHERE uid=? AND achievement_id=?', (UID, AID))
                injected.append(True)
            return rows

        condition = int(self.cfg['condition'])
        parameter = self.cfg.get('parameter')
        parameter = parameter if parameter and parameter != [0] else None
        with patch.object(self.db, 'query', side_effect=query), patch.dict(achievements._CFG_BY_COND, {condition: [self.cfg]}, clear=True):
            changes = self.service.trigger_condition(UID, condition, delta=1, param=parameter, ctx=self.ctx)
        self.assertEqual(injected, [True])
        self.assertEqual((self.row()['progress'], self.row()['complete_flag'], self.row()['achieve_time']), (3, 1, 5678))
        self.assertEqual(changes, [])


if __name__ == '__main__':
    unittest.main()
