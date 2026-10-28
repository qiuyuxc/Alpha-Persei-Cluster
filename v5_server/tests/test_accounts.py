"""Real account, UID isolation, login packet, and progression contracts.

Run against a complete database COPY using GM_TEST_DB_SEED. No live save changes.
"""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

SEED = Path(os.environ['GM_TEST_DB_SEED']).resolve()
WORK = tempfile.TemporaryDirectory(prefix='ag-account-tests-')
BOOT = Path(WORK.name) / 'bootstrap.db'
shutil.copyfile(SEED, BOOT)
os.environ['ACCOUNT_DB_PATH'] = str(BOOT)

from account_db import AccountDB
from auth_service import AuthError, get_service, token_hash
import auth_protocol
import codec
import generator
import login
import fresh_profile
import res_version_manager
from transport import pack_up, unpack_down

OLD = 2174928301
PASSWORD = 'TestOnlyPassword49'


class Sink:
    def __init__(self):
        self.frames = []
    def settimeout(self, value):
        pass
    def sendall(self, frame):
        self.frames.append(frame)


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=WORK.name)
        self.path = Path(self.folder.name) / 'test.db'
        shutil.copyfile(SEED, self.path)
        self.db = AccountDB(str(self.path))
        self.auth = get_service(self.db)
        self.setup_token = self.auth.bootstrap_token(OLD)
        self.old_save = self.snapshot(OLD)
        self.owner = self.auth.create('900001', PASSWORD, self.setup_token)
        self.player = self.auth.create('900002', PASSWORD)
        self.uid = self.player['uid']

    def tearDown(self):
        patch.stopall()
        self.db.close()
        self.folder.cleanup()

    def snapshot(self, uid):
        return {table: self.db.query(f'SELECT * FROM {table} WHERE uid=?', (uid,))
                for table in ('users', 'game_user', 'hero', 'equip', 'servant', 'currency', 'chapter', 'task', 'guide', 'claim_ledger')}

    def test_owner_binding_is_single_use_and_preserves_save(self):
        self.assertEqual(self.owner['uid'], OLD)
        self.assertEqual(self.owner['is_admin'], 1)
        self.assertEqual(self.snapshot(OLD), self.old_save)
        with self.assertRaises(AuthError):
            self.auth.create('900099', PASSWORD, self.setup_token)
        self.assertFalse(self.db.query('SELECT * FROM auth_bootstrap'))

    def test_unconfigured_server_does_not_give_first_visitor_admin(self):
        path = Path(self.folder.name) / 'unconfigured.db'
        shutil.copyfile(SEED, path)
        db = AccountDB(str(path))
        try:
            auth = get_service(db)
            with self.assertRaises(AuthError): auth.create('900001', PASSWORD)
            auth.bootstrap_token(OLD)
            with self.assertRaises(AuthError): auth.create('900001', PASSWORD, 'forged')
            self.assertFalse(db.query('SELECT * FROM auth_accounts'))
        finally:
            db.close()

    def test_fresh_state_and_separate_uids(self):
        other = self.auth.create('900003', PASSWORD)['uid']
        self.assertEqual(len({OLD, self.uid, other}), 3)
        for uid in (self.uid, other):
            role = self.db.get('users', uid)
            self.assertEqual((role['level'], role['exp'], role['plot_progress']), (1, 0, 0))
            heroes = self.db.query('SELECT * FROM hero WHERE uid=?', (uid,))
            self.assertEqual([(h['id'], h['level'], h['star'], h['weapon_exp']) for h in heroes], [(1084, 1, 100, 0)])
            for table in ('equip', 'servant', 'guide', 'chapter', 'claim_ledger'):
                self.assertFalse(self.db.query(f'SELECT 1 FROM {table} WHERE uid=?', (uid,)), table)
            self.assertEqual(self.db.get('users', uid)['hero_num'], 1)
        self.db.set_currency(self.uid, 1, 123)
        self.assertEqual(self.db.query('SELECT num FROM currency WHERE uid=? AND id=1', (other,))[0]['num'], 0)
        self.assertEqual(self.snapshot(OLD), self.old_save)

    def test_failed_initialization_rolls_back_every_table(self):
        before = self.db.query('SELECT uid FROM users ORDER BY uid')
        def broken(db, uid, *args):
            db.execute('INSERT INTO users(uid) VALUES (?)', (uid,))
            raise RuntimeError('injected failure')
        with patch('fresh_profile.initialize', side_effect=broken), self.assertRaises(RuntimeError):
            self.auth.create('900099', PASSWORD)
        self.assertEqual(self.db.query('SELECT uid FROM users ORDER BY uid'), before)
        self.assertFalse(self.db.query("SELECT 1 FROM auth_accounts WHERE username='900099'"))

    def test_password_storage_and_invalid_credentials(self):
        row = self.auth.account(self.uid)
        self.assertNotIn(PASSWORD, row['password_hash'])
        self.assertTrue(row['password_hash'].startswith('scrypt$'))
        self.assertNotEqual(row['password_hash'], self.auth.account(OLD)['password_hash'])
        for name, password in (('900002', 'wrong'), ('999999', PASSWORD), ('900002', None)):
            with self.assertRaises(AuthError): self.auth.login(name, password)

    def test_concurrent_same_name_creates_one_account(self):
        def signup(_):
            try:
                return self.auth.create('900009', PASSWORD)['uid']
            except AuthError:
                return None
            finally:
                self.db.close()
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(signup, range(3)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(len(self.db.query("SELECT 1 FROM auth_accounts WHERE username='900009'")), 1)

    def test_sessions_scope_expiry_uid_and_revocation(self):
        token = self.auth.issue(self.uid, 'sdk')
        self.assertEqual(self.auth.verify(token, {'sdk'})['uid'], self.uid)
        stored = self.db.query('SELECT token_hash FROM auth_sessions')[0]['token_hash']
        self.assertEqual(stored, token_hash(token))
        self.assertNotEqual(stored, token)
        for kinds, uid in (({'web'}, self.uid), ({'sdk'}, OLD)):
            with self.assertRaises(AuthError): self.auth.verify(token, kinds, uid)
        self.db.execute('UPDATE auth_sessions SET expires_at=0')
        with self.assertRaises(AuthError): self.auth.verify(token, {'sdk'})
        token = self.auth.issue(self.uid, 'web')
        version = self.auth.account(self.uid)['token_version']
        self.auth.change_password(self.uid, 'AnotherPassword49', PASSWORD)
        with self.assertRaises(AuthError): self.auth.verify(token, {'web'})
        self.assertFalse(self.auth.connection_valid(self.uid, version))
        self.assertEqual(self.auth.login('900002', 'AnotherPassword49')['uid'], self.uid)

    def test_disable_reenable_does_not_revive_tokens_or_allow_owner_disable(self):
        token = self.auth.issue(self.uid, 'sdk')
        self.auth.set_disabled(self.uid, True)
        with self.assertRaises(AuthError): self.auth.login('900002', PASSWORD)
        self.auth.set_disabled(self.uid, False)
        with self.assertRaises(AuthError): self.auth.verify(token, {'sdk'})
        self.assertEqual(self.auth.login('900002', PASSWORD)['uid'], self.uid)
        with self.assertRaises(AuthError): self.auth.set_disabled(OLD, True)

    def app_client(self):
        import server_net
        core = types.SimpleNamespace(db=self.db, log=lambda *a, **kw: None)
        app = server_net.build_https_app(core=core, host_ip='127.0.0.1')
        app.config['TESTING'] = True
        return app.test_client()

    def test_http_sdk_encoded_login_mix_and_gateway_chain(self):
        client = self.app_client()
        wire = hashlib.md5(PASSWORD.encode()).hexdigest()[:8].upper() + base64.b64encode(PASSWORD.encode()).decode()
        data = client.post('/sdk-api/pass/user/login', data={'username': '900002', 'password': wire, 'encode': 'true'}).get_json()
        self.assertEqual(data['errorCode'], '0', data)
        token = data['data']['token']
        refresh = client.post('/sdk-api/pass/user/loginByToken', data={'token': token}).get_json()
        self.assertEqual(refresh['data']['userId'], self.uid)
        mixed = client.post('/mix-sdk-api/pass/user/login', data={'channelLoginRespInfo': json.dumps(data['data'])}).get_json()
        self.assertEqual(mixed['data']['id'], self.uid)
        gw = auth_protocol.gateway(self.db, {'token': mixed['data']['token']}, '127.0.0.1', 8105)
        account, ack = auth_protocol.game(self.db, {'user_id': self.uid, 'gstoken': gw['gstoken']})
        self.assertEqual((account['uid'], ack['result']), (self.uid, 0))
        with self.assertRaises(AuthError):
            auth_protocol.game(self.db, {'user_id': OLD, 'gstoken': gw['gstoken']})
        for body in ({'token': 'old-fake-token'}, {'token': token + 'x'}):
            self.assertNotEqual(client.post('/sdk-api/pass/user/loginByToken', data=body).get_json()['errorCode'], '0')
        self.assertNotEqual(client.post('/sdk-api/pass/user/login', data={'username': '900002', 'password': '00000000' + wire[8:], 'encode': 'true'}).get_json()['errorCode'], '0')

    def test_http_gm_requires_admin_and_registration_cannot_elevate(self):
        client = self.app_client()
        for path in ('/api/gm/accounts', '/api/gm/gacha/rules', '/api/account/profile'):
            self.assertEqual(client.get(path).status_code, 401)
            token = self.auth.issue(self.uid, 'web')
            self.assertEqual(client.get(path, headers={'Authorization': 'Bearer ' + token}).status_code, 403)
        token = self.auth.issue(OLD, 'web')
        response = client.get('/api/gm/accounts', headers={'Authorization': 'Bearer ' + token})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('password_hash', response.get_data(as_text=True))
        result = client.post('/api/auth/register', json={'username': '900010', 'password': PASSWORD,
                            'uid': OLD, 'is_admin': 1, 'fresh_profile': 0}).get_json()
        self.assertEqual(result['code'], 0, result)
        self.assertEqual((result['data']['is_admin'], result['data']['fresh_profile']), (0, 1))
        self.assertNotEqual(result['data']['uid'], OLD)
        self.assertEqual(client.options('/api/gm/accounts').status_code, 204)
        self.assertEqual(client.get('/web/account.html').status_code, 200)

    def test_sms_and_unauthenticated_reset_are_not_bypasses(self):
        client = self.app_client()
        for path in ('loginBySms', 'registerBySms', 'resetPassword'):
            data = client.post('/sdk-api/pass/user/' + path, data={'phone': '900001', 'authCode': '1234', 'newPassword': 'attacker'}).get_json()
            self.assertNotEqual(data['errorCode'], '0', path)
        self.assertEqual(self.auth.login('900001', PASSWORD)['uid'], OLD)

    def test_registration_rate_limit_and_no_password_query_login(self):
        client = self.app_client()
        for _ in range(5): client.post('/api/auth/register', json={})
        self.assertEqual(client.post('/api/auth/register', json={}).status_code, 429)
        result = client.get('/sdk-api/pass/user/login?username=900001&password=anything').get_json()
        self.assertNotEqual(result['errorCode'], '0')

    def test_full_login_has_no_inherited_progress_or_assets(self):
        res_version_manager.set_current_version('321')
        sdk = self.auth.issue(self.uid, 'sdk')
        gw = auth_protocol.gateway(self.db, {'token': sdk}, '127.0.0.1', 8105)
        _, ack = auth_protocol.game(self.db, {'user_id': self.uid, 'gstoken': gw['gstoken']})
        sink = Sink()
        class SilentAI:
            @staticmethod
            def get_instance(**kw): return SilentAI()
            def trigger_login_greeting(self, *a, **kw): pass
        with patch.dict('sys.modules', {'ai_translator': types.SimpleNamespace(AITranslator=SilentAI)}), \
             patch('socket.socket.connect', side_effect=AssertionError('No external network in tests')):
            count, dynamic = login.send_login_flood(sink, self.db, self.uid, generator, frame_delay=0,
                                                   auth_context={10039: gw, 10043: ack})
        self.assertGreaterEqual(count, 190)
        self.assertEqual(count, dynamic)
        frames = [unpack_down(f) for f in sink.frames]
        decoded = {f['cmd']: codec.decode(f['payload'], 'sc_' + str(f['cmd'])) for f in frames}
        resident_groups = [[int(entry['id']) for entry in json.loads(row['payload_json'])['user_chapter_list']]
                           for row in self.db.query('SELECT payload_json FROM login_push WHERE cmd=24009 AND dynamic=0 ORDER BY seq')]
        sent_groups = [[int(entry['id']) for entry in codec.decode(frame['payload'], 'sc_24009').get('user_chapter_list', [])]
                       for frame in frames if frame['cmd'] == 24009]
        self.assertTrue(resident_groups)
        self.assertEqual(sent_groups[:len(resident_groups)], resident_groups)
        self.assertIn(10201, decoded)
        self.assertNotIn(89013, decoded)
        self.assertEqual(decoded[10039]['user_id'], self.uid)
        self.assertEqual(decoded[12009]['user_level'], 1)
        self.assertEqual(decoded[23009]['total_exp'], 0)
        self.assertFalse(decoded[12011].get('mod_guide_list'))
        self.assertFalse(decoded[12001].get('story_list'))
        self.assertEqual(decoded[28001]['newbie_phase'], 1)
        heroes = decoded[14009]['hero_info_list']
        self.assertEqual(len(heroes), 1)
        self.assertFalse(heroes[0].get('clear_mission_list'))
        self.assertFalse(decoded[13009].get('equip_list'))
        self.assertFalse(decoded[46011].get('weapon_list'))
        self.assertTrue(all(not r['clear_times'] for r in decoded[24009]['user_chapter_list']))
        self.assertEqual(decoded[34023]['monthly_card_num'], 0)
        self.assertFalse(decoded[18001].get('clear_difficulty_list'))
        self.assertFalse(decoded[18001]['terminal'].get('upgrade_id_list'))
        self.assertFalse(decoded[32009].get('all_sticker_list'))
        self.assertFalse(decoded[32009].get('info_list'))
        self.assertEqual(decoded[32009]['game_icon'], [{'id': 12001, 'lasted_time': 0, 'obtain_time': 0}])
        self.assertEqual(decoded[90051]['rank_score'], 0)
        self.assertEqual(decoded[11015]['sign_count'], 0)
        self.assertEqual(self.snapshot(OLD), self.old_save)

    def test_fresh_cosmetics_have_initial_app_icon_before_and_after_unlock(self):
        import gm_actions
        def profile(uid):
            return codec.decode(generator.gen_payload(32009, uid=uid, db=self.db), 'sc_32009')
        old_icons = profile(OLD)['game_icon']
        # Build 321 PlayerData.GetCurAppIcon indexes GetAllAppIconList()[1].id.
        # ItemCfg 12001 is the initial icon; 12002..12005 are extra unlocks.
        expected = [{'id': 12001, 'lasted_time': 0, 'obtain_time': 0}]
        self.assertEqual(profile(self.uid).get('game_icon'), expected)
        with patch('gm_actions.sync', return_value={'clients': 0}):
            result = gm_actions.unlock(self.db, self.uid, all_heroes=True)
        self.assertGreater(result['unlocked_count'], 0)
        self.assertEqual(profile(self.uid)['game_icon'], expected)
        self.assertEqual(profile(OLD)['game_icon'], old_icons)
        self.assertEqual(self.snapshot(OLD), self.old_save)

    def test_fresh_cosmetic_switches_roundtrip_without_changing_other_account(self):
        import core
        import operations
        engine = core.Core(db=self.db, generator=generator, codec_encode=codec.encode,
                           codec_decode=codec.decode, log=lambda *a, **kw: None)
        for cmd, key, value in ((32032, 'icon_id', 2110111),
                                (32034, 'iconframe_id', 2001), (32114, 'id', 8001)):
            response = engine.dispatch(core.CoreRequest(cmd, codec.encode(f'cs_{cmd}', {key: value}), uid=self.uid))
            ack = next(f for f in response.frames if f['sc'] == cmd + 1)
            self.assertEqual(codec.decode(ack['payload'], f'sc_{cmd + 1}')['result'], 0)
        # Regenerating the login payload covers persistence after reconnect.
        card = codec.decode(generator.gen_payload(32009, uid=self.uid, db=self.db), 'sc_32009')
        for current, owned, expected in (('icon', 'icon_list', 2110111),
                                         ('icon_frame', 'icon_frame_list', 2001),
                                         ('information_background_id', 'information_background_list', 8001)):
            self.assertEqual(card[current], expected)
            self.assertIn(expected, [entry['id'] for entry in card[owned]])
        self.assertEqual(card.get('game_icon'), [{'id': 12001, 'lasted_time': 0, 'obtain_time': 0}])
        self.assertEqual(self.snapshot(OLD), self.old_save)

    def test_chapter_and_task_progress_are_persistent_per_user(self):
        from stage_service import StageService
        service = StageService.get_instance(db=self.db)
        self.assertFalse(service.is_chapter_unlocked(self.uid, 10301)[0])
        service.pass_stage(self.uid, 1030001)
        self.assertTrue(self.db.query('SELECT 1 FROM chapter WHERE uid=? AND id=1030001 AND clear_times>0', (self.uid,)))
        other = self.auth.create('900003', PASSWORD)['uid']
        self.assertFalse(self.db.query('SELECT 1 FROM chapter WHERE uid=?', (other,)))
        for tid in range(32901, 32908):
            self.db.execute('UPDATE task SET progress=1 WHERE uid=? AND task_id=?', (self.uid, tid))
            self.db.claim_task(self.uid, tid)
        root = self.db.query('SELECT progress FROM task WHERE uid=? AND task_id=32900', (self.uid,))[0]
        self.assertEqual(root['progress'], 7)
        self.db.claim_task(self.uid, 32900)
        tasks = self.db.query('SELECT task_id FROM task WHERE uid=?', (self.uid,))
        self.assertEqual(fresh_profile.task_phases(self.db, self.uid, tasks)[3], 2)
        other_tasks = self.db.query('SELECT task_id FROM task WHERE uid=?', (other,))
        self.assertEqual(fresh_profile.task_phases(self.db, other, other_tasks)[3], 1)

    def test_actual_game_socket_rejects_uid_forgery_and_preauth_login(self):
        from server_net import ThreadedServer, GameHandler
        core = types.SimpleNamespace(log=lambda *a, **kw: None)
        server = ThreadedServer(('127.0.0.1', 0), GameHandler)
        # server_close must join handlers before tearDown deletes their SQLite files.
        server.daemon_threads = False
        server.block_on_close = True
        server.core, server.db, server.generator = core, self.db, generator
        server.codec_encode, server.codec_decode = codec.encode, codec.decode
        server.game_port = server.server_address[1]
        server.frame_delay = 0
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
        thread.start()
        try:
            token = self.auth.issue(self.uid, 'game')
            with socket.create_connection(server.server_address, timeout=3) as sock:
                data = {'channel_id': 9, 'server_id': 2, 'account': '900002', 'user_id': OLD,
                        'timestamp': int(time.time()), 'gstoken': token, 'type': 1}
                sock.sendall(pack_up(10042, codec.encode('cs_10042', data)))
                response = unpack_down(sock.recv(4096))
                self.assertNotEqual(codec.decode(response['payload'], 'sc_10043')['result'], 0)
                self.assertEqual(sock.recv(4096), b'')
            with socket.create_connection(server.server_address, timeout=3) as sock:
                sock.sendall(pack_up(10200, b''))
                self.assertEqual(sock.recv(4096), b'')
            with socket.create_connection(server.server_address, timeout=3) as sock:
                data['user_id'] = self.uid
                sock.sendall(pack_up(10042, codec.encode('cs_10042', data)))
                response = unpack_down(sock.recv(4096))
                self.assertEqual(codec.decode(response['payload'], 'sc_10043')['result'], 0)
        finally:
            server.shutdown(); server.server_close(); thread.join(3)

    def test_actual_gateway_returns_authenticated_uid_and_rejects_fake_token(self):
        from server_net import ThreadedServer, GatewayHandler
        server = ThreadedServer(('127.0.0.1', 0), GatewayHandler)
        server.core = types.SimpleNamespace(log=lambda *a, **kw: None)
        server.db, server.material = self.db, None
        server.codec_encode, server.codec_decode = codec.encode, codec.decode
        server.gw_port, server.game_port, server.host_ip = server.server_address[1], 8105, '127.0.0.1'
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
        thread.start()
        try:
            for uid in (OLD, self.uid, None):
                token = self.auth.issue(uid, 'mix') if uid else 'old-fake-token'
                data = {'channel_id': 9, 'account': 'anything', 'timestamp': int(time.time()), 'token': token, 'app_id': 'test'}
                with socket.create_connection(server.server_address, timeout=3) as sock:
                    sock.sendall(pack_up(10038, codec.encode('cs_10038', data)))
                    response = unpack_down(sock.recv(4096))
                    body = codec.decode(response['payload'], 'sc_10039')
                    if uid:
                        self.assertEqual(body['user_id'], uid)
                        self.assertEqual(self.auth.verify(body['gstoken'], {'game'})['uid'], uid)
                    else:
                        self.assertNotEqual(body['result'], 0)
        finally:
            server.shutdown(); server.server_close(); thread.join(3)

    def test_unimplemented_response_does_not_replay_template_progress(self):
        import middleware
        req = middleware.CSRequest(777, uid=self.uid)
        ctx = middleware.HandlerCtx(db=self.db)
        with patch('middleware._load_templates', return_value={'777': {'sc': 24009, 'template': {'user_chapter_list': [{'id': 1030001, 'clear_times': 99}]}}}):
            response = middleware._template_response(req, ctx)
        data = codec.decode(response.frames[0].payload, 'sc_24009')
        self.assertTrue(all(not r['clear_times'] for r in data['user_chapter_list']))

    def test_fresh_guild_boss_keeps_config_ids_but_no_recorded_progress(self):
        recorded = json.loads(self.db.query('SELECT payload_json FROM login_push WHERE cmd=33001 LIMIT 1')[0]['payload_json'])
        data = codec.decode(fresh_profile.static_payload(33001, recorded), 'sc_33001')
        self.assertEqual(data['info']['boss_tempalte_id'], recorded['info']['boss_tempalte_id'])
        self.assertGreater(data['info']['boss_tempalte_id'], 0)
        self.assertEqual(data['info']['difficulty'], 1)
        self.assertFalse(data['info'].get('cleared_difficulty_list'))
        for key in ('total_damage', 'collective_damage_award', 'personal_score'):
            self.assertEqual(data['info'][key], 0)
        self.assertEqual({r['stage_id'] for r in data['pre_info']}, {r['stage_id'] for r in recorded['pre_info']})
        self.assertTrue(all(r['score'] == 0 for r in data['pre_info']))
        with self.assertRaises(ValueError):
            fresh_profile.static_payload(33001, {})

    def test_resident_stage_initialization_reads_only_own_progress(self):
        recorded = {'user_chapter_list': [{'id': 5010101, 'clear_times': 99, 'star_list': [1, 1, 1]}]}
        data = codec.decode(fresh_profile.resident_stage_payload(self.db, self.uid, recorded), 'sc_24009')
        self.assertEqual(data['user_chapter_list'], [{'id': 5010101, 'clear_times': 0, 'star_list': [0, 0, 0]}])
        self.db.execute("INSERT INTO chapter (uid,id,clear_times,star_list) VALUES (?,5010101,2,'[1,1,0]')", (self.uid,))
        data = codec.decode(fresh_profile.resident_stage_payload(self.db, self.uid, recorded), 'sc_24009')
        self.assertEqual(data['user_chapter_list'], [{'id': 5010101, 'clear_times': 2, 'star_list': [1, 1, 0]}])

    def test_new_accounts_receive_public_activity_topology_without_player_progress(self):
        recorded = json.loads(self.db.query('SELECT payload_json FROM login_push WHERE cmd=11001 LIMIT 1')[0]['payload_json'])['activity_list']
        catalog = {r['activity_id']: r for r in recorded}
        definitions = fresh_profile.activity_definitions(self.db, [])
        self.assertEqual(len(definitions), len(catalog))
        generated = codec.decode(generator.gen_payload(11001, uid=self.uid, db=self.db, res_version='321'), 'sc_11001')
        available = {r['activity_id']: r for r in generated['activity_list']}
        for activity_id in (3, 180103, 3705601, 301701):
            self.assertIn(activity_id, available)
            self.assertEqual(available[activity_id]['template'], catalog[activity_id]['template'])
            self.assertEqual(available[activity_id].get('sub_activity_id_list', []), catalog[activity_id].get('sub_activity_id_list', []))
        self.assertFalse(self.db.query('SELECT 1 FROM activity WHERE uid=?', (self.uid,)))
        self.assertEqual(self.snapshot(OLD), self.old_save)

    def test_vehicle_ball_tasks_exist_at_zero_without_advancing_main_story(self):
        ids = {50101001, 50101002, 50101003, 50101004}
        data = codec.decode(generator.gen_payload(28001, uid=self.uid, db=self.db), 'sc_28001')
        tasks = {r['id']: r for r in data['assignment_list']}
        self.assertTrue(ids.issubset(tasks))
        for task_id in ids:
            self.assertEqual((tasks[task_id]['progress'], tasks[task_id]['complete_flag']), (0, 0))
        self.assertEqual((data['newbie_phase'], data['assignment_phase']), (1, 1))
        self.db.execute('UPDATE task SET progress=1,complete_flag=1,claimed_ts=123 WHERE uid=? AND task_id=50101001', (self.uid,))
        fresh_profile.ensure_resident_tasks(self.db, self.uid)
        row = self.db.query('SELECT progress,complete_flag,claimed_ts FROM task WHERE uid=? AND task_id=50101001', (self.uid,))[0]
        self.assertEqual(row, {'progress': 1, 'complete_flag': 1, 'claimed_ts': 123})
        self.assertEqual(self.snapshot(OLD), self.old_save)

    def test_guide_completion_ack_persists_and_is_idempotent(self):
        import core
        import operations
        engine = core.Core(db=self.db, generator=generator, codec_encode=codec.encode,
                           codec_decode=codec.decode, log=lambda *a, **kw: None)
        for cmd in (12012, 12012, 12112):
            req = core.CoreRequest(cmd, codec.encode(f'cs_{cmd}', {'mod_guide_id': 501}), uid=self.uid)
            response = engine.dispatch(req)
            ack = next(f for f in response.frames if f['sc'] == cmd + 1)
            self.assertEqual(codec.decode(ack['payload'], f'sc_{cmd + 1}')['result'], 0)
        rows = self.db.query('SELECT guide_id,kind FROM guide WHERE uid=?', (self.uid,))
        self.assertEqual(rows, [{'guide_id': 501, 'kind': 'both'}])
        for cmd in (12011, 12111):
            self.assertIn(501, codec.decode(generator.gen_payload(cmd, uid=self.uid, db=self.db), f'sc_{cmd}')['mod_guide_list'])
        self.assertEqual(self.snapshot(OLD), self.old_save)

    def test_guide_ack_encoding_failure_rolls_back_completion(self):
        import core
        import operations
        def fail_ack(name, data):
            if name == 'sc_12013': raise ValueError('injected codec failure')
            return codec.encode(name, data)
        engine = core.Core(db=self.db, generator=generator, codec_encode=fail_ack,
                           codec_decode=codec.decode, log=lambda *a, **kw: None)
        req = core.CoreRequest(12012, codec.encode('cs_12012', {'mod_guide_id': 502}), uid=self.uid)
        response = engine.dispatch(req)
        self.assertNotEqual(codec.decode(response.frames[-1]['payload'], 'sc_12013')['result'], 0)
        self.assertFalse(self.db.query('SELECT 1 FROM guide WHERE uid=? AND guide_id=502', (self.uid,)))

    def test_unknown_monster_cannot_poison_collection_updates(self):
        import illustrated_listener as illustrated
        self.assertFalse(illustrated.is_known_monster(101))
        self.assertIsNone(illustrated.trigger_monster_kill(self.db, self.uid, 101))
        self.assertFalse(self.db.query("SELECT 1 FROM illustrated WHERE uid=? AND kind='enemy'", (self.uid,)))
        self.assertTrue(illustrated.is_known_monster(2017))
        self.assertEqual(illustrated.trigger_monster_kill(self.db, self.uid, 2017)['times'], 1)
        self.assertEqual(illustrated.trigger_monster_kill(self.db, self.uid, 2017)['times'], 2)

    def test_legacy_invalid_monster_is_filtered_without_resetting_save(self):
        import illustrated_listener as illustrated
        self.db.execute("INSERT INTO illustrated(uid,kind,item_id,times,is_view) VALUES (?,'enemy',101,1,0)", (self.uid,))
        illustrated.trigger_monster_kill(self.db, self.uid, 2017, times=3)
        data = codec.decode(generator.gen_payload(52001, uid=self.uid, db=self.db), 'sc_52001')
        self.assertEqual(data['enemy_info'], [{'id': 2017, 'times': 3, 'is_view': 0}])
        self.assertTrue(self.db.query("SELECT 1 FROM illustrated WHERE uid=? AND kind='enemy' AND item_id=101", (self.uid,)))
        self.assertEqual(self.db.get('users', self.uid)['level'], 1)
        self.assertEqual(self.snapshot(OLD), self.old_save)

    def test_intro_settlement_and_relogin_do_not_create_unknown_monster(self):
        import core
        import middleware
        import operations
        import illustrated_listener as illustrated
        engine = core.Core(db=self.db, generator=generator, codec_encode=codec.encode,
                           codec_decode=codec.decode, log=lambda *a, **kw: None)
        session = {'dest': 1030001, 'heroes': [318], 'battle_times': 1, 'stage_type': 1,
                   'result_data': {'result': 1, 'info': {'battle_time': 17000}}}
        with patch('middleware.get_battle_session', return_value=session):
            response = engine.dispatch(core.CoreRequest(54032, codec.encode('cs_54032', {'battle_id': 987654321}), uid=self.uid))
        ack = next(f for f in response.frames if f['sc'] == 54033)
        self.assertEqual(codec.decode(ack['payload'], 'sc_54033')['result'], 0)
        for frame in response.frames:
            if frame['sc'] == 52009:
                data = codec.decode(frame['payload'], 'sc_52009')
                self.assertTrue(all(illustrated.is_known_monster(r['id']) for r in data.get('enemy_info', [])))
        self.assertFalse(self.db.query("SELECT 1 FROM illustrated WHERE uid=? AND kind='enemy' AND item_id=101", (self.uid,)))
        data = codec.decode(generator.gen_payload(52001, uid=self.uid, db=self.db), 'sc_52001')
        self.assertTrue(all(illustrated.is_known_monster(r['id']) for r in data.get('enemy_info', [])))
        self.assertEqual(self.db.query('SELECT clear_times FROM chapter WHERE uid=? AND id=1030001', (self.uid,))[0]['clear_times'], 1)
        self.assertEqual(self.snapshot(OLD), self.old_save)

    def test_current_seven_day_rewards_match_client_config(self):
        expected = [(38, 20), (20003, 20), (24, 80), (38, 20), (20003, 30), (24, 100), (32, 2000)]
        self.assertEqual([generator.seven_day_reward(4400101, day) for day in range(1, 8)], expected)

    def test_current_seven_day_sign_repeats_without_duplicate_rewards(self):
        import core
        import operations
        engine = core.Core(db=self.db, generator=generator, codec_encode=codec.encode,
                           codec_decode=codec.decode, log=lambda *a, **kw: None)
        request = core.CoreRequest(11010, codec.encode('cs_11010', {'activity_id': 4400101}), uid=self.uid)
        def balance(item_id):
            return sum(r['num'] or 0 for table in ('currency', 'material')
                       for r in self.db.query(f'SELECT num FROM {table} WHERE uid=? AND id=?', (self.uid, item_id)))
        items = (38, 20003, 24, 32)
        before = {item: balance(item) for item in items}
        initial = int(time.time())
        cycle_rewards = [(38, 20), (20003, 20), (24, 80), (38, 20), (20003, 30), (24, 100), (32, 2000)]
        expected_rewards = cycle_rewards * 2 + cycle_rewards[:1]
        with patch('event_bus.bus.emit'):
            for day, (item, amount) in enumerate(expected_rewards, 1):
                cycle_day = (day - 1) % 7 + 1
                with patch('time.time', return_value=initial + (day - 1) * 86400):
                    old_balance = balance(item)
                    for attempt in range(2):
                        response = engine.dispatch(request)
                        ack = next(f for f in response.frames if f['sc'] == 11011)
                        result = codec.decode(ack['payload'], 'sc_11011')
                        self.assertEqual(result['result'], 0, (day, attempt, result))
                        self.assertEqual(result['item_list'], [{'id': item, 'num': amount}])
                        sign = next(f for f in response.frames if f['sc'] == 11015)
                        self.assertEqual(codec.decode(sign['payload'], 'sc_11015')['sign_count'], cycle_day)
                    self.assertEqual(balance(item), old_balance + amount)
        self.assertEqual({item: balance(item) - before[item] for item in items}, {38: 100, 20003: 100, 24: 360, 32: 4000})
        self.assertEqual(self.db.query('SELECT sign_count FROM sign WHERE uid=? AND activity_id=4400101', (self.uid,))[0]['sign_count'], 1)
        self.assertEqual(self.snapshot(OLD), self.old_save)

    def test_legacy_login_sign_uses_saved_progress(self):
        self.db.execute('DELETE FROM sign WHERE uid=? AND activity_id=4400101', (OLD,))
        def login_sign():
            return codec.decode(generator.gen_payload(11015, uid=OLD, db=self.db, res_version='311'), 'sc_11015')
        self.assertEqual(login_sign()['sign_count'], 0)
        last = int(time.time()) - 86400
        self.db.execute('INSERT INTO sign(uid,activity_id,sign_count,last_sign_ts) VALUES(?,4400101,2,?)', (OLD, last))
        saved = self.db.query('SELECT * FROM sign WHERE uid=? ORDER BY activity_id', (OLD,))
        self.assertEqual(login_sign()['sign_count'], 2)
        self.assertEqual(login_sign()['last_sign_time'], last)
        self.assertEqual(self.db.query('SELECT * FROM sign WHERE uid=? ORDER BY activity_id', (OLD,)), saved)
        # A completed non-repeating event is not reopened by the weekly reset.
        self.db.execute('UPDATE sign SET sign_count=7,last_sign_ts=? WHERE uid=? AND activity_id=4300101', (last, OLD))
        limited = codec.decode(generator.gen_11015(4300101, uid=OLD, db=self.db), 'sc_11015')
        self.assertEqual(limited['sign_count'], 7)
        self.assertEqual(self.snapshot(OLD), self.old_save)

    def test_completed_seven_day_refreshes_at_five(self):
        import core
        import operations
        engine = core.Core(db=self.db, generator=generator, codec_encode=codec.encode,
                           codec_decode=codec.decode, log=lambda *a, **kw: None)
        last = int(time.mktime((2026, 10, 25, 8, 0, 0, 0, 0, -1)))
        reset = int(time.mktime((2026, 10, 26, 5, 0, 0, 0, 0, -1)))
        self.db.execute('INSERT INTO sign(uid,activity_id,sign_count,last_sign_ts) VALUES(?,4400101,7,?)', (self.uid, last))
        with patch('time.time', return_value=reset - 1):
            state = generator._sign_state(self.db, self.uid, 4400101)
            self.assertEqual(state['sign_count'], 7)
            self.assertTrue(state['signed_today'])
        with patch('time.time', return_value=reset), patch('event_bus.bus.emit'):
            packet = codec.decode(generator.gen_11015(4400101, uid=self.uid, db=self.db), 'sc_11015')
            self.assertEqual((packet['sign_count'], packet['last_sign_time']), (0, 0))
            self.assertEqual(self.db.query('SELECT sign_count FROM sign WHERE uid=? AND activity_id=4400101', (self.uid,))[0]['sign_count'], 7)
            old_balance = self.db.query('SELECT num FROM currency WHERE uid=? AND id=38', (self.uid,))[0]['num']
            request = core.CoreRequest(11010, codec.encode('cs_11010', {'activity_id': 4400101}), uid=self.uid)
            for attempt in range(2):
                response = engine.dispatch(request)
                ack = codec.decode(next(f['payload'] for f in response.frames if f['sc'] == 11011), 'sc_11011')
                self.assertEqual(ack['result'], 0, (attempt, ack))
                self.assertEqual(ack['item_list'], [{'id': 38, 'num': 20}])
                progress = codec.decode(next(f['payload'] for f in response.frames if f['sc'] == 11015), 'sc_11015')
                self.assertEqual(progress['sign_count'], 1)
            self.assertEqual(self.db.query('SELECT num FROM currency WHERE uid=? AND id=38', (self.uid,))[0]['num'], old_balance + 20)
        self.assertEqual(self.snapshot(OLD), self.old_save)


if __name__ == '__main__':
    unittest.main()
