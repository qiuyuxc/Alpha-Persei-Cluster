"""v5.0.2 gameplay fixes adapted to the multi-account server.

Use a complete database COPY via GM_TEST_DB_SEED; no live save is changed.
"""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, mock_open, patch

SEED = Path(os.environ['GM_TEST_DB_SEED']).resolve()
SANDBOX = tempfile.TemporaryDirectory(prefix='ag-upstream-v502-tests-')
BOOT = Path(SANDBOX.name) / 'bootstrap.db'
shutil.copyfile(SEED, BOOT)
os.environ['ACCOUNT_DB_PATH'] = str(BOOT)

from account_db import AccountDB
import achievement_service as achievements
import battle_payload
import battle_server as battle
from codec import decode
import event_bus
import fatigue_service
from gm_actions import context
import inventory_service
from inventory_service import InventoryService
import task_listener

UID, OTHER = 2174928301, 264084870
NOW = 1793376000


class GameplayContracts(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=SANDBOX.name)
        self.addCleanup(self.folder.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('time.time', return_value=NOW))
        self.stack.enter_context(patch.object(event_bus.EventBus, '_handlers', {}))
        self.events = self.stack.enter_context(patch('event_bus.bus.emit'))
        self.stack.enter_context(patch.object(achievements.AchievementService, '_instance', None))
        self.path = Path(self.folder.name) / 'player.db'
        shutil.copyfile(SEED, self.path)
        self.db = AccountDB(str(self.path))
        self.addCleanup(self.db.close)
        self.ctx = context(self.db)
        self.ctx.uid = UID
        self.ctx.log = lambda *args, **kwargs: None
        self.other = self.state(OTHER)

    def tearDown(self):
        self.assertEqual(self.state(OTHER), self.other)
        self.assertEqual(self.db.query('PRAGMA integrity_check')[0]['integrity_check'], 'ok')

    def state(self, uid=UID):
        return {table: self.db.query('SELECT * FROM ' + table + ' WHERE uid=?', (uid,))
                for table in ('users', 'currency', 'hero', 'task', 'achievement')}

    def balance(self, iid):
        rows = self.db.query('SELECT num FROM currency WHERE uid=? AND id=?', (UID, iid))
        return int(rows[0]['num']) if rows else 0

    def reset_experience(self, experience=0, stored_level=1):
        self.db.execute('UPDATE users SET exp=?,level=? WHERE uid=?',
                        (experience, stored_level, UID))
        self.db.set_currency(UID, 12, experience)
        self.db.set_currency(UID, 4, 100)
        self.db.execute('UPDATE currency SET last_fatigue_recover_time=0 WHERE uid=? AND id=4', (UID,))

    def test_player_experience_uses_native_thresholds_and_level_cap(self):
        # GameLevelSetting/LvTools: first costs are 5, 6 and 7; max level is 100.
        for experience, level in ((0, 1), (4, 1), (5, 2), (10, 2), (11, 3),
                                  (17, 3), (18, 4), (20, 4), (10_000_000, 100)):
            with self.subTest(experience=experience):
                self.assertEqual(InventoryService.calc_player_level(experience), level)

    def test_one_level_grants_ten_fatigue_and_updates_real_balances(self):
        self.reset_experience()
        summary = InventoryService.grant_item(self.ctx, UID, 12, 5)
        self.assertEqual((self.db.get('users', UID)['level'], self.balance(12)), (2, 5))
        self.assertEqual(self.balance(4), 110)
        self.assertEqual(summary['currency'][4], 10)
        level_events = [call for call in self.events.call_args_list
                        if call.args[0] == event_bus.Events.PLAYER_LEVEL_UP]
        self.assertEqual(len(level_events), 1)
        self.assertEqual(level_events[0].kwargs['stamina_gain'], 10)
        self.assertTrue({4, 12}.issubset(self.ctx.touched_items))

    def test_multi_level_grant_sums_only_new_level_rewards(self):
        self.reset_experience()
        InventoryService.grant_item(self.ctx, UID, 12, 18)
        self.assertEqual((self.db.get('users', UID)['level'], self.balance(4)), (4, 130))
        InventoryService.grant_item(self.ctx, UID, 12, 1)
        self.assertEqual((self.balance(12), self.balance(4)), (19, 130))

    def test_stale_stored_level_does_not_replay_historical_fatigue_rewards(self):
        self.reset_experience(experience=18, stored_level=1)
        InventoryService.grant_item(self.ctx, UID, 12, 1)
        self.assertEqual((self.db.get('users', UID)['level'], self.balance(4)), (4, 100))
        self.assertFalse(any(call.args[0] == event_bus.Events.PLAYER_LEVEL_UP
                             for call in self.events.call_args_list))

    def test_invalid_experience_curve_leaves_save_and_notifications_unchanged(self):
        self.reset_experience()
        before = self.state()
        touched = set(self.ctx.touched_items)
        with patch.object(inventory_service, '_USER_LEVEL_SETTINGS_CACHE', None), \
                patch('inventory_service.open', mock_open(read_data='{"1":0,"2":6}'), create=True):
            with self.assertRaises(ValueError):
                InventoryService.grant_item(self.ctx, UID, 12, 5)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.ctx.touched_items, touched)
        self.events.assert_not_called()

    def test_invalid_level_reward_leaves_experience_and_fatigue_unchanged(self):
        self.reset_experience()
        before = self.state()
        touched = set(self.ctx.touched_items)
        with patch.object(fatigue_service, '_LEVEL_UP_REWARDS_CACHE', None, create=True), \
                patch('fatigue_service.open', mock_open(read_data='{"1":0,"2":-1}'), create=True):
            with self.assertRaises(ValueError):
                InventoryService.grant_item(self.ctx, UID, 12, 5)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.ctx.touched_items, touched)
        self.events.assert_not_called()

    def test_level_seventy_restores_fatigue_up_to_240(self):
        self.db.execute('UPDATE users SET level=70 WHERE uid=?', (UID,))
        self.db.set_currency(UID, 4, 239)
        self.db.execute('UPDATE currency SET last_fatigue_recover_time=? WHERE uid=? AND id=4',
                        (NOW - 360, UID))
        fatigue_service.settle_fatigue(self.db, UID, now_ts=NOW)
        self.assertEqual(self.balance(4), 240)
        self.assertEqual(self.db.get('currency', UID, 'AND id=4')['last_fatigue_recover_time'], 0)

    def add_task(self, task_id, condition, parameter):
        self.db.execute('INSERT INTO task_cfg (task_id,condition,need,additional_parameter,task_type) '
                        'VALUES(?,?,1000,?,4)', (task_id, condition, json.dumps(parameter)))
        self.db.execute('INSERT INTO task (uid,task_id,progress) VALUES(?,?,0)', (UID, task_id))

    def task_progress(self, task_id):
        return self.db.query('SELECT progress FROM task WHERE uid=? AND task_id=?',
                             (UID, task_id))[0]['progress']

    def reset_tasks(self):
        self.db.execute('DELETE FROM task_cfg WHERE condition IN (300,600,2021)')

    def test_activity_point_candidates_update_each_task_once_and_send_one_diff(self):
        self.reset_tasks()
        for task_id, parameter in ((990001, [0]), (990002, [22]), (990003, [1]),
                                   (990004, [999]), (990005, [22, 1])):
            self.add_task(task_id, 300, parameter)
        self.add_task(990006, 2021, [22])
        task_listener.on_activity_point_gain(self.ctx, UID, pt_id=1, item_id=22, amount=10)
        self.assertEqual([self.task_progress(i) for i in range(990001, 990007)],
                         [10, 10, 10, 0, 10, 10])
        frames = [frame for frame in self.ctx.pending_frames if frame.cmd == 28007]
        self.assertEqual(len(frames), 1)
        changed = decode(frames[0].payload, 'sc_28007')['progress_list']
        self.assertEqual(len(changed), 5)
        self.assertEqual(len({row['id'] for row in changed}), 5)

    def test_equal_activity_point_identifiers_do_not_double_count(self):
        self.reset_tasks()
        self.add_task(990001, 300, [22])
        self.add_task(990002, 300, [0])
        task_listener.on_activity_point_gain(self.ctx, UID, pt_id=22, item_id=22, amount=10)
        self.assertEqual([self.task_progress(990001), self.task_progress(990002)], [10, 10])

    def test_shop_purchase_matches_nested_and_legacy_shop_restrictions(self):
        self.reset_tasks()
        parameters = ([[2], 0], [[3], 0], [2, 3], [0, 0], [[2], 1], [-1])
        for i, parameter in enumerate(parameters, 990001):
            self.add_task(i, 600, parameter)
        task_listener.on_shop_buy(self.ctx, UID, count=2, shop_id=2)
        self.assertEqual([self.task_progress(i) for i in range(990001, 990007)],
                         [2, 0, 2, 2, 0, 0])

    def test_shop_without_identity_advances_only_unrestricted_tasks(self):
        self.reset_tasks()
        self.add_task(990001, 600, [[2], 0])
        self.add_task(990002, 600, [0, 0])
        task_listener.on_shop_buy(self.ctx, UID)
        self.assertEqual([self.task_progress(990001), self.task_progress(990002)], [0, 1])

    def reset_login_facts(self, retained_days=0):
        service = achievements.AchievementService.get_instance(self.db)
        service.init_user_achievements(UID)
        self.db.execute('UPDATE achievement SET progress=?,complete_flag=0,achieve_time=0 '
                        'WHERE uid=? AND condition=12', (retained_days, UID))
        extra = json.loads(self.db.get('users', UID).get('extra') or '{}')
        extra.pop('total_login_days', None)
        extra.pop('last_ach_login_date', None)
        self.db.execute('UPDATE users SET extra=? WHERE uid=?', (json.dumps(extra), UID))
        return service

    def test_first_real_login_starts_at_one_and_same_day_login_is_idempotent(self):
        service = self.reset_login_facts()
        self.assertEqual(service.on_user_daily_login(UID, self.ctx, NOW), 1)
        self.assertEqual(service.on_user_daily_login(UID, self.ctx, NOW + 60), 1)
        self.assertEqual(service.on_user_daily_login(UID, self.ctx, NOW + 86400), 2)

    def test_legacy_login_progress_is_retained_without_a_251_day_floor(self):
        service = self.reset_login_facts(retained_days=17)
        self.assertEqual(service.on_user_daily_login(UID, self.ctx, NOW), 18)

    def test_reconciliation_recovers_only_persisted_login_days(self):
        service = self.reset_login_facts()
        service.reconcile_achievements(UID, self.ctx)
        extra = json.loads(self.db.get('users', UID)['extra'])
        self.assertEqual(extra['total_login_days'], 0)

    def test_first_clear_accepts_client_star_flags_without_losing_achievement_events(self):
        service = achievements.AchievementService.get_instance(self.db)
        with patch.object(service, 'trigger_condition') as trigger:
            achievements._on_stage_first_clear(self.ctx, UID, stage_id=1030001,
                                               difficulty=2, win_stars=[1, 1, 1])
        self.assertEqual([call.args[1] for call in trigger.call_args_list], [450, 100004])

    def test_partial_first_clear_does_not_count_a_three_star_achievement(self):
        service = achievements.AchievementService.get_instance(self.db)
        with patch.object(service, 'trigger_condition') as trigger:
            achievements._on_stage_first_clear(self.ctx, UID, stage_id=1030001,
                                               difficulty=2, win_stars=[1, 0, 1])
        self.assertEqual([call.args[1] for call in trigger.call_args_list], [100004])

    def trial(self, template):
        return decode(battle_payload.build_battle_hero_bytes(self.db._conn(), UID, template),
                      'battle_hero_net_rec')

    def test_trial_hero_keeps_template_identity_type_and_zero_breakthrough(self):
        hero = self.trial(100101)
        self.assertEqual((hero['hero_type'], hero['hero_base_info']['id']), (2, 100101))
        self.assertEqual(hero['hero_base_info']['break_level'], 0)
        self.assertEqual([equip['now_break_level'] for equip in hero['equip_list']], [3] * 6)

    def test_trial_hero_and_weapon_use_cumulative_client_experience(self):
        hero = self.trial(100101)['hero_base_info']
        self.assertEqual(hero['exp'], 752610)  # Captured Lv80 hero total.
        self.assertEqual(hero['weapon']['exp'], 99800)  # Captured Lv60 weapon total.

    def test_trial_level_one_weapon_retains_zero_experience_and_breakthrough(self):
        hero = self.trial(112703)['hero_base_info']
        self.assertEqual((hero['weapon']['exp'], hero['weapon']['breakthrough']), (0, 0))

    def test_battle_entry_rejects_unknown_account_and_empty_teams(self):
        for uid, heroes in ((999999999, [1084]), (UID, []), (UID, [0])):
            with self.subTest(uid=uid, heroes=heroes), self.assertRaises(ValueError):
                battle_payload.build_sc_54003_dynamic(self.db, uid, heroes)


class BattleSessionContracts(unittest.TestCase):
    def setUp(self):
        self.socket = MagicMock()
        socket_patch = patch.object(battle.socket, 'socket', return_value=self.socket)
        socket_patch.start()
        self.addCleanup(socket_patch.stop)
        self.server = battle.BattleServer(log=lambda *args, **kwargs: None)
        self.address = ('127.0.0.1', 30001)
        self.retired = self.server._new_session(101, 202, self.address)

    def test_new_session_resets_the_reused_client_heartbeat_sequence(self):
        self.server._pong_seq[101] = 8
        self.server._new_session(101, 203, self.address)
        self.assertNotIn(101, self.server._pong_seq)

    def test_retired_session_cannot_send_a_pong_for_its_replacement(self):
        replacement = self.server._new_session(101, 203, self.address)
        self.server._pong_seq[101] = 2
        frame = battle.xs_encode_frame(battle.OP_PING, battle._u64(1000), flag=1, rpc_id=1)
        self.server._handle_frame(frame, self.retired)
        self.assertIs(self.server._conns[101], replacement)
        self.assertEqual(self.server._pong_seq[101], 2)
        self.socket.sendto.assert_not_called()


if __name__ == '__main__':
    unittest.main()
