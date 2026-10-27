"""First password SDK login creates one account without changing existing saves."""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

SEED = Path(os.environ['GM_TEST_DB_SEED']).resolve()
SANDBOX = tempfile.TemporaryDirectory(prefix='ag-game-registration-tests-')
BOOTSTRAP = Path(SANDBOX.name) / 'bootstrap.db'
shutil.copyfile(SEED, BOOTSTRAP)
os.environ['ACCOUNT_DB_PATH'] = str(BOOTSTRAP)

from flask import Flask
from account_db import AccountDB
from auth_api import install
from auth_service import AuthError, get_service, token_hash
import auth_protocol

OLD = 2174928301
PASSWORD = 'GameSignupTest49'
TABLES = ('users', 'game_user', 'hero', 'equip', 'servant', 'currency', 'material',
          'chapter', 'task', 'guide', 'claim_ledger', 'auth_accounts', 'auth_sessions')


class GameRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=SANDBOX.name)
        self.path = Path(self.folder.name) / 'player.db'
        shutil.copyfile(SEED, self.path)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('event_bus.bus.emit'))
        self.db = AccountDB(str(self.path))
        self.auth = get_service(self.db)
        self.auth.create('900001', PASSWORD, self.auth.bootstrap_token(OLD))
        self.known = self.auth.create('900002', PASSWORD)['uid']
        self.app = Flask('game_registration_test')
        install(self.app, self.db)
        self.client = self.app.test_client()

    def tearDown(self):
        self.stack.close()
        self.db.close()
        self.folder.cleanup()

    def login(self, name, password=PASSWORD, **extra):
        return self.client.post('/sdk-api/pass/user/login',
                                data={'username': name, 'password': password, **extra}).get_json()

    def snapshot(self, uid=None):
        where, args = ('', ()) if uid is None else (' WHERE uid=?', (uid,))
        return {table: sorted(self.db.query('SELECT * FROM ' + table + where, args),
                              key=lambda row: json.dumps(row, sort_keys=True)) for table in TABLES}

    def test_first_login_creates_fresh_account_and_reuses_it_after_reconnect(self):
        old_save = self.snapshot(OLD)
        response = self.login('900003')
        self.assertEqual(response['errorCode'], '0', response)
        account = response['data']
        uid = account['userId']
        self.assertNotIn(uid, (OLD, self.known))
        self.assertEqual(self.auth.verify(account['token'], {'sdk'})['uid'], uid)
        role = self.db.get('users', uid)
        self.assertEqual((role['level'], role['exp'], role['plot_progress']), (1, 0, 0))
        self.assertEqual([(h['id'], h['level']) for h in self.db.query('SELECT * FROM hero WHERE uid=?', (uid,))], [(1084, 1)])
        for table in ('equip', 'servant', 'chapter', 'guide'):
            self.assertEqual(self.db.query('SELECT * FROM ' + table + ' WHERE uid=?', (uid,)), [])
        again = self.login('900003')['data']
        self.assertEqual(again['userId'], uid)
        self.assertNotEqual(again['token'], account['token'])
        reopened = AccountDB(str(self.path))
        try:
            self.assertEqual(get_service(reopened).login_or_register_sdk('900003', PASSWORD)['uid'], uid)
        finally:
            reopened.close()
        self.assertEqual(self.snapshot(OLD), old_save)
        self.assertTrue(self.client.get('/api/auth/status').get_json()['data']['game_auto_registration'])

    def test_encoded_first_login_works_through_mix_gateway_and_game_auth(self):
        wire = hashlib.md5(PASSWORD.encode()).hexdigest()[:8].upper() + base64.b64encode(PASSWORD.encode()).decode()
        response = self.login('900004', wire, encode='TRUE')
        self.assertEqual(response['errorCode'], '0', response)
        account = response['data']
        mixed = self.client.post('/mix-sdk-api/pass/user/login',
            data={'channelLoginRespInfo': json.dumps(account)}).get_json()
        self.assertEqual(mixed['data']['id'], account['userId'])
        gw = auth_protocol.gateway(self.db, {'token': mixed['data']['token']}, '127.0.0.1', 8105)
        self.assertEqual(gw['is_new_player'], 1)
        owner, ack = auth_protocol.game(self.db, {'user_id': account['userId'], 'gstoken': gw['gstoken']})
        self.assertEqual(owner['uid'], account['userId'])
        self.assertEqual(ack['result'], 0)

    def test_existing_account_uses_original_password_and_save(self):
        password_hash = self.auth.account(OLD)['password_hash']
        self.assertEqual(self.login('900001')['data']['userId'], OLD)
        before = self.snapshot()
        response = self.login('900001', 'WrongPassword49')
        self.assertEqual(response['errorCode'], '401')
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.auth.account(OLD)['password_hash'], password_hash)

    def test_disabled_account_is_rejected_and_not_recreated(self):
        self.auth.set_disabled(self.known, True)
        before = self.snapshot()
        response = self.login('900002')
        self.assertEqual(response['errorCode'], '401')
        self.assertEqual(self.snapshot(), before)

    def test_existing_numeric_json_username_keeps_legacy_login_compatibility(self):
        response = self.client.post('/sdk-api/pass/user/login',
                                    json={'username': 900001, 'password': PASSWORD}).get_json()
        self.assertEqual(response['errorCode'], '0')
        self.assertEqual(response['data']['userId'], OLD)
        before = self.snapshot()
        response = self.client.post('/sdk-api/pass/user/login',
                                    json={'username': 900015, 'password': PASSWORD}).get_json()
        self.assertNotEqual(response['errorCode'], '0')
        self.assertEqual(self.snapshot(), before)

    def test_invalid_first_login_and_bad_encoding_do_not_create_accounts(self):
        before = self.snapshot()
        for name in ('abc', '12345', '1' * 17, None):
            self.assertNotEqual(self.login(name)['errorCode'], '0')
        for password in ('', 'short', 'x' * 65, None):
            self.assertNotEqual(self.login('900005', password)['errorCode'], '0')
        self.assertNotEqual(self.login('900005', '00000000' + base64.b64encode(PASSWORD.encode()).decode(), encode='true')['errorCode'], '0')
        self.assertEqual(self.snapshot(), before)

    def test_profile_initialization_failure_rolls_back_entire_registration(self):
        before = self.snapshot()
        def fail(db, uid, *args):
            db.execute('INSERT INTO users(uid) VALUES (?)', (uid,))
            raise RuntimeError('profile initialization failed')
        with patch('fresh_profile.initialize', side_effect=fail), self.assertRaises(RuntimeError):
            self.auth.login_or_register_sdk('900006', PASSWORD)
        self.assertEqual(self.snapshot(), before)

    def test_first_session_failure_rolls_back_account_and_initial_profile(self):
        before = self.snapshot()
        with patch.object(self.auth, 'issue', side_effect=RuntimeError('session creation failed')):
            with self.assertRaises(RuntimeError):
                self.auth.login_or_register_sdk('900007', PASSWORD)
        self.assertEqual(self.snapshot(), before)

    def test_concurrent_same_credentials_create_one_uid_and_valid_sessions(self):
        before = self.db.query('SELECT uid FROM users')
        def attempt(_):
            try:
                return self.auth.login_or_register_sdk('900008', PASSWORD)
            finally:
                self.db.close()
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(attempt, range(3)))
        self.assertEqual(len({r['uid'] for r in results}), 1)
        self.assertEqual(len(self.db.query('SELECT uid FROM users')), len(before) + 1)
        for result in results:
            self.assertEqual(self.auth.verify(result['token'], {'sdk'})['uid'], result['uid'])

    def test_concurrent_different_passwords_cannot_overwrite_winning_account(self):
        def attempt(password):
            try:
                result = self.auth.login_or_register_sdk('900009', password)
                return result['uid'], password, 0
            except AuthError as error:
                return None, password, error.status
            finally:
                self.db.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, (PASSWORD, 'OtherSignupPassword49')))
        winner = [r for r in results if r[0] is not None]
        loser = [r for r in results if r[0] is None]
        self.assertEqual((len(winner), len(loser)), (1, 1))
        self.assertEqual(loser[0][2], 401)
        self.assertEqual(self.auth.login('900009', winner[0][1])['uid'], winner[0][0])
        with self.assertRaises(AuthError):
            self.auth.login('900009', loser[0][1])

    def test_web_token_and_mix_login_do_not_register_unknown_accounts(self):
        before = self.snapshot()
        response = self.client.post('/api/auth/login', json={'username': '900010', 'password': PASSWORD})
        self.assertEqual(response.status_code, 401)
        for path, data in (
            ('/sdk-api/pass/user/loginByToken', {'username': '900010', 'password': PASSWORD}),
            ('/mix-sdk-api/pass/user/login', {'username': '900010', 'password': PASSWORD, 'channelLoginRespInfo': '{}'}),
        ):
            self.assertNotEqual(self.client.post(path, data=data).get_json()['errorCode'], '0')
        self.assertEqual(self.snapshot(), before)

    def test_registration_quota_shared_with_web_and_does_not_block_existing_login(self):
        for name in ('901001', '901002'):
            self.assertEqual(self.client.post('/api/auth/register', json={'username': name, 'password': PASSWORD}).get_json()['code'], 0)
        for name in ('901003', '901004', '901005'):
            self.assertEqual(self.login(name)['errorCode'], '0')
        self.assertEqual(self.login('901006')['errorCode'], '429')
        self.assertFalse(self.db.query("SELECT 1 FROM auth_accounts WHERE username='901006'"))
        self.assertEqual(self.login('900001')['data']['userId'], OLD)

    def test_unconfigured_server_does_not_register_first_visitor_as_admin(self):
        path = Path(self.folder.name) / 'not-ready.db'
        shutil.copyfile(SEED, path)
        db = AccountDB(str(path))
        try:
            app = Flask('unconfigured_game_registration_test')
            install(app, db)
            client = app.test_client()
            before = db.query('SELECT uid FROM users')
            response = client.post('/sdk-api/pass/user/login', data={'username': '900011', 'password': PASSWORD}).get_json()
            self.assertEqual(response['errorCode'], '503')
            self.assertFalse(client.get('/api/auth/status').get_json()['data']['game_auto_registration'])
            self.assertEqual(db.query('SELECT uid FROM users'), before)
            self.assertFalse(db.query('SELECT * FROM auth_accounts'))
        finally:
            db.close()

    def test_first_login_cannot_choose_admin_uid_or_inherit_old_profile(self):
        old_save = self.snapshot(OLD)
        response = self.login('900012', uid=str(OLD), is_admin='1', fresh_profile='0', tutorial_mode='skip')
        self.assertEqual(response['errorCode'], '0')
        account = self.auth.verify(response['data']['token'], {'sdk'})
        self.assertNotEqual(account['uid'], OLD)
        self.assertEqual((account['is_admin'], account['fresh_profile'], account['tutorial_mode']), (0, 1, 'full'))
        self.assertEqual(self.snapshot(OLD), old_save)
        self.assertEqual(self.client.get('/api/gm/accounts', headers={'Authorization': 'Bearer ' + response['data']['token']}).status_code, 401)

    def test_registration_callback_only_runs_for_new_names_and_failure_leaves_no_profile(self):
        callback = Mock(side_effect=AuthError('registration quota exhausted', 429))
        self.assertEqual(self.auth.login_or_register_sdk('900001', PASSWORD, callback)['uid'], OLD)
        callback.assert_not_called()
        before = self.snapshot()
        with self.assertRaises(AuthError) as caught:
            self.auth.login_or_register_sdk('900013', PASSWORD, callback)
        self.assertEqual(caught.exception.status, 429)
        callback.assert_called_once_with()
        self.assertEqual(self.snapshot(), before)

    def test_json_and_legacy_alias_login_use_scoped_hashed_credentials(self):
        response = self.client.post('/pass/user/login', json={'username': '900014', 'password': PASSWORD})
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        account = response.get_json()['data']
        row = self.auth.account(account['userId'])
        self.assertTrue(row['password_hash'].startswith('scrypt$'))
        self.assertNotIn(PASSWORD, row['password_hash'])
        session = self.db.query('SELECT * FROM auth_sessions WHERE token_hash=?', (token_hash(account['token']),))[0]
        self.assertEqual(session['kind'], 'sdk')
        self.assertNotEqual(session['token_hash'], account['token'])
        self.assertEqual(self.auth.login('900014', PASSWORD)['uid'], account['userId'])


if __name__ == '__main__':
    unittest.main()
