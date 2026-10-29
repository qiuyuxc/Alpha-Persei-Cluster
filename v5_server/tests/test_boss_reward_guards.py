"""A79/A80/A83: real dream-reconstruction claims, records and resets."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from copy import deepcopy
import datetime
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
SANDBOX = tempfile.TemporaryDirectory(prefix='ag-dream-reward-tests-')
BOOT = Path(SANDBOX.name) / 'bootstrap.db'
shutil.copyfile(SEED, BOOT)
os.environ['ACCOUNT_DB_PATH'] = str(BOOT)

from account_db import AccountDB
from codec import decode, encode
from core import Core, OperationError
import event_bus
from gm_actions import context
from inventory_service import InventoryService
from middleware import CSRequest, DownFrame
from operation_notifications import checkpoint_notifications
import operations
import weekly_challenge_service as weekly

UID, OTHER = 2174928301, 264084870
NOW = int(datetime.datetime(2026, 10, 29, 12).timestamp())
BOSS_TABLES = ('boss_challenge_progress', 'boss_challenge_normal', 'boss_challenge_advance',
               'boss_challenge_scores', 'boss_challenge_claimed_rewards', 'boss_challenge_affixes')
TABLES = ('users', 'currency', 'material', 'hero', 'hero_piece', 'claim_ledger') + BOSS_TABLES


class BossRewardTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=SANDBOX.name)
        self.addCleanup(self.folder.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('time.time', return_value=NOW))
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
        self.db.check_and_lazy_refresh_boss_challenge(UID, NOW)
        for table in BOSS_TABLES[1:]:
            self.db.execute('DELETE FROM ' + table + ' WHERE uid=?', (UID,))
        self.db.set_boss_challenge_mode(UID, 102)
        for iid in (1, 37, 41, 42):
            self.db.set_currency(UID, iid, 100)
        self.catalog = weekly.get_boss_catalog()
        self.bosses = [row['id'] for row in weekly.get_boss_challenge_data(UID, self.db)[2]['boss_list']]
        self.other = self.state(OTHER)

    def tearDown(self):
        self.assertEqual(self.state(OTHER), self.other)
        self.assertEqual(self.db.query('PRAGMA integrity_check')[0]['integrity_check'], 'ok')
        with sqlite3.connect('file:' + str(self.path) + '?mode=ro', uri=True) as saved:
            saved.row_factory = sqlite3.Row
            state = {table: sorted([dict(row) for row in saved.execute(
                'SELECT * FROM ' + table + ' WHERE uid=?', (UID,))],
                key=lambda row: json.dumps(row, sort_keys=True)) for table in TABLES}
        self.assertEqual(state, self.state())

    def state(self, uid=UID):
        return {table: sorted(self.db.query('SELECT * FROM ' + table + ' WHERE uid=? /*snapshot*/', (uid,)),
                              key=lambda row: json.dumps(row, sort_keys=True)) for table in TABLES}

    def balance(self, iid):
        return InventoryService.get_item_balance(self.db, UID, iid)[1]

    def request(self, cls, data, ctx=None):
        ctx = ctx or self.ctx
        before = self.state()
        balances = {iid: self.balance(iid) for iid in (1, 37, 41, 42)}
        response = Core()._run_operation(cls(), CSRequest(
            cls.cmd, encode('cs_' + str(cls.cmd), data), uid=UID), ctx)
        replies = [frame for frame in response.frames if frame['sc'] == cls.sc]
        self.assertEqual(len(replies), 1)
        reply = decode(replies[0]['payload'], 'sc_' + str(cls.sc))
        if os.environ.get('GM_WEEKLY_TEST_EVIDENCE'):
            record = {'test': self.id(), 'cmd': cls.cmd, 'request': data, 'reply': reply,
                      'asset_delta': {iid: self.balance(iid) - value for iid, value in balances.items()},
                      'state_unchanged': self.state() == before,
                      'frames': [frame['sc'] for frame in response.frames]}
            with open(os.environ['GM_WEEKLY_TEST_EVIDENCE'], 'a', encoding='utf-8') as output:
                output.write(json.dumps(record, ensure_ascii=False) + '\n')
        return reply, response.frames

    def rejected(self, cls, data, code=None):
        before = self.state()
        self.ctx.touched_items.add(38)
        self.ctx.append_frame(DownFrame(10051, b'\x08\x01'))
        checkpoint = checkpoint_notifications(self.ctx)
        reply, frames = self.request(cls, data)
        self.assertNotEqual(reply['result'], 0, reply)
        if code is not None:
            self.assertEqual(reply['result'], code)
        self.assertFalse(reply.get('item_list'))
        self.assertEqual(self.state(), before)
        after = checkpoint_notifications(self.ctx)
        for name in checkpoint.keys() - {'pending_frames'}:
            self.assertEqual(after[name], checkpoint[name])
        self.assertFalse(self.ctx.pending_frames)
        self.assertEqual(sum(frame['sc'] == 10051 and frame['payload'] == b'\x08\x01'
                             for frame in frames), 1)

    def score(self, amount=13000, boss=None):
        self.db.record_boss_challenge_advance_clear(UID, boss or self.bosses[0], amount,
                                                   heroes=[1066, 1093, 1058])

    def normal_clear(self):
        self.db.set_boss_challenge_mode(UID, 4)
        group = weekly.get_boss_challenge_data(UID, self.db)[1]['boss_challenge_list'][0]['group_id']
        stage = self.catalog['stage_groups'][str(group)]['stages'][0]
        self.db.record_boss_challenge_normal_clear(UID, group, stage,
            star_list=[{'star_id': i, 'is_achieve': 1} for i in (1, 2, 3)], heroes=[1066])
        return group, stage

    def failing_query(self, table):
        original = self.db.query

        def fail(sql, *args, **kwargs):
            if table in sql and '/*snapshot*/' not in sql:
                raise sqlite3.OperationalError('injected dream read failure')
            return original(sql, *args, **kwargs)

        return patch.object(self.db, 'query', side_effect=fail)

    def test_point_reward_below_threshold_cannot_award(self):
        self.score(9999)
        self.rejected(operations.BossChallengeClaimPointRewardOp, {'point': 10000})

    def test_star_reward_without_achievements_cannot_award(self):
        self.db.set_boss_challenge_mode(UID, 4)
        self.rejected(operations.BossChallengeClaimStarRewardOp, {'star': 3})

    def test_unknown_point_and_star_tiers_have_no_default_rewards(self):
        self.score(50000)
        self.rejected(operations.BossChallengeClaimPointRewardOp, {'point': 1})
        self.db.set_boss_challenge_mode(UID, 4)
        self.rejected(operations.BossChallengeClaimStarRewardOp, {'star': 1})

    def test_reward_cannot_be_borrowed_from_another_advance_mode(self):
        self.db.set_boss_challenge_mode(UID, 101)
        self.rejected(operations.BossChallengeClaimPointRewardOp, {'point': 10000})

    def test_single_point_retry_awards_only_once(self):
        self.score(10000)
        before = {iid: self.balance(iid) for iid in (1, 41, 37)}
        first, frames = self.request(operations.BossChallengeClaimPointRewardOp, {'point': 10000})
        self.assertEqual(first['result'], 0)
        self.assertEqual({r['id']: r['num'] for r in first['item_list']}, {1: 50, 41: 8, 37: 26})
        second, _ = self.request(operations.BossChallengeClaimPointRewardOp, {'point': 10000})
        self.assertEqual(second['result'], 0)
        self.assertFalse(second.get('item_list'))
        self.assertEqual({iid: self.balance(iid) - value for iid, value in before.items()},
                         {1: 50, 41: 8, 37: 26})
        self.assertLess([f['sc'] for f in frames].index(45101), [f['sc'] for f in frames].index(45103))

    def test_normal_star_retry_and_server_reward_quantities(self):
        self.normal_clear()
        before = {iid: self.balance(iid) for iid in (41, 37)}
        first, _ = self.request(operations.BossChallengeClaimStarRewardOp, {'star': 3})
        self.assertEqual(first['result'], 0)
        self.assertEqual({r['id']: r['num'] for r in first['item_list']}, {41: 4, 37: 12})
        second, _ = self.request(operations.BossChallengeClaimStarRewardOp, {'star': 3})
        self.assertEqual(second['result'], 0)
        self.assertFalse(second.get('item_list'))
        self.assertEqual({iid: self.balance(iid) - value for iid, value in before.items()}, {41: 4, 37: 12})

    def test_legacy_integer_star_lists_still_qualify_and_encode_correctly(self):
        group, stage = self.normal_clear()
        self.db.execute('UPDATE boss_challenge_normal SET star_info=? WHERE uid=? AND group_id=?',
                        (json.dumps([{'stage_id': stage, 'star_list': [1, 1, 1]}]), UID, group))
        reply, frames = self.request(operations.BossChallengeClaimStarRewardOp, {'star': 3})
        self.assertEqual(reply['result'], 0)
        data = decode(next(f['payload'] for f in frames if f['sc'] == 45001), 'sc_45001')
        boss = next(row for row in data['boss_challenge_list'] if row['group_id'] == group)
        self.assertEqual(boss['star_info'][0]['star_list'], [1, 1, 1])

    def test_single_then_bulk_and_bulk_retry_share_the_same_records(self):
        self.score(13000)
        before = {iid: self.balance(iid) for iid in (1, 41, 37)}
        self.assertEqual(self.request(operations.BossChallengeClaimPointRewardOp, {'point': 10000})[0]['result'], 0)
        bulk, _ = self.request(operations.BossChallengeClaimAllPointRewardOp, {})
        self.assertEqual(bulk['result'], 0)
        self.assertEqual({r['id']: r['num'] for r in bulk['item_list']}, {1: 50, 41: 8, 37: 26})
        again, _ = self.request(operations.BossChallengeClaimAllPointRewardOp, {})
        self.assertEqual(again['result'], 0)
        self.assertFalse(again.get('item_list'))
        self.assertEqual({iid: self.balance(iid) - value for iid, value in before.items()}, {1: 100, 41: 16, 37: 52})

    def test_bulk_then_single_cannot_award_a_tier_again(self):
        self.score(13000)
        self.assertEqual(self.request(operations.BossChallengeClaimAllPointRewardOp, {})[0]['result'], 0)
        before = self.state()
        reply, _ = self.request(operations.BossChallengeClaimPointRewardOp, {'point': 10000})
        self.assertEqual(reply['result'], 0)
        self.assertFalse(reply.get('item_list'))
        self.assertEqual(self.state(), before)

    def test_mode_101_bulk_uses_saved_advance_id_and_its_7000_point_tier(self):
        self.db.set_boss_challenge_mode(UID, 101)
        boss = weekly.get_boss_challenge_data(UID, self.db)[2]['boss_list'][0]['id']
        self.score(7000, boss)
        reply, _ = self.request(operations.BossChallengeClaimAllPointRewardOp, {})
        self.assertEqual(reply['result'], 0)
        self.assertEqual({r['id']: r['num'] for r in reply['item_list']}, {1: 50, 41: 7, 37: 24})

    def test_failed_bulk_history_read_cannot_repeat_claimed_rewards(self):
        self.score(13000)
        self.db.claim_boss_challenge_reward(UID, 2, 10000)
        with self.failing_query('boss_challenge_claimed_rewards'):
            self.rejected(operations.BossChallengeClaimAllPointRewardOp, {})

    def test_failed_score_read_returns_error_instead_of_success_with_empty_state(self):
        self.score(13000)
        with self.failing_query('boss_challenge_advance'):
            self.rejected(operations.BossChallengeClaimAllPointRewardOp, {})

    def test_missing_reward_catalog_returns_failure_without_default_awards(self):
        self.score(13000)
        with patch.object(weekly, 'get_boss_catalog', return_value={}):
            self.rejected(operations.BossChallengeClaimPointRewardOp, {'point': 10000}, 406)

    def test_scores_outside_the_current_roster_cannot_supply_reward_eligibility(self):
        self.score(50000, 999999)
        before = self.state()
        reply, _ = self.request(operations.BossChallengeClaimAllPointRewardOp, {})
        self.assertEqual(reply['result'], 0)
        self.assertFalse(reply.get('item_list'))
        self.assertEqual(self.state(), before)

    def test_duplicate_normal_star_records_count_each_achievement_once(self):
        group, stage = self.normal_clear()
        stars = [{'star_id': 1, 'is_achieve': 1}] * 3
        self.db.execute('UPDATE boss_challenge_normal SET star_info=? WHERE uid=? AND group_id=?',
                        (json.dumps([{'stage_id': stage, 'star_list': stars}] * 3), UID, group))
        self.rejected(operations.BossChallengeClaimStarRewardOp, {'star': 3})

    def test_prior_week_scores_are_not_eligible_for_the_new_weeks_reward(self):
        self.score(13000)
        with patch('time.time', return_value=NOW + 7 * 86400):
            self.rejected(operations.BossChallengeClaimPointRewardOp, {'point': 10000})

    def test_claimed_point_reward_blocks_single_boss_reset(self):
        self.score(13000)
        self.db.claim_boss_challenge_reward(UID, 2, 10000)
        self.rejected(operations.BossChallengeResetHardBossOp, {'boss_id': self.bosses[0]})

    def test_claimed_point_reward_blocks_whole_mode_reset(self):
        self.score(13000)
        self.db.claim_boss_challenge_reward(UID, 2, 10000)
        self.rejected(operations.BossChallengeResetHardModeOp, {})

    def test_unclaimed_single_reset_clears_both_score_copies_and_keeps_other_boss(self):
        self.score(13000)
        self.score(11000, self.bosses[1])
        reply, frames = self.request(operations.BossChallengeResetHardBossOp, {'boss_id': self.bosses[0]})
        self.assertEqual(reply['result'], 0)
        for table in ('boss_challenge_advance', 'boss_challenge_scores'):
            self.assertEqual(self.db.query('SELECT score FROM ' + table + ' WHERE uid=? AND boss_id=?',
                                          (UID, self.bosses[0]))[0]['score'], 0)
            self.assertEqual(self.db.query('SELECT score FROM ' + table + ' WHERE uid=? AND boss_id=?',
                                          (UID, self.bosses[1]))[0]['score'], 11000)
        self.assertLess([f['sc'] for f in frames].index(45101), [f['sc'] for f in frames].index(45107))

    def test_unclaimed_whole_mode_reset_clears_scores_and_releases_heroes(self):
        self.score(13000)
        reply, _ = self.request(operations.BossChallengeResetHardModeOp, {})
        self.assertEqual(reply['result'], 0)
        self.assertEqual(self.db.get_boss_challenge_progress(UID)['mode'], 0)
        self.assertTrue(all(row['score'] == 0 and row['used_heroes'] == '[]' for row in
                            self.db.query('SELECT score,used_heroes FROM boss_challenge_advance WHERE uid=?', (UID,))))

    def test_reset_history_read_failure_preserves_scores_heroes_and_mode(self):
        self.score(13000)
        with self.failing_query('boss_challenge_claimed_rewards'):
            self.rejected(operations.BossChallengeResetHardModeOp, {})

    def test_real_grant_failure_rolls_back_assets_records_and_notifications(self):
        self.score(13000)
        original, writes = InventoryService.grant_items, []

        def fail(*args, **kwargs):
            result = original(*args, **kwargs)
            writes.append(self.balance(1))
            self.ctx.append_frame(DownFrame(53003, b'\x08\x01'))
            raise RuntimeError('injected after real dream award')

        before = self.balance(1)
        with patch.object(InventoryService, 'grant_items', side_effect=fail):
            self.rejected(operations.BossChallengeClaimPointRewardOp, {'point': 10000})
        self.assertEqual(writes, [before + 50])

    def test_required_reward_reply_failure_rolls_back_the_successful_award(self):
        self.score(13000)
        original = self.ctx.codec_encode

        def fail(proto, data):
            if proto == 'sc_45103' and data.get('result') == 0:
                raise RuntimeError('injected dream receipt failure')
            return original(proto, data)

        with patch.object(self.ctx, 'codec_encode', side_effect=fail):
            self.rejected(operations.BossChallengeClaimPointRewardOp, {'point': 10000})

    def test_required_progress_refresh_failure_cannot_be_swallowed_after_award(self):
        self.score(13000)
        original = self.ctx.codec_encode

        def fail(proto, data):
            if proto == 'sc_45101':
                raise RuntimeError('injected dream state refresh failure')
            return original(proto, data)

        with patch.object(self.ctx, 'codec_encode', side_effect=fail):
            self.rejected(operations.BossChallengeClaimPointRewardOp, {'point': 10000})

    def test_direct_caught_nested_award_failure_preserves_only_callers_changes(self):
        self.score(13000)
        original = InventoryService.grant_items
        marker = self.balance(38)
        checkpoint = checkpoint_notifications(self.ctx)

        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('injected direct dream grant failure')

        with self.db.transaction():
            self.db.set_currency(UID, 38, marker + 1)
            caller = self.state()
            with patch.object(InventoryService, 'grant_items', side_effect=fail):
                with self.assertRaises(RuntimeError):
                    weekly.claim_boss_challenge_rewards(self.ctx, UID, 2, 10000)
            self.assertEqual(self.state(), caller)
        self.assertEqual(self.balance(38), marker + 1)
        self.assertEqual(checkpoint_notifications(self.ctx), checkpoint)

    def test_concurrent_single_and_bulk_award_each_tier_once(self):
        self.score(13000)
        barrier = threading.Barrier(2)
        before = self.balance(1)

        def call(cls, data):
            db = AccountDB(str(self.path))
            try:
                ctx = context(db)
                ctx.uid, ctx.log = UID, lambda *a, **kw: None
                barrier.wait(timeout=15)
                response = Core()._run_operation(cls(), CSRequest(
                    cls.cmd, encode('cs_' + str(cls.cmd), data), uid=UID), ctx)
                return decode(next(f['payload'] for f in response.frames if f['sc'] == cls.sc), 'sc_' + str(cls.sc))
            finally:
                db.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(call, operations.BossChallengeClaimPointRewardOp, {'point': 10000}),
                    pool.submit(call, operations.BossChallengeClaimAllPointRewardOp, {})]
            replies = [job.result(timeout=30) for job in jobs]
        self.assertTrue(any(reply['result'] == 0 for reply in replies))
        self.request(operations.BossChallengeClaimAllPointRewardOp, {})
        self.assertEqual(self.balance(1) - before, 100)
        self.assertEqual(len(self.db.query('SELECT reward_id FROM boss_challenge_claimed_rewards WHERE uid=? AND reward_type=2',
                                         (UID,))), 2)


if __name__ == '__main__':
    unittest.main()
