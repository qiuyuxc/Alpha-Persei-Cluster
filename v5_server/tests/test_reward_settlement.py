"""A57/A61/A137/A138: real packets, ownership, reward counts and rollback."""
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
SANDBOX = tempfile.TemporaryDirectory(prefix='ag-reward-settlement-tests-')
BOOTSTRAP = Path(SANDBOX.name) / 'bootstrap.db'
shutil.copyfile(SEED, BOOTSTRAP)
os.environ['ACCOUNT_DB_PATH'] = str(BOOTSTRAP)

from account_db import AccountDB
import achievement_service as achievements
from codec import decode, encode
from core import Core
from draw_service import DrawService
import event_bus
from gm_actions import context
from inventory_service import InventoryService
from middleware import CSRequest, DownFrame
from operations import DrawOp

UID = 2174928301
OTHER = 264084870
HERO = 1011
TABLES = ('hero', 'hero_piece', 'currency', 'material', 'servant',
          'achievement', 'draw_state', 'draw_group_state', 'draw_record')


class SettlementTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=SANDBOX.name)
        self.addCleanup(self.folder.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(achievements.AchievementService, '_instance', None))
        self.stack.enter_context(patch.object(DrawService, '_instance', None))
        self.stack.enter_context(patch.object(event_bus.EventBus, '_handlers', {}))
        self.events = self.stack.enter_context(patch('event_bus.bus.emit'))
        self.stack.enter_context(patch('game_cycles.apply_rotation'))
        self.path = Path(self.folder.name) / 'player.db'
        shutil.copyfile(SEED, self.path)
        self.db = AccountDB(str(self.path))
        self.addCleanup(self.db.close)
        self.ctx = context(self.db)
        self.ctx.uid = UID
        self.ctx.log = lambda *args, **kwargs: None
        self.achievement = achievements.AchievementService.get_instance(self.db)
        self.stack.enter_context(patch.object(self.achievement, 'reconcile_achievements'))
        self.draw = DrawService.get_instance(self.db)
        self.pool = 10001
        self.group = self.draw.get_pool_group(self.pool)
        self.draw.set_pity(UID, self.group, 0, is_up_guaranteed=0, since_sr=0)
        detail = json.loads(self.db.query('SELECT detail_json FROM draw_pool WHERE pool_id=?',
                                          (self.pool,))[0]['detail_json'])
        pair = detail.get('cost_once')
        self.cost_id, self.cost_num = (map(int, pair) if isinstance(pair, list)
                                      else (int(detail.get('cost_id', 38)), int(pair or 1)))
        table = 'currency' if self.cost_id < 1000 else 'material'
        self.db.execute('INSERT INTO ' + table + '(uid,id,num) VALUES (?,?,1000000) '
                        'ON CONFLICT(uid,id) DO UPDATE SET num=1000000', (UID, self.cost_id))
        self.other_state = self.state(OTHER)
        self.aids = [aid for aid, cfg in achievements._CFG.items() if cfg.get('reward')][:2]

    def state(self, uid=UID):
        return {table: sorted(self.db.query('SELECT * FROM ' + table + ' WHERE uid=?', (uid,)),
                              key=lambda row: json.dumps(row, sort_keys=True))
                for table in TABLES}

    def prepare_achievement(self, aid, ready=True, claimed=False):
        need = int(achievements._CFG[aid]['need'])
        self.db.execute('DELETE FROM achievement WHERE uid=? AND achievement_id=?', (UID, aid))
        self.db.execute('INSERT INTO achievement(uid,achievement_id,progress,need,complete_flag) '
                        'VALUES (?,?,?,?,?)', (UID, aid, need if ready else need - 1, need, int(claimed)))

    def request(self, operation, data):
        packet = encode('cs_' + str(operation.cmd), data)
        response = Core()._run_operation(operation(), CSRequest(operation.cmd, packet, uid=UID), self.ctx)
        replies = [f for f in response.frames if f['sc'] == operation.sc]
        self.assertEqual(len(replies), 1)
        self.assertEqual(self.state(OTHER), self.other_state)
        return decode(replies[0]['payload'], 'sc_' + str(operation.sc)), response.frames

    def claim(self, ids):
        return self.request(achievements.AchievementRewardOp, {'id': ids})

    def test_duplicate_achievement_ids_award_each_achievement_once(self):
        for aid in self.aids:
            self.prepare_achievement(aid)
        expected = {}
        for aid in self.aids:
            for iid, num in achievements._CFG[aid]['reward']:
                expected[iid] = expected.get(iid, 0) + num
        balances = {iid: InventoryService.get_item_balance(self.db, UID, iid)[1] for iid in expected}
        reply, _ = self.claim([self.aids[0], self.aids[1], self.aids[0], self.aids[1]])
        self.assertEqual(reply['result'], 0)
        self.assertEqual({x['id']: x['num'] for x in reply['reward_list']}, expected)
        for iid, num in expected.items():
            self.assertEqual(InventoryService.get_item_balance(self.db, UID, iid)[1], balances[iid] + num)
        settled = self.state()
        reply, _ = self.claim(self.aids)
        self.assertEqual(reply['result'], 0)
        self.assertEqual(reply.get('reward_list', []), [])
        self.assertEqual(self.state(), settled)

    def test_mixed_incomplete_achievement_request_rejects_without_partial_rewards(self):
        self.prepare_achievement(self.aids[0])
        self.prepare_achievement(self.aids[1], ready=False)
        before = self.state()
        reply, _ = self.claim(self.aids)
        self.assertEqual(reply['result'], 2)
        self.assertEqual(self.state(), before)
        self.assertFalse(self.ctx.touched_items)

    def test_unknown_zero_and_missing_achievement_rows_reject_the_entire_request(self):
        self.prepare_achievement(self.aids[0])
        self.db.execute('DELETE FROM achievement WHERE uid=? AND achievement_id=?', (UID, self.aids[1]))
        before = self.state()
        for invalid in (999999999, 0, self.aids[1]):
            with self.subTest(invalid=invalid):
                reply, _ = self.claim([self.aids[0], invalid])
                self.assertEqual(reply['result'], 2)
                self.assertEqual(self.state(), before)

    def test_already_claimed_and_ready_achievements_can_be_acknowledged_together(self):
        self.prepare_achievement(self.aids[0], claimed=True)
        self.prepare_achievement(self.aids[1])
        reply, _ = self.claim(self.aids)
        self.assertEqual(reply['result'], 0)
        expected = {iid: num for iid, num in achievements._CFG[self.aids[1]]['reward']}
        self.assertEqual({x['id']: x['num'] for x in reply['reward_list']}, expected)

    def test_achievement_grant_failure_rolls_back_flags_assets_and_notifications(self):
        self.prepare_achievement(self.aids[0])
        before = self.state()
        real_grant = InventoryService.grant_items

        def fail_after_grant(*args, **kwargs):
            real_grant(*args, **kwargs)
            self.ctx.append_frame(DownFrame(53003, b'\x08\x01'))
            raise RuntimeError('injected after rewards')

        with patch.object(InventoryService, 'grant_items', side_effect=fail_after_grant):
            reply, frames = self.claim([self.aids[0]])
        self.assertEqual(reply['result'], 9)
        self.assertEqual(self.state(), before)
        self.assertFalse(self.ctx.touched_items)
        self.assertEqual([f['sc'] for f in frames], [53005])

    def test_standalone_achievement_failure_is_atomic(self):
        self.prepare_achievement(self.aids[0])
        before = self.state()
        with patch.object(InventoryService, 'grant_items', side_effect=RuntimeError('injected grant failure')):
            with self.assertRaises(RuntimeError):
                self.achievement.claim_achievements(UID, [self.aids[0]], self.ctx)
        self.assertEqual(self.state(), before)

    def test_achievement_ack_encoding_failure_rolls_back_reward_and_claim(self):
        self.prepare_achievement(self.aids[0])
        before = self.state()
        real_encode = self.ctx.codec_encode
        self.ctx.codec_encode = lambda name, data: b'' if name == 'sc_53005' else real_encode(name, data)
        reply, _ = self.claim([self.aids[0]])
        self.assertNotEqual(reply['result'], 0)
        self.assertEqual(self.state(), before)
        self.assertFalse(self.ctx.touched_items)

    def test_locked_placeholder_unlocks_without_resetting_growth_or_giving_pieces(self):
        self.db.execute('UPDATE hero SET unlock=0,level=23,exp=1234,star=400 WHERE uid=? AND id=?', (UID, HERO))
        before = self.db.query('SELECT * FROM hero WHERE uid=? AND id=?', (UID, HERO))[0]
        pieces = self.db.query('SELECT * FROM hero_piece WHERE uid=? AND hero_id=?', (UID, HERO))
        with self.db.transaction():
            summary = InventoryService.grant_items(self.ctx, UID, [(HERO, 1)], source='test')
        after = self.db.query('SELECT * FROM hero WHERE uid=? AND id=?', (UID, HERO))[0]
        self.assertEqual(after['unlock'], 1)
        self.assertEqual({k: v for k, v in after.items() if k not in ('unlock', 'update_ts')},
                         {k: v for k, v in before.items() if k not in ('unlock', 'update_ts')})
        self.assertEqual(self.db.query('SELECT * FROM hero_piece WHERE uid=? AND hero_id=?', (UID, HERO)), pieces)
        self.assertEqual(summary['hero_unlocked'], [HERO])
        self.assertEqual(summary['hero_converted'], [])
        self.assertEqual(InventoryService.get_item_balance(self.db, UID, HERO)[1], 1)

    def test_two_copies_of_locked_hero_unlock_once_and_convert_only_second(self):
        self.db.execute('UPDATE hero SET unlock=0 WHERE uid=? AND id=?', (UID, HERO))
        pieces = InventoryService.get_item_balance(self.db, UID, 10000 + HERO)[1]
        with self.db.transaction():
            summary = InventoryService.grant_items(self.ctx, UID, [(HERO, 2)])
        self.assertEqual(summary['hero_unlocked'], [HERO])
        self.assertEqual(len(summary['hero_converted']), 1)
        self.assertEqual(InventoryService.get_item_balance(self.db, UID, HERO)[1], 1)
        self.assertEqual(InventoryService.get_item_balance(self.db, UID, 10000 + HERO)[1], pieces + 18)
        unlock_events = [c for c in self.events.call_args_list if c.args[0] == event_bus.Events.HERO_UNLOCK]
        self.assertEqual(len(unlock_events), 1)

    def test_missing_hero_row_initializes_normally(self):
        self.db.execute('DELETE FROM hero WHERE uid=? AND id=?', (UID, HERO))
        with self.db.transaction():
            summary = InventoryService.grant_items(self.ctx, UID, [(HERO, 1)])
        row = self.db.query('SELECT level,exp,star,unlock FROM hero WHERE uid=? AND id=?', (UID, HERO))[0]
        self.assertEqual(row, {'level': 1, 'exp': 0, 'star': 200, 'unlock': 1})
        self.assertEqual(summary['hero_unlocked'], [HERO])

    def test_owned_hero_keeps_growth_and_only_converts_to_pieces(self):
        self.db.execute('UPDATE hero SET unlock=1 WHERE uid=? AND id=?', (UID, HERO))
        hero = self.db.query('SELECT * FROM hero WHERE uid=? AND id=?', (UID, HERO))
        with self.db.transaction():
            summary = InventoryService.grant_items(self.ctx, UID, [(HERO, 1)])
        self.assertEqual(self.db.query('SELECT * FROM hero WHERE uid=? AND id=?', (UID, HERO)), hero)
        self.assertEqual(summary['hero_unlocked'], [])
        self.assertEqual(summary['hero_piece'], {HERO: 18})

    def test_draw_award_failure_rolls_back_tickets_pity_and_history(self):
        before = self.state()
        with patch('inventory_service.grant_items', side_effect=RuntimeError('injected award failure')) as failed:
            reply, _ = self.request(DrawOp, {'pool': self.pool, 'type': 10})
        self.assertTrue(failed.called)
        self.assertEqual(reply['result'], 9)
        self.assertEqual(self.state(), before)
        self.assertFalse(self.ctx.touched_items)

    def test_draw_partial_award_failure_rolls_back_inserted_instances(self):
        before = self.state()
        real_grant = InventoryService.grant_items

        def fail_after_grant(*args, **kwargs):
            real_grant(*args, **kwargs)
            raise RuntimeError('injected after inventory grant')

        with patch('inventory_service.grant_items', side_effect=fail_after_grant):
            reply, _ = self.request(DrawOp, {'pool': self.pool, 'type': 10})
        self.assertEqual(reply['result'], 9)
        self.assertEqual(self.state(), before)
        self.assertFalse(self.ctx.touched_items)

    def test_draw_record_failure_rolls_back_complete_settlement(self):
        before = self.state()
        real_execute = self.db.execute

        def fail_record(sql, args=()):
            if 'INSERT INTO draw_record' in sql:
                raise sqlite3.OperationalError('injected history failure')
            return real_execute(sql, args)

        with patch.object(self.db, 'execute', side_effect=fail_record):
            reply, _ = self.request(DrawOp, {'pool': self.pool, 'type': 1})
        self.assertEqual(reply['result'], 9)
        self.assertEqual(self.state(), before)

    def test_draw_pool_state_failure_rolls_back_group_pity_and_tickets(self):
        before = self.state()
        real_execute = self.db.execute

        def fail_pool_state(sql, args=()):
            if 'INSERT INTO draw_state ' in sql:
                raise sqlite3.OperationalError('injected per-pool pity failure')
            return real_execute(sql, args)

        with patch.object(self.db, 'execute', side_effect=fail_pool_state):
            reply, _ = self.request(DrawOp, {'pool': self.pool, 'type': 1})
        self.assertEqual(reply['result'], 9)
        self.assertEqual(self.state(), before)

    def test_legacy_draw_schema_migration_preserves_targets_and_pity_and_is_idempotent(self):
        with sqlite3.connect('file:' + str(SEED) + '?mode=ro', uri=True) as seed:
            seed.row_factory = sqlite3.Row
            original = [dict(row) for row in seed.execute(
                'SELECT * FROM draw_state WHERE uid=? ORDER BY pool_id', (UID,))]
        current = self.db.query('SELECT * FROM draw_state WHERE uid=? ORDER BY pool_id', (UID,))
        self.assertTrue(all(row['since_sr'] == 0 for row in current))
        self.assertEqual([{key: row[key] for key in original[0]} for row in current], original)
        self.draw._init_db_schema()
        self.assertEqual(self.db.query('SELECT * FROM draw_state WHERE uid=? ORDER BY pool_id', (UID,)), current)

    def test_required_asset_refresh_failure_rolls_back_draw_and_achievement(self):
        self.prepare_achievement(self.aids[0])
        before = self.state()
        import generator
        real_generate = generator.gen_payload

        def fail_assets(cmd, *args, **kwargs):
            return b'' if cmd == 17023 else real_generate(cmd, *args, **kwargs)

        with patch('generator.gen_payload', side_effect=fail_assets):
            for op, data in ((DrawOp, {'pool': self.pool, 'type': 1}),
                             (achievements.AchievementRewardOp, {'id': [self.aids[0]]})):
                with self.subTest(operation=op.cmd):
                    reply, _ = self.request(op, data)
                    self.assertNotEqual(reply['result'], 0)
                    self.assertEqual(self.state(), before)
                    self.assertFalse(self.ctx.touched_items)

    def test_draw_ack_encoding_failure_does_not_charge_or_create_rewards(self):
        before = self.state()
        real_encode = self.ctx.codec_encode
        self.ctx.codec_encode = lambda name, data: b'' if name == 'sc_16011' else real_encode(name, data)
        reply, _ = self.request(DrawOp, {'pool': self.pool, 'type': 1})
        self.assertNotEqual(reply['result'], 0)
        self.assertEqual(self.state(), before)
        self.assertFalse(self.ctx.touched_items)

    def test_standalone_draw_failure_rolls_back_tickets_and_pity(self):
        before = self.state()
        with patch('inventory_service.grant_items', side_effect=RuntimeError('injected award failure')):
            with self.assertRaises(RuntimeError):
                self.draw.draw(self.ctx, UID, self.pool, 10)
        self.assertEqual(self.state(), before)
        self.assertFalse(self.ctx.touched_items)

    def test_successful_draw_charges_once_and_survives_database_reopen(self):
        balance = InventoryService.get_item_balance(self.db, UID, self.cost_id)[1]
        count = len(self.db.query('SELECT * FROM draw_record WHERE uid=? AND pool_group=?', (UID, self.group)))
        other_records = self.db.query('SELECT * FROM draw_record WHERE uid=? AND pool_group!=?', (UID, self.group))
        with patch('draw_service.random.random', return_value=1):
            reply, _ = self.request(DrawOp, {'pool': self.pool, 'type': 1})
        self.assertEqual(reply['result'], 0)
        self.assertEqual(InventoryService.get_item_balance(self.db, UID, self.cost_id)[1], balance - self.cost_num)
        self.assertEqual(len(self.db.query('SELECT * FROM draw_record WHERE uid=? AND pool_group=?',
                                          (UID, self.group))), min(count + 1, 100))
        self.assertEqual(self.db.query('SELECT * FROM draw_record WHERE uid=? AND pool_group!=?',
                                       (UID, self.group)), other_records)
        self.assertEqual(self.draw.get_draw_state(UID, self.group)['since_ssr'], 1)
        expected = self.state()
        self.db.close()
        self.db = AccountDB(str(self.path))
        self.addCleanup(self.db.close)
        self.assertEqual(self.state(), expected)

    def test_drawn_locked_heroes_unlock_at_s_a_and_b_rarities(self):
        # Force each production branch while keeping the real ticket and award paths.
        cases = ((1066, 's'), (1011, 'a'), (1084, 'b'))
        for hero, rarity in cases:
            with self.subTest(rarity=rarity):
                self.db.execute('UPDATE hero SET unlock=0 WHERE uid=? AND id=?', (UID, hero))
                self.draw.set_pity(UID, self.group, 0, is_up_guaranteed=0, since_sr=0)
                if rarity == 's':
                    values = [0]
                elif rarity == 'a':
                    values = [1, 0, 1]
                else:
                    values = [1, 1, 0]
                with patch('draw_service.random.random', side_effect=values), \
                        patch('draw_service.random.choice', return_value=hero):
                    reply, _ = self.request(DrawOp, {'pool': self.pool, 'type': 1})
                self.assertEqual(reply['result'], 0, reply)
                self.assertEqual(InventoryService.get_item_balance(self.db, UID, hero)[1], 1)
                self.assertTrue(any(x['id'] == hero for x in reply['item']))


if __name__ == '__main__':
    unittest.main()
