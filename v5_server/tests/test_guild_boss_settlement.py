"""Real protocol settlement, best scores and claims on disposable public saves."""
import unittest
from unittest.mock import patch

import battle_server as transport
from codec import decode
import guild_boss
from inventory_service import InventoryService
import middleware
import social_service as social
import test_family_flows as family

UID, OTHER, GID, HERO, NOW = family.UID, family.OTHER, family.GID, family.HERO, family.NOW


class GuildBossTests(unittest.TestCase):
    social_request = family.FamilyFlowTests.social_request
    join = family.FamilyFlowTests.join
    battle_request = family.FamilyFlowTests.battle_request

    def setUp(self):
        family.FamilyFlowTests.setUp(self)
        self.join()
        self.clock.return_value = NOW + 5 * social.DAY + 3600

    def start(self, heroes=None, frame_packer=None):
        body = {'common_info': {'hero_list': heroes or [{'hero_id': HERO, 'hero_type': 1}],
                               'type': 33, 'dest': 6, 'activity_id': 103, 'battle_times': 1},
                'is_restarted_battle': 0}
        ack, frames = self.battle_request(54030, body, frame_packer)
        self.assertEqual(ack['result'], 0, frames)
        return dict(frames)[54007]['battle_start']['battle_id'], frames

    def finish(self, bid, result=1, damage=3041038, milliseconds=8217, frame_packer=None, info=None):
        session = middleware.get_battle_session(bid)
        if session is not None:
            session['result_data'] = {'result': result, 'info': info if info is not None else {
                'total_damage': damage, 'battle_time': milliseconds}}
        return self.battle_request(54032, {'battle_id': bid}, frame_packer)

    def best(self):
        return social.boss_info(self.db, UID, GID, self.clock.return_value)['info']

    def stored_run(self, bid):
        return guild_boss.find_run(self.db, UID, bid)

    def test_native_damage_and_seconds_persist_without_normal_stage_rewards(self):
        bid, _ = self.start()
        before = {table: self.db.query('SELECT * FROM ' + table)
                  for table in ('users', 'currency', 'hero')}
        ack, frames = self.finish(bid)
        self.assertEqual(ack['result'], 0, frames)
        result = ack['battle_result']
        self.assertEqual((result['dest'], result['result'], result['use_seconds']), (6, 1, 8))
        self.assertFalse(result.get('all_drop_list'))
        self.assertFalse(result.get('star_list'))
        self.assertEqual(self.stored_run(bid)['boss_hp'], 2711610)
        self.assertEqual(self.stored_run(bid)['damage'], 3041038)
        self.assertEqual(self.stored_run(bid)['score'], 157333)
        info = dict(frames)[33001]['info']
        self.assertEqual((info['personal_score'], info['total_damage']), (157333, 157333))
        self.assertEqual(before, {table: self.db.query('SELECT * FROM ' + table) for table in before})

    def test_lower_and_equal_attempts_leave_personal_best_guild_damage_and_rank_time(self):
        bid, _ = self.start()
        self.finish(bid, milliseconds=6000)
        original = self.db.query('SELECT * FROM social_boss_scores WHERE uid=?', (UID,))
        for milliseconds in (8000, 6000):
            self.clock.return_value += 60
            bid, _ = self.start()
            self.finish(bid, milliseconds=milliseconds)
            self.assertEqual(self.db.query('SELECT * FROM social_boss_scores WHERE uid=?', (UID,)), original)
        self.assertEqual(self.best()['personal_score'], 158000)
        self.assertEqual(self.best()['total_damage'], 158000)

    def test_better_attempt_adds_only_the_improvement_to_collective_damage(self):
        bid, _ = self.start()
        self.finish(bid, result=2, damage=271161)
        self.assertEqual(self.best()['personal_score'], 10000)
        bid, _ = self.start()
        self.finish(bid, damage=3009006, milliseconds=6171)
        self.assertEqual(self.best()['personal_score'], 158000)
        self.assertEqual(self.best()['total_damage'], 158000)

    def test_failure_keeps_damage_fraction_and_quit_awards_no_score(self):
        bid, _ = self.start()
        ack, _ = self.finish(bid, result=2, damage=271161)
        self.assertEqual(ack['battle_result']['result'], 2)
        self.assertEqual(self.stored_run(bid)['score'], 10000)
        bid, _ = self.start()
        ack, _ = self.finish(bid, result=3, info={})
        self.assertEqual(ack['battle_result']['result'], 3)
        self.assertEqual(self.stored_run(bid)['score'], 0)
        self.assertEqual(self.best()['personal_score'], 10000)

    def test_damage_fraction_uses_combat_hp_and_keeps_the_native_time_formula(self):
        bid, _ = self.start()
        self.finish(bid, damage=271161, milliseconds=6000)
        self.assertEqual(self.stored_run(bid)['score'], 68000)
        bid, _ = self.start()
        self.finish(bid, milliseconds=181000)
        self.assertEqual(self.stored_run(bid)['score'], 99666)

    def test_missing_damage_or_invalid_statistic_does_not_fabricate_points(self):
        for info in ({'battle_time': 6000}, {'total_damage': 10000},
                     {'battle_time': 6000, 'total_damage': -1},
                     {'battle_time': 6000, 'total_damage': True}):
            with self.subTest(info=info):
                bid, _ = self.start()
                ack, _ = self.finish(bid, info=info)
                self.assertNotEqual(ack['result'], 0)
                self.assertEqual(self.stored_run(bid)['state'], 0)
                self.assertEqual(self.best()['personal_score'], 0)

    def test_foreign_duplicate_or_inconsistent_hero_damage_is_rejected(self):
        for statistics, total in (([{'hero_id': 999, 'damage': 100}], 100),
                                  ([{'hero_id': HERO, 'damage': 100}] * 2, 200),
                                  ([{'hero_id': HERO, 'damage': 100}], 200)):
            with self.subTest(statistics=statistics):
                bid, _ = self.start()
                ack, _ = self.finish(bid, info={'battle_time': 6000,
                    'total_damage': total, 'hero_damage': statistics})
                self.assertNotEqual(ack['result'], 0)
                self.assertEqual(self.stored_run(bid)['state'], 0)
                self.assertEqual(self.best()['personal_score'], 0)

    def test_reply_packing_failure_rolls_back_run_and_points_then_retries_once(self):
        bid, _ = self.start()
        def fail(*args, **kwargs):
            raise ValueError('injected Boss reply failure')
        ack, _ = self.finish(bid, frame_packer=fail)
        self.assertNotEqual(ack['result'], 0)
        self.assertEqual(self.stored_run(bid)['state'], 0)
        self.assertEqual(self.best()['personal_score'], 0)
        ack, _ = self.finish(bid)
        self.assertEqual(ack['result'], 0)
        ack, frames = self.finish(bid)
        self.assertEqual(ack['result'], 0)
        self.assertFalse(any(cmd == 33001 for cmd, _ in frames))
        self.assertEqual(self.best()['total_damage'], 157333)

    def test_result_encoding_failure_cannot_commit_or_cache_an_empty_receipt(self):
        bid, _ = self.start()
        original = self.ctx.codec_encode
        with patch.object(self.ctx, 'codec_encode',
                          side_effect=lambda name, body: None if name == 'sc_54033' else original(name, body)):
            ack, _ = self.finish(bid)
        self.assertNotEqual(ack['result'], 0)
        self.assertEqual(self.stored_run(bid)['state'], 0)
        self.assertEqual(self.best()['personal_score'], 0)
        ack, _ = self.finish(bid)
        self.assertEqual(ack['result'], 0)
        self.assertEqual(self.best()['total_damage'], 157333)

    def test_receipt_survives_session_loss_and_week_change_without_resending_stale_state(self):
        bid, _ = self.start()
        first, _ = self.finish(bid)
        middleware._BATTLE_SESSIONS.clear()
        self.clock.return_value = NOW + 7 * social.DAY
        ack, frames = self.battle_request(54032, {'battle_id': bid})
        self.assertEqual(ack, first)
        self.assertEqual([cmd for cmd, _ in frames], [54033])

    def test_pending_boss_run_cannot_credit_after_week_reset(self):
        bid, _ = self.start()
        self.clock.return_value = NOW + 7 * social.DAY
        ack, _ = self.finish(bid)
        self.assertNotEqual(ack['result'], 0)
        self.assertEqual(self.stored_run(bid)['state'], 0)
        self.assertFalse(self.db.query('SELECT 1 FROM social_boss_scores WHERE uid=?', (UID,)))

    def test_difficulty_change_cannot_credit_the_new_difficulty(self):
        bid, _ = self.start()
        self.db.execute('UPDATE social_guilds SET difficulty=2 WHERE id=?', (GID,))
        ack, _ = self.finish(bid)
        self.assertNotEqual(ack['result'], 0)
        self.assertEqual(self.stored_run(bid)['state'], 0)

    def test_leave_and_rejoin_invalidates_unfinished_old_battle(self):
        bid, _ = self.start()
        ack, _ = self.social_request(31012, {'id': GID})
        self.assertEqual(ack['result'], 0)
        self.join()
        ack, _ = self.finish(bid)
        self.assertNotEqual(ack['result'], 0)
        self.assertEqual(self.stored_run(bid)['state'], 2)

    def test_foreign_player_cannot_settle_another_players_durable_run(self):
        bid, _ = self.start()
        middleware._BATTLE_SESSIONS.clear()
        ack, _ = self.battle_request(54032, {'battle_id': bid}, uid=OTHER)
        self.assertNotEqual(ack['result'], 0)
        self.assertEqual(self.stored_run(bid)['state'], 0)

    def test_personal_best_powers_login_rank_and_atomic_single_reward_claim(self):
        bid, _ = self.start()
        self.finish(bid, milliseconds=6171)
        import social_protocol
        payload = decode(social_protocol.login_payload(self.db, UID, 33001), 'sc_33001')
        self.assertEqual(payload['info']['personal_score'], 158000)
        rank, _ = self.social_request(33006, {'club_id': GID})
        self.assertEqual(rank['info_list'][0]['id'], UID)
        self.assertEqual(rank['info_list'][0]['score'], 158000)
        before = InventoryService.get_item_balance(self.db, UID, 40)[1]
        reward = {'club_id': GID, 'type': 1, 'award_id': 40000}
        ack, frames = self.social_request(33008, reward)
        self.assertEqual(ack['result'], 0, frames)
        self.assertEqual(InventoryService.get_item_balance(self.db, UID, 40)[1], before + 48)
        self.assertIn(40000, dict(frames)[33001]['info']['personal_score_award_admitted'])
        ack, _ = self.social_request(33008, reward)
        self.assertEqual(ack['result'], 6027)
        self.assertEqual(InventoryService.get_item_balance(self.db, UID, 40)[1], before + 48)

    def test_reward_rejection_uses_client_condition_phase_and_missing_tier_messages(self):
        reward = {'club_id': GID, 'type': 1, 'award_id': 40000}
        self.assertEqual(self.social_request(33008, reward)[0]['result'], 2005)
        self.assertEqual(self.social_request(33008, {**reward, 'award_id': 1})[0]['result'], 6026)
        self.clock.return_value = NOW
        self.assertEqual(self.social_request(33008, reward)[0]['result'], 6045)

    def test_migration_is_idempotent_and_preserves_existing_guild_progress_and_assets(self):
        tables = ('users', 'hero', 'currency', 'social_members', 'social_guilds',
                  'social_boss_scores', 'social_prepose_scores', 'social_reward_claims')
        before = {table: self.db.query('SELECT * FROM ' + table) for table in tables}
        self.db.execute('DELETE FROM server_global_config WHERE key=?', (guild_boss.KEY,))
        self.db.execute('DROP TABLE social_boss_runs')
        social.initialize(self.db, self.clock.return_value)
        social.initialize(self.db, self.clock.return_value)
        self.assertEqual(before, {table: self.db.query('SELECT * FROM ' + table) for table in tables})


class BattleDamageTests(unittest.TestCase):
    def body(self, info):
        return (transport._f_varint2(1, 123456) + transport._f_varint2(2, 105)
                + transport._f_varint2(3, 1) + transport._f_bytes(4, info))

    def test_repeated_hero_fields_sum_to_the_native_total_even_after_unknown_fixed_fields(self):
        info = transport._f_varint2(2, 8217) + b'\x99\x01' + b'\0' * 8
        for hero, damage in ((1053, 2082736), (1170, 395247), (1075, 563055)):
            info += transport._f_bytes(10, transport._f_varint2(1, hero) + transport._f_varint2(4, damage))
        bid, uid, result, parsed = transport.parse_battle_result(self.body(info))
        self.assertEqual((bid, uid, result), (123456, 105, 1))
        self.assertEqual(parsed['battle_time'], 8217)
        self.assertEqual(parsed['total_damage'], 3041038)
        self.assertEqual([entry['hero_id'] for entry in parsed['hero_damage']], [1053, 1170, 1075])

    def test_missing_statistics_do_not_create_damage_but_explicit_zero_statistics_do(self):
        info = transport._f_varint2(2, 6000)
        self.assertNotIn('total_damage', transport.parse_battle_result(self.body(info))[3])
        info += transport._f_bytes(10, transport._f_varint2(1, 1011))
        self.assertEqual(transport.parse_battle_result(self.body(info))[3]['total_damage'], 0)

    def test_truncated_hero_damage_message_is_rejected(self):
        info = transport._f_varint2(2, 6000) + transport._f_bytes(10, b'\x08\xf3\x07\x20\x80')
        with self.assertRaises((ValueError, IndexError)):
            transport.parse_battle_result(self.body(info))


if __name__ == '__main__':
    unittest.main()
