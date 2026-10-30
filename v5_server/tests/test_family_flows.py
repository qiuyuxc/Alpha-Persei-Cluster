"""Family management, borrowed heroes and preparation rewards on copied saves."""
from contextlib import ExitStack
import datetime
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

SEED = Path(os.environ['GM_TEST_DB_SEED']).resolve()
SANDBOX = tempfile.TemporaryDirectory(prefix='ag-family-flows-')
BOOTSTRAP = Path(SANDBOX.name) / 'bootstrap.db'
shutil.copyfile(SEED, BOOTSTRAP)
os.environ['ACCOUNT_DB_PATH'] = str(BOOTSTRAP)

from account_db import AccountDB
from codec import decode, encode
from core import Core
import event_bus
import gm_actions
from inventory_service import InventoryService
from middleware import CSRequest, get_battle_session
import middleware
from peripheral_service import GetForeignHeroInfoOp
import social_protocol as protocol
import social_service as social
import social_runtime as runtime

UID, OTHER, THIRD = 2174928301, 264084870, 10001
GID = social.DEFAULT_GUILD
NOW = int(datetime.datetime(2026, 10, 26, 5).timestamp())
HERO, BORROWED = 1011, 1039


class FamilyFlowTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.folder = tempfile.TemporaryDirectory(dir=SANDBOX.name)
        self.addCleanup(self.folder.cleanup)
        path = Path(self.folder.name) / 'player.db'
        shutil.copyfile(SEED, path)
        self.clock = self.stack.enter_context(patch('time.time', return_value=NOW))
        self.stack.enter_context(patch('event_bus.bus.emit'))
        self.stack.enter_context(patch.object(event_bus.EventBus, '_handlers', {}))
        self.stack.enter_context(patch.object(middleware, '_BATTLE_SESSIONS', {}))
        self.stack.enter_context(patch('lazy_timer.lazy_timer.pulse'))
        self.push = self.stack.enter_context(patch('server_net.push_gm_frames', return_value=1))
        self.stack.enter_context(patch('server_net.get_online_uids', return_value=[UID, OTHER, THIRD]))
        self.db = AccountDB(str(path))
        self.addCleanup(self.db.close)
        self.ctx = gm_actions.context(self.db)
        self.ctx.uid = UID
        self.ctx.log = lambda *a, **k: None
        self.core = Core(db=self.db, generator=gm_actions.generator,
                         codec_encode=encode, codec_decode=decode)
        social.initialize(self.db, NOW)
        for uid in (OTHER, THIRD):
            self.db.execute('INSERT OR IGNORE INTO users(uid,nick,level) VALUES(?,?,80)', (uid, str(uid)))
        for uid, hid, level in ((UID, HERO, 28), (OTHER, BORROWED, 47)):
            self.db.execute('INSERT INTO hero(uid,id,unlock,level,star,using_skin,battle_using_skin) '
                            'VALUES(?,?,1,?,300,?,?) ON CONFLICT(uid,id) DO UPDATE SET '
                            'unlock=1,level=excluded.level,star=300,using_skin=excluded.using_skin, '
                            'battle_using_skin=excluded.battle_using_skin', (uid, hid, level, hid, hid))

    def social_request(self, cmd, data=None, uid=UID):
        response = protocol.dispatch(CSRequest(cmd, encode('cs_' + str(cmd), data or {}), uid=uid), self.ctx)
        frames = [(f.cmd, decode(f.payload, 'sc_' + str(f.cmd))) for f in response.frames]
        if os.environ.get('GM_FAMILY_TEST_EVIDENCE'):
            with open(os.environ['GM_FAMILY_TEST_EVIDENCE'], 'a') as out:
                out.write(json.dumps({'test': self.id(), 'uid': uid, 'cmd': cmd,
                                      'request': data or {}, 'frames': frames}, ensure_ascii=False) + '\n')
        return dict(frames)[cmd + 1], frames

    def join(self, uid=UID, gid=GID):
        ack, frames = self.social_request(31010, {'id': gid}, uid)
        self.assertEqual(ack['result'], 0)
        return frames

    def set_assist(self):
        self.join()
        self.join(OTHER)
        ack, _ = self.social_request(33010, {'club_id': GID, 'hero_id_list': [BORROWED]}, OTHER)
        self.assertEqual(ack['result'], 0)

    def pushed_to(self, uid):
        return [(f.cmd, decode(f.payload, 'sc_' + str(f.cmd)))
                for call in self.push.call_args_list if call.args[0] == uid for f in call.args[1]]

    def battle_request(self, cmd, data, frame_packer=None, uid=UID):
        payload = encode('cs_' + str(cmd), data)
        if cmd == 54030:
            # A literal p54 fixture remains valid even when the old server's
            # globally merged hero_info_net_rec has picked the p32 definition.
            def varint(field, value):
                return middleware._tag(field, 0) + middleware._varint(value)
            def message(field, body):
                return middleware._tag(field, 2) + middleware._varint(len(body)) + body
            common = data['common_info']
            body = b''.join(message(1, varint(1, h['hero_id']) + varint(2, h.get('owner_id', 0))
                                    + varint(3, h['hero_type'])) for h in common.get('hero_list', []))
            for field, name in ((2, 'type'), (3, 'dest'), (4, 'activity_id'), (5, 'battle_times'),
                                (7, 'cooperate_unique_skill_id'), (8, 'cooperate_unique_skill_level')):
                body += varint(field, common.get(name, 1 if name == 'battle_times' else 0))
            payload = message(1, body) + varint(2, 0)
        req = CSRequest(cmd, payload, uid=uid, frame_packer=frame_packer)
        result = self.core._run_handler(req, self.ctx, [], [])
        frames = [(f['sc'], decode(f['payload'], 'sc_' + str(f['sc']))) for f in result.frames]
        return dict(frames)[cmd + 1], frames

    def start_prepare(self, level=1, heroes=None, uid=UID):
        body = {'common_info': {'hero_list': heroes or [{'hero_id': HERO, 'hero_type': 1}],
                               'type': 32, 'dest': level, 'activity_id': 32,
                               'battle_times': 1, 'cooperate_unique_skill_id': 0,
                               'cooperate_unique_skill_level': 0}, 'is_restarted_battle': 0}
        ack, frames = self.battle_request(54030, body, uid=uid)
        self.assertEqual(ack['result'], 0, frames)
        return dict(frames)[54007]['battle_start']['battle_id'], frames

    def finish(self, bid, result=1, milliseconds=60000, frame_packer=None, uid=UID):
        session = get_battle_session(bid)
        session['result_data'] = {'result': result, 'info': {'battle_time': milliseconds}}
        return self.battle_request(54032, {'battle_id': bid}, frame_packer, uid)

    def test_join_notifies_existing_members_without_double_counting(self):
        self.join()
        self.push.reset_mock()
        self.join(OTHER)
        pushed = self.pushed_to(UID)
        added = [body['member']['id'] for cmd, body in pushed if cmd == 31985]
        self.assertEqual(added, [OTHER])
        own = next(body for cmd, body in pushed if cmd == 31001)
        self.assertEqual(own['club']['member_count'], 8)
        self.assertLess(next(i for i, f in enumerate(pushed) if f[0] == 31985),
                        next(i for i, f in enumerate(pushed) if f[0] == 31001))

    def test_review_notifies_approved_player_and_existing_member_cache(self):
        self.join()
        self.social_request(31018, {'setting': {'examine_recruit_join_request_switch': 2,
                                              'join_request_limit': 1}})
        self.join(OTHER)
        self.push.reset_mock()
        ack, frames = self.social_request(31032, {'id': OTHER, 'type': 1})
        self.assertEqual(ack['result'], 0)
        self.assertTrue(any(cmd == 31001 for cmd, _ in self.pushed_to(OTHER)))
        self.assertTrue(any(cmd == 31985 and body['member']['id'] == OTHER for cmd, body in frames))

    def test_position_changes_update_actor_and_other_member_lists(self):
        self.join()
        self.join(OTHER)
        self.push.reset_mock()
        ack, frames = self.social_request(31016, {'id': OTHER, 'post': 2})
        self.assertEqual(ack['result'], 0)
        self.assertTrue(any(cmd == 31995 and body['id'] == OTHER for cmd, body in frames))
        self.assertTrue(any(cmd == 31995 and body['id'] == OTHER for cmd, body in self.pushed_to(OTHER)))
        ack, frames = self.social_request(31016, {'id': OTHER, 'post': 1})
        self.assertEqual(ack['result'], 0)
        self.assertTrue(any(cmd == 31997 and body['id'] == OTHER for cmd, body in frames))
        self.assertEqual(social.membership(self.db, UID)['post'], 3)
        self.assertEqual(self.db.get('users', OTHER)['club_post'], 1)

    def test_captain_leave_notifies_successor_position(self):
        self.join()
        self.join(OTHER)
        self.push.reset_mock()
        ack, _ = self.social_request(31012, {'id': GID})
        self.assertEqual(ack['result'], 0)
        pushed = self.pushed_to(OTHER)
        self.assertTrue(any(cmd == 31989 and body['id'] == UID for cmd, body in pushed))
        self.assertTrue(any(cmd == 31997 and body['id'] == OTHER for cmd, body in pushed))

    def test_bot_cannot_take_captain_while_real_members_remain(self):
        self.join()
        bot = self.db.query('SELECT uid FROM social_members WHERE guild_id=? AND is_bot=1 LIMIT 1', (GID,))[0]['uid']
        ack, _ = self.social_request(31016, {'id': bot, 'post': 1})
        self.assertNotEqual(ack['result'], 0)
        self.assertEqual(social.membership(self.db, UID)['post'], 1)

    def test_creating_family_clears_previous_join_applications(self):
        self.join()
        self.social_request(31018, {'setting': {'examine_recruit_join_request_switch': 2,
                                              'join_request_limit': 1}})
        self.join(OTHER)
        ack, _ = self.social_request(31008, {'name': '申请后新建', 'icon': 1, 'notice': '测试'}, OTHER)
        self.assertEqual(ack['result'], 0)
        self.assertFalse(self.db.query('SELECT 1 FROM social_requests WHERE uid=?', (OTHER,)))
        requests = decode(protocol.request_frame(self.db, UID).payload, 'sc_31979')
        self.assertFalse(requests.get('join_list'))

    def test_last_member_leaving_empty_family_does_not_break_recommendations(self):
        self.join()
        bots = self.db.query('SELECT uid FROM social_members WHERE guild_id=? AND is_bot=1', (GID,))
        for bot in bots:
            ack, _ = self.social_request(31014, {'id': bot['uid']})
            self.assertEqual(ack['result'], 0)
        ack, _ = self.social_request(31012, {'id': GID})
        self.assertEqual(ack['result'], 0)
        ack, _ = self.social_request(31006)
        self.assertEqual(ack['result'], 0)
        self.assertFalse(any(r['club_info']['id'] == GID for r in ack.get('recommend_club_list', [])))

    def test_nonexistent_family_search_returns_failure(self):
        ack, _ = self.social_request(31028, {'id': GID + 99})
        self.assertNotEqual(ack['result'], 0)

    def test_saved_assists_are_read_from_current_owner_progress(self):
        self.set_assist()
        ack, _ = self.social_request(33002, {'club_id': GID})
        assists = ack.get('assist_info_list', [])
        found = next((h for h in assists if h['member_id'] == OTHER and h['assist_hero_id'] == BORROWED), None)
        self.assertIsNotNone(found)
        self.assertEqual(found['hero_level'], 47)
        self.db.execute('UPDATE hero SET level=49 WHERE uid=? AND id=?', (OTHER, BORROWED))
        ack, _ = self.social_request(33002, {'club_id': GID})
        self.assertEqual(next(h for h in ack['assist_info_list'] if h['member_id'] == OTHER)['hero_level'], 49)

    def test_locked_placeholder_cannot_be_saved_as_assist(self):
        self.join()
        self.db.execute('UPDATE hero SET unlock=0 WHERE uid=? AND id=?', (UID, HERO))
        ack, _ = self.social_request(33010, {'club_id': GID, 'hero_id_list': [HERO]})
        self.assertNotEqual(ack['result'], 0)
        self.assertEqual(social.membership(self.db, UID)['assist_heroes'], '[]')

    def test_foreign_assist_details_include_real_hero_and_keep_battle_schema(self):
        self.set_assist()
        req = CSRequest(32020, encode('cs_32020', {'user_id': OTHER, 'type': 2}), uid=UID)
        response = self.core._run_operation(GetForeignHeroInfoOp(), req, self.ctx)
        body = decode(next(f['payload'] for f in response.frames if f['sc'] == 32021), 'sc_32021')
        self.assertEqual(body['result'], 0)
        self.assertTrue(body.get('hero_list'), body)
        self.assertEqual(body['hero_list'][0]['hero_base_info']['id'], BORROWED)
        self.assertEqual(body['hero_list'][0]['hero_base_info']['level'], 47)
        # p54's hero_info_net_rec remains the three-field battle request record.
        request = {'common_info': {'hero_list': [{'hero_id': BORROWED, 'owner_id': OTHER, 'hero_type': 3}],
                                  'type': 32, 'dest': 1, 'activity_id': 32,
                                  'cooperate_unique_skill_id': 0, 'cooperate_unique_skill_level': 0},
                   'is_restarted_battle': 0}
        self.assertEqual(decode(encode('cs_54030', request), 'cs_54030')['common_info']['hero_list'][0]['hero_id'], BORROWED)

    def test_assist_battle_loads_owner_progress_instead_of_borrower_row(self):
        self.set_assist()
        bid, frames = self.start_prepare(heroes=[{'hero_id': HERO, 'hero_type': 1},
                                                {'hero_id': BORROWED, 'hero_type': 3, 'owner_id': OTHER}])
        self.assertIn('player_battle_info', dict(frames)[54003]['player_info'])
        battle_heroes = dict(frames)[54003]['player_info']['player_battle_info']['hero_list']
        hero = next(h for h in battle_heroes if h['hero_base_info']['id'] == BORROWED)
        self.assertEqual(hero['hero_base_info']['level'], 47)
        self.assertEqual(hero['hero_type'], 3)

    def test_preparation_requires_membership_and_valid_level(self):
        body = {'common_info': {'hero_list': [{'hero_id': HERO, 'hero_type': 1}], 'type': 32,
                               'dest': 1, 'activity_id': 32, 'cooperate_unique_skill_id': 0,
                               'cooperate_unique_skill_level': 0}, 'is_restarted_battle': 0}
        ack, _ = self.battle_request(54030, body)
        self.assertNotEqual(ack['result'], 0)
        self.join()
        body['common_info']['dest'] = 6
        ack, _ = self.battle_request(54030, body)
        self.assertNotEqual(ack['result'], 0)

    def test_preparation_win_updates_shared_score_daily_quota_and_claim(self):
        self.join()
        balances = {iid: InventoryService.get_item_balance(self.db, UID, iid)[1] for iid in (4, 40, 52003)}
        bid, _ = self.start_prepare()
        ack, frames = self.finish(bid)
        self.assertEqual(ack['result'], 0)
        info = social.boss_info(self.db, UID, GID, NOW)
        self.assertEqual(info['day_clear_prepose_times'], 1)
        self.assertEqual(info['pre_info'][0]['score'], 13600)
        self.assertEqual(InventoryService.get_item_balance(self.db, UID, 4)[1], balances[4])
        # The callback increments locally; its authoritative snapshot follows it.
        self.assertLess(next(i for i, f in enumerate(frames) if f[0] == 54033),
                        next(i for i, f in enumerate(frames) if f[0] == 33001))
        claimed, _ = self.social_request(33008, {'club_id': GID, 'type': 3, 'award_id': 1})
        self.assertEqual(claimed['result'], 0)
        for iid, num in social.CATALOG['prepose']['1']['personal_reward']:
            if iid == 52003:
                self.assertEqual(social.member_wire(self.db, social.membership(self.db, UID), NOW)['total_vitality'], num)
            else:
                self.assertEqual(InventoryService.get_item_balance(self.db, UID, iid)[1], balances[iid] + num)
        self.assertEqual(social.boss_info(self.db, UID, GID, NOW)['day_first_clear_award_admitted'], 1)
        again, _ = self.social_request(33008, {'club_id': GID, 'type': 3, 'award_id': 1})
        self.assertNotEqual(again['result'], 0)

    def test_preparation_daily_limit_and_next_day_reset(self):
        self.join()
        for _ in range(3):
            bid, _ = self.start_prepare()
            ack, _ = self.finish(bid)
            self.assertEqual(ack['result'], 0)
        body = {'common_info': {'hero_list': [{'hero_id': HERO, 'hero_type': 1}], 'type': 32,
                               'dest': 1, 'activity_id': 32, 'cooperate_unique_skill_id': 0,
                               'cooperate_unique_skill_level': 0}, 'is_restarted_battle': 0}
        ack, _ = self.battle_request(54030, body)
        self.assertNotEqual(ack['result'], 0)
        self.clock.return_value = NOW + social.DAY
        self.assertEqual(social.boss_info(self.db, UID, GID, self.clock.return_value)['day_clear_prepose_times'], 0)
        bid, _ = self.start_prepare()
        ack, _ = self.finish(bid)
        self.assertEqual(ack['result'], 0)

    def test_preparation_failure_does_not_count_as_clear(self):
        self.join()
        bid, _ = self.start_prepare()
        ack, _ = self.finish(bid, result=2)
        self.assertEqual(ack['result'], 0)
        self.assertEqual(social.boss_info(self.db, UID, GID, NOW)['day_clear_prepose_times'], 0)
        claimed, _ = self.social_request(33008, {'club_id': GID, 'type': 3, 'award_id': 1})
        self.assertNotEqual(claimed['result'], 0)

    def test_preparation_settlement_packet_failure_can_retry_without_losing_score(self):
        self.join()
        bid, _ = self.start_prepare()
        def fail(*args, **kwargs):
            raise ValueError('injected preparation packing failure')
        ack, _ = self.finish(bid, frame_packer=fail)
        self.assertNotEqual(ack['result'], 0)
        self.assertEqual(social.boss_info(self.db, UID, GID, NOW)['day_clear_prepose_times'], 0)
        ack, _ = self.finish(bid)
        self.assertEqual(ack['result'], 0)
        self.assertEqual(social.boss_info(self.db, UID, GID, NOW)['day_clear_prepose_times'], 1)
        ack, frames = self.finish(bid)
        self.assertEqual(ack['result'], 0)
        self.assertFalse(any(cmd == 33001 for cmd, _ in frames))
        self.assertEqual(social.boss_info(self.db, UID, GID, NOW)['pre_info'][0]['score'], 13600)

    def test_preparation_reply_encoding_failure_restores_run_score_and_quota(self):
        self.join()
        bid, _ = self.start_prepare()
        original = self.ctx.codec_encode
        with patch.object(self.ctx, 'codec_encode',
                          side_effect=lambda name, body: None if name == 'sc_54033' else original(name, body)):
            ack, _ = self.finish(bid)
        self.assertNotEqual(ack['result'], 0)
        self.assertEqual(self.db.query('SELECT state FROM social_prepose_runs WHERE uid=? AND battle_id=?',
                                       (UID, bid))[0]['state'], 0)
        self.assertFalse(self.db.query('SELECT 1 FROM social_prepose_scores WHERE guild_id=? AND uid=?',
                                       (GID, UID)))
        self.assertEqual(social.boss_info(self.db, UID, GID, NOW)['day_clear_prepose_times'], 0)
        ack, _ = self.finish(bid)
        self.assertEqual(ack['result'], 0)
        self.assertEqual(social.boss_info(self.db, UID, GID, NOW)['day_clear_prepose_times'], 1)

    def test_leaving_and_rejoining_cannot_submit_old_preparation(self):
        self.join()
        bid, _ = self.start_prepare()
        self.social_request(31012, {'id': GID})
        self.join()
        ack, _ = self.finish(bid)
        self.assertNotEqual(ack['result'], 0)
        self.assertEqual(social.boss_info(self.db, UID, GID, NOW)['day_clear_prepose_times'], 0)

    def test_preparation_does_not_credit_other_players_battle(self):
        self.join()
        self.join(OTHER)
        bid, _ = self.start_prepare()
        ack, _ = self.finish(bid, uid=OTHER)
        self.assertNotEqual(ack['result'], 0)
        self.assertEqual(social.boss_info(self.db, OTHER, GID, NOW)['day_clear_prepose_times'], 0)

    def test_unlisted_foreign_hero_cannot_be_borrowed(self):
        self.join()
        self.join(OTHER)
        body = {'common_info': {'hero_list': [{'hero_id': HERO, 'hero_type': 1},
                                             {'hero_id': BORROWED, 'hero_type': 3, 'owner_id': OTHER}],
                               'type': 32, 'dest': 1, 'activity_id': 32}, 'is_restarted_battle': 0}
        ack, _ = self.battle_request(54030, body)
        self.assertNotEqual(ack['result'], 0)

    def test_missing_dynamic_hero_data_rolls_back_preparation_start(self):
        self.join()
        body = {'common_info': {'hero_list': [{'hero_id': HERO, 'hero_type': 1}],
                               'type': 32, 'dest': 1, 'activity_id': 32}, 'is_restarted_battle': 0}
        with patch('battle_payload.build_sc_54003_dynamic', return_value=None):
            ack, _ = self.battle_request(54030, body)
        self.assertNotEqual(ack['result'], 0)
        self.assertFalse(self.db.query('SELECT 1 FROM social_prepose_runs WHERE uid=?', (UID,)))

    def test_preparation_first_reward_encoding_failure_restores_assets_and_claim(self):
        self.join()
        bid, _ = self.start_prepare()
        self.finish(bid)
        before = {iid: InventoryService.get_item_balance(self.db, UID, iid)[1] for iid in (40, 52003)}
        original = protocol.encode
        with patch.object(protocol, 'encode', side_effect=lambda n, b: None if n == 'sc_33001' else original(n, b)):
            ack, _ = self.social_request(33008, {'club_id': GID, 'type': 3, 'award_id': 1})
        self.assertNotEqual(ack['result'], 0)
        self.assertEqual(social.boss_info(self.db, UID, GID, NOW)['day_first_clear_award_admitted'], 0)
        self.assertEqual(before, {iid: InventoryService.get_item_balance(self.db, UID, iid)[1] for iid in before})
        ack, _ = self.social_request(33008, {'club_id': GID, 'type': 3, 'award_id': 1})
        self.assertEqual(ack['result'], 0)

    def test_preparation_quota_and_claim_are_shared_across_family_changes(self):
        self.join()
        bid, _ = self.start_prepare()
        self.finish(bid)
        self.social_request(33008, {'club_id': GID, 'type': 3, 'award_id': 1})
        self.social_request(31012, {'id': GID})
        self.social_request(31008, {'name': '换会不重领', 'icon': 1, 'notice': '测试'})
        gid = social.membership(self.db, UID)['guild_id']
        self.assertEqual(social.boss_info(self.db, UID, gid, NOW)['day_clear_prepose_times'], 1)
        ack, _ = self.social_request(33008, {'club_id': gid, 'type': 3, 'award_id': 1})
        self.assertNotEqual(ack['result'], 0)

    def test_preparation_cross_day_or_phase_boundary_cannot_credit_stale_run(self):
        self.join()
        bid, _ = self.start_prepare()
        self.clock.return_value = NOW + social.DAY
        ack, _ = self.finish(bid)
        self.assertNotEqual(ack['result'], 0)
        self.assertEqual(social.boss_info(self.db, UID, GID, self.clock.return_value)['day_clear_prepose_times'], 0)
        self.clock.return_value = NOW + 5 * social.DAY - 1
        bid, _ = self.start_prepare()
        self.clock.return_value += 1
        ack, _ = self.finish(bid)
        self.assertNotEqual(ack['result'], 0)

    def test_preparation_finished_receipt_survives_loss_of_memory_session(self):
        self.join()
        bid, _ = self.start_prepare()
        first, _ = self.finish(bid)
        self.clock.return_value += 120
        with patch.object(middleware, '_BATTLE_SESSIONS', {}):
            replay, frames = self.battle_request(54032, {'battle_id': bid})
        self.assertEqual(replay, first)
        self.assertFalse(any(cmd == 33001 for cmd, _ in frames))
        self.assertEqual(social.boss_info(self.db, UID, GID, self.clock.return_value)['day_clear_prepose_times'], 1)

    def test_preparation_missing_battle_statistics_cannot_invent_score(self):
        self.join()
        bid, _ = self.start_prepare()
        get_battle_session(bid)['result_data'] = {'result': 1, 'info': {}}
        ack, _ = self.battle_request(54032, {'battle_id': bid})
        self.assertNotEqual(ack['result'], 0)
        self.assertEqual(social.boss_info(self.db, UID, GID, NOW)['day_clear_prepose_times'], 0)

    def test_subsecond_preparation_uses_milliseconds_and_shared_score_cap(self):
        self.join()
        bid, _ = self.start_prepare()
        ack, _ = self.finish(bid, milliseconds=900)
        self.assertEqual(ack['result'], 0)
        self.assertEqual(ack['battle_result']['use_seconds'], 0)
        self.assertEqual(social.boss_info(self.db, UID, GID, NOW)['pre_info'][0]['score'], 16000)
        self.db.execute('UPDATE social_prepose_scores SET score=95900 WHERE guild_id=? AND uid=?', (GID, UID))
        bid, _ = self.start_prepare()
        self.finish(bid)
        self.assertEqual(social.boss_info(self.db, UID, GID, NOW)['pre_info'][0]['score'], 96000)

    def test_position_event_encoding_failure_does_not_commit_or_push(self):
        self.join()
        self.join(OTHER)
        self.push.reset_mock()
        original = protocol.encode
        with patch.object(protocol, 'encode', side_effect=lambda n, b: None if n == 'sc_31995' else original(n, b)):
            ack, _ = self.social_request(31016, {'id': OTHER, 'post': 2})
        self.assertNotEqual(ack['result'], 0)
        self.assertEqual(social.membership(self.db, OTHER)['post'], 3)
        self.push.assert_not_called()

    def test_borrowed_foreign_equipment_uses_prefab_and_preserves_enchantments(self):
        self.set_assist()
        slots = [{'id': 1, 'effect_list': [{'id': 101, 'level': 2}]}, {'id': 2, 'effect_list': []}]
        self.db.execute('DELETE FROM equip WHERE uid=? AND hero_id=?', (OTHER, BORROWED))
        self.db.execute('INSERT INTO equip(uid,id,prefab_id,hero_id,exp,now_break_level,enchant_slots,race) '
                        'VALUES(?,?,?,?,?,?,?,?)', (OTHER, 987654, 510001, BORROWED, 1234, 2, json.dumps(slots), 7))
        self.db.execute('UPDATE hero SET equip_slot=? WHERE uid=? AND id=?',
                        (json.dumps({'1': 987654}), OTHER, BORROWED))
        req = CSRequest(32020, encode('cs_32020', {'user_id': OTHER, 'type': 2}), uid=UID)
        response = self.core._run_operation(GetForeignHeroInfoOp(), req, self.ctx)
        body = decode(next(f['payload'] for f in response.frames if f['sc'] == 32021), 'sc_32021')
        self.assertTrue(body.get('hero_list'), body)
        gear = body['hero_list'][0]['equip_list'][0]
        self.assertEqual(gear['equip_id'], 510001)
        self.assertEqual((gear['exp'], gear['now_break_level'], gear['race']), (1234, 2, 7))
        # Protobuf omits an empty repeated effect_list; the empty slot itself
        # and every nonempty effect must still survive the foreign-hero reply.
        self.assertEqual([(slot['id'], slot.get('effect_list', [])) for slot in gear['enchant_slot_list']],
                         [(slot['id'], slot['effect_list']) for slot in slots])
        _, frames = self.start_prepare(heroes=[{'hero_id': BORROWED, 'hero_type': 3, 'owner_id': OTHER}])
        battle_gear = dict(frames)[54003]['player_info']['player_battle_info']['hero_list'][0]['equip_list'][0]
        self.assertEqual((battle_gear['equip_id'], battle_gear['prefab_id']), (987654, 510001))

    def test_online_daily_reset_updates_limits_when_npcs_are_disabled(self):
        self.join()
        social.save_settings(self.db, {'enabled': False})
        bid, _ = self.start_prepare()
        self.finish(bid)
        with patch.object(runtime, '_activity_weeks', {}), patch.object(runtime, '_activity_days', {}, create=True):
            runtime.notify_guild_windows(self.db, [UID], NOW, self.push)
            self.push.reset_mock()
            runtime.notify_guild_windows(self.db, [UID], NOW + social.DAY, self.push)
            pushed = self.pushed_to(UID)
            self.assertEqual([cmd for cmd, _ in pushed], [33001])
            self.assertEqual(pushed[0][1]['day_clear_prepose_times'], 0)
            runtime.notify_guild_windows(self.db, [UID], NOW + social.DAY, self.push)
            self.assertEqual(self.push.call_count, 1)

    def test_preparation_schema_migration_preserves_original_tables(self):
        import guild_prepose
        self.join()
        tables = ('users', 'hero', 'currency', 'social_guilds', 'social_members',
                  'social_contributions', 'social_prepose_scores', 'social_boss_scores', 'social_reward_claims')
        before = {table: self.db.query('SELECT * FROM ' + table) for table in tables}
        self.db.execute('DELETE FROM server_global_config WHERE key=?', (guild_prepose.KEY,))
        self.db.execute('DROP TABLE social_prepose_daily')
        self.db.execute('DROP TABLE social_prepose_runs')
        guild_prepose.initialize(self.db)
        guild_prepose.initialize(self.db)
        self.assertEqual(before, {table: self.db.query('SELECT * FROM ' + table) for table in tables})
        self.assertEqual(social.boss_info(self.db, UID, GID, NOW)['day_clear_prepose_times'], 0)

    def test_unavailable_foreign_assists_do_not_report_success_and_launch_callback(self):
        self.set_assist()
        self.db.execute('UPDATE hero SET unlock=0 WHERE uid=? AND id=?', (OTHER, BORROWED))
        req = CSRequest(32020, encode('cs_32020', {'user_id': OTHER, 'type': 2}), uid=UID)
        response = self.core._run_operation(GetForeignHeroInfoOp(), req, self.ctx)
        body = decode(next(f['payload'] for f in response.frames if f['sc'] == 32021), 'sc_32021')
        self.assertNotEqual(body['result'], 0)


if __name__ == '__main__':
    unittest.main()
