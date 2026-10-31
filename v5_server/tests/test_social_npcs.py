"""NPC contracts on disposable databases; all model calls are injected."""
from concurrent.futures import ThreadPoolExecutor
import datetime
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_seed import get_seed_path

SEED = get_seed_path()
SANDBOX = tempfile.TemporaryDirectory(prefix='ag-social-tests-')
BOOTSTRAP = Path(SANDBOX.name) / 'bootstrap.db'
shutil.copyfile(SEED, BOOTSTRAP)
os.environ['ACCOUNT_DB_PATH'] = str(BOOTSTRAP)

from account_db import AccountDB
from codec import encode, decode
from middleware import CSRequest
from inventory_service import InventoryService
import gm_actions
import social_service as social
import social_protocol as protocol
import social_runtime as runtime

UID, OTHER, THIRD = 2174928301, 10001, 264084870
NOW = int(datetime.datetime(2026, 10, 26, 5).timestamp())
GID = social.DEFAULT_GUILD
MODEL = {'enabled': True, 'provider': 'test', 'providers': {'test': {'api_key': 'test-only-key', 'type': 'openai'}}}


class SocialTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=SANDBOX.name)
        self.path = Path(self.folder.name) / 'player.db'
        shutil.copyfile(SEED, self.path)
        self.clock = patch('time.time', return_value=NOW).start()
        self.db = AccountDB(str(self.path))
        patch('event_bus.bus.emit').start()
        self.push = patch('server_net.push_gm_frames', return_value=1).start()
        patch('server_net.get_online_uids', return_value=[UID, OTHER]).start()
        self.ctx = gm_actions.context(self.db)
        social.initialize(self.db, NOW)

    def tearDown(self):
        patch.stopall()
        self.db.close()
        self.folder.cleanup()

    def scalar(self, sql, args=()):
        return next(iter(self.db.query(sql, args)[0].values()))

    def dispatch(self, cmd, data=None, uid=UID):
        req = CSRequest(cmd=cmd, payload=encode('cs_' + str(cmd), data or {}), uid=uid)
        return protocol.dispatch(req, self.ctx)

    def ack(self, cmd, data=None, uid=UID):
        result = self.dispatch(cmd, data, uid)
        packet = next(f for f in result.frames if f.cmd == cmd + 1)
        return decode(packet.payload, 'sc_' + str(cmd + 1))

    def join(self, uid=UID, gid=GID):
        self.assertEqual(self.ack(31010, {'id': gid}, uid)['result'], 0)

    def advance(self, now):
        with self.db.transaction(immediate=True):
            return social.advance_guilds(self.db, now, social.settings(self.db))

    def job(self, now=NOW, channel=1, uid=UID):
        with self.db.transaction(immediate=True):
            social.send_message(self.db, uid, channel, '请问今天推进什么？', now_ts=now)
        return runtime.claim_job(self.db, now + 4, [uid])

    def test_initialization_preserves_players_and_is_idempotent(self):
        before = {t: self.db.query('SELECT * FROM ' + t) for t in ('users', 'currency', 'hero', 'auth_accounts', 'ai_characters')}
        self.assertEqual(len(social.status(self.db, NOW)['bots']), 6)
        self.assertTrue(all('人机' in b['name'] for b in social.status(self.db, NOW)['bots']))
        social.initialize(self.db, NOW + social.DAY)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM social_guilds'), 1)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM social_messages'), 1)
        self.assertEqual(before, {t: self.db.query('SELECT * FROM ' + t) for t in before})
        self.assertFalse(social.membership(self.db, UID))
        for cmd in (27005, 27007, 27023, 31979, 33001):
            self.assertIsNotNone(decode(protocol.login_payload(self.db, UID, cmd), 'sc_' + str(cmd)))
        with self.assertRaises(ValueError):
            protocol.login_payload(self.db, UID, 31001)

    def test_membership_authorization_and_successor(self):
        self.join()
        self.join(OTHER)
        self.assertEqual(social.membership(self.db, UID)['post'], 1)
        self.assertEqual(social.membership(self.db, OTHER)['post'], 3)
        self.assertNotEqual(self.ack(31024, {'notice': 'unauthorized'}, OTHER)['result'], 0)
        self.assertEqual(self.ack(31012, {'id': GID})['result'], 0)
        self.assertEqual(social.membership(self.db, OTHER)['post'], 1)
        self.assertFalse(social.membership(self.db, UID))
        self.assertEqual(self.scalar('SELECT club_id FROM users WHERE uid=?', (UID,)), 0)
        self.assertEqual(self.scalar('SELECT club_post FROM users WHERE uid=?', (OTHER,)), 1)

    def test_request_review_and_guild_privacy(self):
        self.join()
        self.assertEqual(self.ack(31018, {'setting': {'examine_recruit_join_request_switch': 2, 'join_request_limit': 1}})['result'], 0)
        self.join(OTHER)
        self.assertFalse(social.membership(self.db, OTHER))
        requests = decode(protocol.request_frame(self.db, UID).payload, 'sc_31979')['join_list']
        self.assertEqual([r['id'] for r in requests], [OTHER])
        self.assertFalse(decode(protocol.request_frame(self.db, OTHER).payload, 'sc_31979').get('join_list'))
        self.assertNotEqual(self.ack(31032, {'id': OTHER, 'type': 1}, OTHER)['result'], 0)
        self.assertEqual(self.ack(31032, {'id': OTHER, 'type': 1})['result'], 0)
        self.assertEqual(social.membership(self.db, OTHER)['guild_id'], GID)

    def test_chat_isolation_rate_limit_and_bot_loop_prevention(self):
        self.join()
        self.assertEqual(self.ack(31008, {'name': '另一支援站', 'icon': 2, 'notice': '测试'}, OTHER)['result'], 0)
        self.assertEqual(self.ack(27020, {'type': 1, 'content': '本会私密测试'})['result'], 0)
        self.assertFalse(social.history(self.db, OTHER, 3))
        self.assertNotIn(OTHER, protocol.recipients(self.db, 3, GID, [UID, OTHER]))
        self.assertNotEqual(self.ack(27500, {'channel_type': 3, 'timestamp': 0}, THIRD)['result'], 0)
        self.assertNotEqual(self.ack(27014, {'type': 1, 'content': '太快'})['result'], 0)
        self.clock.return_value = NOW + 5
        self.assertEqual(self.ack(27010, {'room_id': 2}, OTHER)['result'], 0)
        self.assertEqual(self.ack(27014, {'type': 1, 'content': '房间一'})['result'], 0)
        self.assertFalse(social.history(self.db, OTHER, 1))
        before = self.db.query('SELECT * FROM social_reply_jobs')
        with self.db.transaction():
            social._append(self.db, 1, 1, 90001001, '人机不触发回复', NOW + 6, is_bot=True)
        self.assertEqual(self.db.query('SELECT * FROM social_reply_jobs'), before)
        for cmd, data in ((27014, {'type': 1, 'content': '<size=500>bad</size>'}),
                          (27014, {'type': 2, 'content': '999999999'}),
                          (27030, {'channel_type': 2, 'type': 1, 'content': 'bad'})):
            self.assertNotEqual(self.ack(cmd, data)['result'], 0)

    def test_codec_failure_rolls_back_membership_and_does_not_push(self):
        original = protocol.encode
        with patch.object(protocol, 'encode', side_effect=lambda name, body: None if name == 'sc_31001' else original(name, body)):
            self.assertNotEqual(self.ack(31010, {'id': GID})['result'], 0)
        self.assertFalse(social.membership(self.db, UID))
        self.push.assert_not_called()

    def test_progress_daily_weekly_and_exactly_once_across_restart(self):
        self.join()
        complete = NOW + 7 * social.DAY - 3600
        self.advance(complete)
        info = social.boss_info(self.db, UID, GID, complete)['info']
        self.assertEqual(info['total_damage'], 400000)
        self.assertEqual(info['personal_score'], 0)
        ranks = social.boss_rank(self.db, UID, GID, complete)
        self.assertEqual(sum(r['score'] for r in ranks), info['total_damage'])
        self.assertNotIn(UID, [r['id'] for r in ranks])
        self.assertGreater(len({r['score'] for r in ranks}), 1)
        for day in (NOW, NOW + social.DAY):
            values = self.db.query('SELECT SUM(vitality) v,SUM(donation) d,SUM(sign_in) s FROM social_contributions WHERE day_id=?', (day,))[0]
            self.assertEqual(values, {'v': 600, 'd': 120, 's': 6})
        before = {t: self.db.query('SELECT * FROM ' + t) for t in ('social_guilds', 'social_contributions', 'social_boss_scores')}
        self.assertFalse(self.advance(complete + 1))
        self.assertFalse(self.advance(NOW + social.DAY))
        reopened = AccountDB(str(self.path))
        try:
            with reopened.transaction(immediate=True):
                self.assertFalse(social.advance_guilds(reopened, complete, social.settings(reopened)))
        finally:
            reopened.close()
        self.assertEqual(before, {t: self.db.query('SELECT * FROM ' + t) for t in before})
        self.advance(NOW + 7 * social.DAY)
        self.assertEqual(social.boss_info(self.db, UID, GID, NOW + 7 * social.DAY)['info']['total_damage'], 0)

    def test_catchup_is_bounded_and_weekly_experience_is_capped(self):
        social.save_settings(self.db, {'daily_vitality': 5000})
        self.advance(NOW + 6 * social.DAY + 23 * 3600)
        self.assertEqual(self.scalar('SELECT exp FROM social_guilds WHERE id=?', (GID,)), 12000)
        self.advance(NOW + 100 * social.DAY)
        self.assertLessEqual(self.scalar('SELECT COUNT(DISTINCT day_id) FROM social_contributions'), 14)

    def test_disabled_activity_has_no_chat_or_progress(self):
        social.save_settings(self.db, {'enabled': False})
        before = self.scalar('SELECT COUNT(*) FROM social_messages')
        self.assertEqual(runtime.activity(self.db, NOW + social.DAY, [UID]), {'messages': [], 'guilds': []})
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM social_messages'), before)
        self.assertFalse(self.job(NOW + social.DAY))

    def test_basic_activity_offline_does_not_replay_backlog(self):
        self.assertFalse(runtime.activity(self.db, NOW + social.DAY, [])['messages'])
        rows = runtime.activity(self.db, NOW + social.DAY, [UID])['messages']
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]['is_bot'])
        self.assertFalse(runtime.activity(self.db, NOW + social.DAY + 30, [UID])['messages'])
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM social_api_budget'), 0)

    def test_reward_conditions_atomic_assets_and_no_double_claim(self):
        self.join()
        body = {'club_id': GID, 'type': 2, 'award_id': 25}
        self.assertNotEqual(self.ack(33008, body)['result'], 0)
        self.clock.return_value = NOW + 7 * social.DAY - 3600
        self.advance(self.clock.return_value)
        reward = social.CATALOG['bosses']['1']['reward'][0][1]
        before = {item: InventoryService.get_item_balance(self.db, UID, item)[1] for item, _ in reward}
        prior_vitality = social.member_wire(self.db, social.membership(self.db, UID), self.clock.return_value)['total_vitality']
        prior_guild = social.guild_wire(self.db, GID, UID, self.clock.return_value)
        self.assertEqual(self.ack(33008, body)['result'], 0)
        for item, count in reward:
            if item == 52003:
                self.assertEqual(social.member_wire(self.db, social.membership(self.db, UID), self.clock.return_value)['total_vitality'], prior_vitality + count)
            elif item in (52001,52002):
                field = 'weekly_gain_coin' if item == 52001 else 'club_exp'
                self.assertEqual(social.guild_wire(self.db, GID, UID, self.clock.return_value)[field], prior_guild[field] + count)
            else:
                self.assertEqual(InventoryService.get_item_balance(self.db, UID, item)[1], before[item] + count)
        self.assertNotEqual(self.ack(33008, body)['result'], 0)
        self.assertNotEqual(self.ack(33008, {**body, 'type': 1, 'award_id': 30000})['result'], 0)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM social_reward_claims'), 1)
        self.ack(31012, {'id': GID})
        self.ack(31008, {'name': '换会测试', 'icon': 1, 'notice': 'test'})
        gid = social.membership(self.db, UID)['guild_id']
        with self.db.transaction():
            self.db.execute('INSERT INTO social_boss_scores VALUES(?,?,?,?,?,?)', (gid, 90001001, NOW, 1, 400000, NOW))
        self.assertNotEqual(self.ack(33008, {**body, 'club_id': gid})['result'], 0)

    def test_reward_codec_rollback_restores_pending_notifications(self):
        self.join()
        self.clock.return_value = NOW + 7 * social.DAY - 3600
        self.advance(self.clock.return_value)
        before = self.db.query('SELECT * FROM currency WHERE uid=?', (UID,))
        self.ctx.touched_items.add(1)
        original = protocol.encode
        with patch.object(protocol, 'encode', side_effect=lambda name, body: None if name == 'sc_33001' else original(name, body)):
            self.assertNotEqual(self.ack(33008, {'club_id': GID, 'type': 2, 'award_id': 25})['result'], 0)
        self.assertEqual(before, self.db.query('SELECT * FROM currency WHERE uid=?', (UID,)))
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM social_reward_claims'), 0)
        self.assertEqual(self.ctx.touched_items, {1})
        self.assertFalse(self.ctx.pending_frames)

    def test_reply_budget_failed_attempts_restart_and_daily_reset(self):
        social.save_settings(self.db, {'api_daily_limit': 2})
        llm = Mock(side_effect=RuntimeError('secret-provider-error'))
        row = runtime.run_reply(self.db, self.job(), NOW + 4, llm, MODEL)
        self.assertIn(row['content'], social.FALLBACK_LINES)
        usage = social.status(self.db, NOW)['api_usage']
        self.assertEqual((usage['reserved_requests'], usage['failed']), (2, 1))
        reopened = AccountDB(str(self.path))
        try:
            self.assertEqual(social.status(reopened, NOW)['api_usage']['reserved_requests'], 2)
        finally:
            reopened.close()
        row = runtime.run_reply(self.db, self.job(NOW + 601), NOW + 605, llm, MODEL)
        self.assertIn(row['content'], social.FALLBACK_LINES)
        self.assertEqual(llm.call_count, 1)
        runtime.run_reply(self.db, self.job(NOW + social.DAY), NOW + social.DAY + 4, llm, MODEL)
        self.assertEqual(llm.call_count, 2)

    def test_missing_disabled_provider_and_zero_limit_never_call_api(self):
        llm = Mock(return_value='answer')
        for i, cfg in enumerate(({}, {**MODEL, 'enabled': False}, MODEL)):
            if i == 2:
                social.save_settings(self.db, {'api_daily_limit': 0})
            row = runtime.run_reply(self.db, self.job(NOW + i * 601), NOW + i * 601 + 4, llm, cfg)
            self.assertIn(row['content'], social.FALLBACK_LINES)
        llm.assert_not_called()

    def test_reply_context_and_output_caps_are_scoped(self):
        self.join()
        with self.db.transaction():
            social._append(self.db, 1, 2, OTHER, 'OTHER_ROOM_SECRET', NOW)
            social._append(self.db, 3, GID + 1, OTHER, 'OTHER_GUILD_SECRET', NOW)
        llm = Mock(return_value='<rich>' + '答' * 300)
        row = runtime.run_reply(self.db, self.job(channel=3), NOW + 4, llm, MODEL)
        prompt, messages = llm.call_args.args
        cfg = llm.call_args.kwargs['cfg']
        self.assertNotIn('SECRET', json.dumps(messages))
        self.assertLessEqual(len(messages[0]['content']), 1000)
        self.assertEqual(cfg['max_tokens'], 128)
        self.assertEqual(cfg['timeout_seconds'], 12)
        self.assertLessEqual(len(row['content']), 160)
        self.assertNotIn('<', row['content'])
        self.assertIn('人机', prompt)
        self.assertEqual((row['channel'], row['scope_id']), (3, GID))

    def test_offline_expired_and_left_guild_jobs_drop(self):
        with self.db.transaction():
            social.send_message(self.db, UID, 1, '离线', now_ts=NOW)
        self.assertFalse(runtime.claim_job(self.db, NOW + 4, []))
        with self.db.transaction():
            social.send_message(self.db, UID, 1, '过期', now_ts=NOW + 601)
        self.assertFalse(runtime.claim_job(self.db, NOW + 800, [UID]))
        self.join()
        job = self.job(NOW + 1202, channel=3)
        with self.db.transaction():
            social.leave_guild(self.db, UID, GID)
        # A reply already in flight is discarded if membership changes.
        self.assertFalse(runtime.run_reply(self.db, job, NOW + 1206, Mock(return_value='test'), MODEL))

    def test_concurrent_chat_and_claim_have_one_global_job(self):
        def send(uid):
            try:
                with self.db.transaction(immediate=True):
                    return social.send_message(self.db, uid, 1, '并发测试', now_ts=NOW)
            finally:
                self.db.close()
        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(len(list(pool.map(send, (UID, OTHER)))), 2)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM social_reply_jobs'), 1)
        def claim(_):
            try:
                return runtime.claim_job(self.db, NOW + 4, [UID, OTHER])
            finally:
                self.db.close()
        with ThreadPoolExecutor(4) as pool:
            jobs = list(pool.map(claim, range(4)))
        self.assertEqual(sum(bool(j) for j in jobs), 1)

    def test_settings_validate_and_do_not_expose_provider_secret(self):
        before = social.settings(self.db)
        for change in ({'api_daily_limit': True}, {'api_daily_limit': -1}, {'reply_minutes': 1},
                       {'reply_tokens': 999}, {'enabled': 'yes'}, {'unknown': 1}, []):
            with self.assertRaises(ValueError):
                social.save_settings(self.db, change)
        self.assertEqual(social.settings(self.db), before)
        status = json.dumps(social.status(self.db, NOW))
        self.assertNotIn('api_key', status)
        self.assertNotIn('system_prompt', status)

    def test_admin_api_authentication_and_persistent_settings(self):
        from flask import Flask
        import auth_api
        import gm_api
        app = Flask(__name__)
        auth_api.install(app, self.db)
        app.register_blueprint(gm_api.gm_bp, url_prefix='/api/gm')
        auth = app.extensions['local_auth']
        auth.create('990001', 'SocialTestPassword42', auth.bootstrap_token(UID))
        auth.create('990002', 'SocialTestPassword42')
        client = app.test_client()
        self.assertEqual(client.get('/api/gm/social/status').status_code, 401)
        user = auth.login('990002', 'SocialTestPassword42')['token']
        admin = auth.login('990001', 'SocialTestPassword42')['token']
        with patch('gm_api.get_db', return_value=self.db), patch('runtime_scheduler.wake'):
            self.assertEqual(client.post('/api/gm/social/settings', json={'enabled': False},
                                         headers={'Authorization': 'Bearer ' + user}).status_code, 403)
            headers = {'Authorization': 'Bearer ' + admin}
            result = client.post('/api/gm/social/settings', json={'api_daily_limit': 8}, headers=headers)
            self.assertEqual(result.get_json()['code'], 0)
            data = client.get('/api/gm/social/status', headers=headers).get_json()['data']
            self.assertEqual(data['settings']['api_daily_limit'], 8)
            bad = client.post('/api/gm/social/settings', json={'api_daily_limit': 'unlimited'}, headers=headers)
            self.assertNotEqual(bad.get_json()['code'], 0)
        self.assertEqual(social.settings(self.db)['api_daily_limit'], 8)

    def test_social_commands_bypass_legacy_templates(self):
        import core
        engine = core.Core(db=self.db, generator=gm_actions.generator, codec_encode=encode, codec_decode=decode)
        with patch('lazy_timer.lazy_timer.pulse'), patch('core._dispatch', side_effect=AssertionError('legacy dispatch')):
            req = core.CoreRequest(cmd=27014, payload=encode('cs_27014', {'type': 1, 'content': 'core integration'}), uid=UID)
            result = engine.dispatch(req)
        self.assertEqual(decode(next(f for f in result.frames if f['sc'] == 27015)['payload'], 'sc_27015')['result'], 0)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM social_messages WHERE is_bot=0'), 1)

    def login_packets(self):
        import login
        import struct
        import zlib
        rows = [r for r in login.load_login_push(self.db) if r[1] in (31001, 33001)]
        self.assertEqual(next(r[4] for r in rows if r[1] == 33001), 0)
        sock = Mock()
        with patch('login.load_login_push', return_value=rows), patch('lazy_timer.lazy_timer.pulse'), \
             patch('res_version_manager.get_current_version', return_value='321'), \
             patch('ai_translator.AITranslator.get_instance'):
            login.send_login_flood(sock, self.db, UID, gm_actions.generator, frame_delay=0)
        packets = {}
        for call in sock.sendall.call_args_list:
            data = call.args[0]
            cmd = struct.unpack('>H', data[5:7])[0]
            body = zlib.decompress(data[11:]) if data[2] == 1 else data[11:]
            packets[cmd] = decode(body, 'sc_' + str(cmd))
        return packets

    def test_login_uses_live_boss_and_restores_chat_room_without_static_replay(self):
        self.join()
        self.clock.return_value = NOW + 7 * social.DAY - 3600
        self.advance(self.clock.return_value)
        self.ack(27010, {'room_id': 2})
        packets = self.login_packets()
        self.assertEqual(packets[31001]['club']['id'], GID)
        self.assertEqual(packets[33001]['info']['total_damage'], 400000)
        self.assertEqual(packets[33001]['info']['personal_score'], 0)
        self.assertEqual(packets[27005]['room_id'], 2)
        self.assertIn(27007, packets)
        self.assertIn(27023, packets)
        self.assertIn(31979, packets)

    def test_unjoined_login_never_sends_empty_club_or_captured_membership(self):
        packets = self.login_packets()
        self.assertNotIn(31001, packets)
        self.assertEqual(packets[27005]['room_id'], 1)
        self.assertIn(27007, packets)
        self.assertIn(27023, packets)
        self.assertEqual(packets[33001]['info']['total_damage'], 0)
        self.assertFalse(social.membership(self.db, UID))

    def test_stale_membership_refresh_cannot_open_empty_guild_main(self):
        response = self.dispatch(31002)
        ack = decode(response.frames[0].payload, 'sc_31003')
        self.assertNotEqual(ack['result'], 0)
        cleared = next(f for f in response.frames if f.cmd == 31989)
        self.assertEqual(decode(cleared.payload, 'sc_31989')['id'], UID)

    def test_rift_calendar_has_disjoint_phases_and_no_far_future_countdown(self):
        import guild_cycle
        for now in (NOW, NOW + social.DAY, NOW + 5 * social.DAY - 1, NOW + 5 * social.DAY,
                    NOW + 7 * social.DAY - 1, NOW + 7 * social.DAY):
            week, change, end = guild_cycle.window(now)
            self.assertEqual(change - week, 5 * social.DAY)
            self.assertEqual(end - week, 7 * social.DAY)
            self.assertEqual(datetime.datetime.fromtimestamp(week).weekday(), 0)
            self.assertEqual(datetime.datetime.fromtimestamp(change).weekday(), 5)
            self.assertEqual(datetime.datetime.fromtimestamp(week).hour, 5)
            active = [a for a in guild_cycle.activities(now)[:2] if a['start_time'] <= now < a['stop_time']]
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0]['activity_id'], 102 if now < change else 103)
            self.assertGreater(active[0]['stop_time'], now)
            self.assertLessEqual(active[0]['stop_time'] - now, 5 * social.DAY)
        self.clock.return_value = NOW + social.DAY
        data = decode(gm_actions.generator.gen_payload(11001, uid=UID, db=self.db, res_version='321'), 'sc_11001')
        rows = {a['activity_id']: a for a in data['activity_list']}
        self.assertEqual((rows[102]['start_time'], rows[102]['stop_time']), (NOW, NOW + 5 * social.DAY))
        self.assertEqual((rows[103]['start_time'], rows[103]['stop_time']), (NOW + 5 * social.DAY, NOW + 7 * social.DAY))
        self.assertEqual(rows[104]['stop_time'], NOW + 7 * social.DAY)
        original = [dict(activity_id=999, start_time=1, stop_time=2440962000, template=99),
                    dict(activity_id=102, start_time=1, stop_time=2440962000, template=57)]
        updated = guild_cycle.apply_windows(original, NOW)
        self.assertEqual(updated[0], original[0])
        self.assertEqual(sum(a['activity_id'] == 102 for a in updated), 1)

    def test_preparation_and_boss_progress_follow_their_own_phases(self):
        self.join()
        self.advance(NOW + 2 * social.DAY)
        prep = social.boss_info(self.db, UID, GID, NOW + 2 * social.DAY)
        self.assertEqual(prep['info']['total_damage'], 0)
        for row in prep['pre_info']:
            self.assertEqual(row['score'], social.CATALOG['prepose'][str(row['level_id'])]['point_max'] * 2 // 5)
        self.advance(NOW + 5 * social.DAY)
        ready = social.boss_info(self.db, UID, GID, NOW + 5 * social.DAY)
        self.assertEqual(ready['info']['total_damage'], 0)
        for row in ready['pre_info']:
            self.assertEqual(row['score'], social.CATALOG['prepose'][str(row['level_id'])]['point_max'])
        self.advance(NOW + 6 * social.DAY)
        challenge = social.boss_info(self.db, UID, GID, NOW + 6 * social.DAY)
        self.assertGreater(challenge['info']['total_damage'], 0)
        self.assertEqual(challenge['pre_info'], ready['pre_info'])
        stored = self.db.query('SELECT * FROM social_prepose_scores')
        self.advance(NOW + 6 * social.DAY + 1)
        self.assertEqual(self.db.query('SELECT * FROM social_prepose_scores'), stored)
        self.advance(NOW + 7 * social.DAY)
        fresh = social.boss_info(self.db, UID, GID, NOW + 7 * social.DAY)
        self.assertEqual(fresh['info']['total_damage'], 0)
        self.assertTrue(all(r['score'] == 0 for r in fresh['pre_info']))
        self.assertEqual(len(self.db.query('SELECT * FROM social_prepose_scores WHERE week_id=?', (NOW,))), len(stored))

    def test_rift_migration_preserves_existing_contributions_rewards_and_assets(self):
        import guild_cycle
        self.join()
        with self.db.transaction():
            self.db.execute('DELETE FROM server_global_config WHERE key=?', (guild_cycle.KEY,))
            self.db.execute('DROP TABLE social_prepose_scores')
            self.db.execute('INSERT INTO social_boss_scores VALUES(?,?,?,?,?,?)', (GID,90001001,NOW,1,100000,NOW))
        tables = ('social_guilds','social_members','social_boss_scores','social_contributions',
                  'social_reward_claims','social_api_budget','users','currency')
        before = {t: self.db.query('SELECT * FROM ' + t) for t in tables}
        guild_cycle.initialize(self.db, NOW + social.DAY)
        info = social.boss_info(self.db, UID, GID, NOW + social.DAY)
        for row in info['pre_info']:
            self.assertEqual(row['score'], social.CATALOG['prepose'][str(row['level_id'])]['point_max'] // 4)
        stored = self.db.query('SELECT * FROM social_prepose_scores')
        guild_cycle.initialize(self.db, NOW + 2 * social.DAY)
        self.assertEqual(stored, self.db.query('SELECT * FROM social_prepose_scores'))
        self.assertEqual(before, {t: self.db.query('SELECT * FROM ' + t) for t in tables})

    def test_rift_week_boundary_notifies_online_users_even_when_npcs_disabled(self):
        self.join()
        social.save_settings(self.db, {'enabled': False})
        runtime._activity_weeks.clear()
        pushed = []
        def push(uid, packets):
            pushed.extend(packets)
            return 1
        runtime.notify_guild_windows(self.db, [UID], NOW, push)
        self.assertEqual(sum(f.cmd == 11003 for f in pushed), 3)
        self.assertTrue(any(f.cmd == 33001 for f in pushed))
        pushed.clear()
        runtime.notify_guild_windows(self.db, [UID], NOW + 5 * social.DAY, push)
        self.assertEqual([f.cmd for f in pushed], [33001])
        pushed.clear()
        runtime.notify_guild_windows(self.db, [UID], NOW + 7 * social.DAY, push)
        self.assertEqual(sum(f.cmd == 11003 for f in pushed), 3)
        payload = decode(next(f.payload for f in pushed if f.cmd == 11003), 'sc_11003')
        self.assertEqual(payload['activity']['start_time'], NOW + 7 * social.DAY)
        runtime._activity_weeks.clear()
        failed = Mock(return_value=0)
        runtime.notify_guild_windows(self.db, [UID], NOW, failed)
        runtime.notify_guild_windows(self.db, [UID], NOW, failed)
        self.assertEqual(failed.call_count, 2)
        runtime._activity_weeks.clear()

    def test_difficulty_unlock_survives_week_reset_and_challenge_cannot_change_it(self):
        self.join()
        self.clock.return_value = NOW + 7 * social.DAY - 3600
        self.advance(self.clock.return_value)
        self.assertNotEqual(self.ack(33004, {'difficulty': 2})['result'], 0)
        self.clock.return_value = NOW + 7 * social.DAY
        self.advance(self.clock.return_value)
        info = social.boss_info(self.db, UID, GID, self.clock.return_value)
        self.assertEqual(info['info']['total_damage'], 0)
        self.assertIn(1, info['info']['cleared_difficulty_list'])
        self.assertEqual(self.ack(33004, {'difficulty': 2})['result'], 0)
        self.assertNotEqual(self.ack(33008, {'club_id': GID, 'type': 2, 'award_id': 25})['result'], 0)


if __name__ == '__main__':
    unittest.main()
