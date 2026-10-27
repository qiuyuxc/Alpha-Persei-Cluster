"""Period boundaries and mail/rotation contracts on isolated player snapshots."""
from concurrent.futures import ThreadPoolExecutor
import datetime
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

SEED = Path(os.environ['GM_TEST_DB_SEED']).resolve()
SANDBOX = tempfile.TemporaryDirectory(prefix='ag-cycle-tests-')
BOOTSTRAP = Path(SANDBOX.name) / 'bootstrap.db'
shutil.copyfile(SEED, BOOTSTRAP)
os.environ['ACCOUNT_DB_PATH'] = str(BOOTSTRAP)

from flask import Flask
from account_db import AccountDB
from codec import encode, decode
import core
import game_cycles
import generator
import gm_actions
import gm_api
from gm_reader import GMReader
from draw_service import DrawService
from lazy_timer import get_daily_5am_ts, get_weekly_mon_5am_ts
import mail_automation
from mail_service import MailService
from middleware import DownFrame
import operations
import runtime_scheduler
import timer_listeners

UID = 2174928301
OTHER = 264084870
DAY = 86400
NOW = int(datetime.datetime(2026, 10, 26, 12).timestamp())


class CycleMailTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=SANDBOX.name)
        self.path = Path(self.folder.name) / 'player.db'
        shutil.copyfile(SEED, self.path)
        patch('time.time', return_value=NOW).start()
        patch('res_version_manager.get_current_version', return_value='311').start()
        self.db = AccountDB(str(self.path))
        DrawService._instance = None
        MailService._instance = None
        self.draw = DrawService.get_instance(self.db)
        game_cycles.get_settings(self.db, NOW)
        mail_automation.initialize(self.db, NOW)
        self.db.execute('UPDATE gm_level_mail_rule SET enabled=0')
        patch('gm_api.get_db', return_value=self.db).start()
        patch('gm_api.get_reader', side_effect=lambda: GMReader(str(self.path))).start()
        patch('gm_api._append_gm_mail_history').start()
        patch('gm_actions.sync', return_value={'clients': 0, 'requires_relogin': True}).start()
        patch('server_net.push_mail_notice', return_value=False).start()
        self.app = Flask(__name__)
        self.app.register_blueprint(gm_api.gm_bp, url_prefix='/api/gm')
        self.client = self.app.test_client()

    def tearDown(self):
        patch.stopall()
        self.db.close()
        self.folder.cleanup()

    def scalar(self, sql, args=()):
        return next(iter(self.db.query(sql, args)[0].values()))

    def post(self, path, body):
        return self.client.post('/api/gm/' + path, json=body).get_json()

    def task(self, tid, owner=UID):
        return self.db.query('SELECT * FROM task WHERE uid=? AND task_id=?', (owner, tid))[0]

    def claimed_task(self, tid, task_type, owner=UID, timestamp=NOW):
        self.db.execute('''INSERT INTO task(uid,task_id,name,task_type,progress,complete_flag,expired_ts,claimed_ts)
            VALUES(?,?,'test',?,7,1,0,?) ON CONFLICT(uid,task_id) DO UPDATE SET progress=7,complete_flag=1,claimed_ts=excluded.claimed_ts''', (owner, tid, task_type, timestamp))
        self.db.mark_claimed(owner, 'task', tid, timestamp)

    def rule(self, **changes):
        return mail_automation.save_rule(self.db, {'name': '测试成长礼包', 'title': '测试成长礼包',
            'level_min': 11, 'level_max': 100, 'attachments': [{'id': 38, 'number': 20}], **changes}, NOW)

    def rotate(self, **changes):
        settings = {'enabled': True, 'duration_days': 7, 'start_ts': get_daily_5am_ts(NOW),
            'fixed_pool_ids': [10001, 10002, 5030601, 5030301],
            'steps': [{'name': '第一组', 'pool_ids': [5020301, 5020601]},
                      {'name': '第二组', 'pool_ids': [4050301, 4050601]}], **changes}
        game_cycles.save_settings(self.db, {'rotation': settings}, NOW)
        return settings

    def protected(self):
        return {table: self.db.query('SELECT * FROM ' + table + ' ORDER BY uid', ())
                for table in ('users', 'hero', 'equip', 'servant', 'material', 'chapter')}

    def test_battlepass_adoption_preserves_rewards_payment_and_experience(self):
        saved = self.db.query('SELECT pay_level,weekly_gain_exp,receive_info FROM battlepass WHERE uid=?', (UID,))[0]
        xp = self.db.query('SELECT * FROM currency WHERE uid=? AND id=14', (UID,))
        protected = self.protected()
        current = game_cycles.ensure_battlepass(self.db, UID, NOW)
        self.assertEqual({key: current[key] for key in saved}, saved)
        self.assertEqual(current['end_timestamp'] - current['start_timestamp'], 42 * DAY)
        self.assertEqual(self.db.query('SELECT * FROM currency WHERE uid=? AND id=14', (UID,)), xp)
        self.assertEqual(self.protected(), protected)
        packet = decode(generator.gen_payload(34031, uid=UID, db=self.db), 'sc_34031')
        self.assertEqual(packet['end_timestamp'], current['end_timestamp'])
        self.assertLessEqual(packet['next_refresh_timestamp'], packet['end_timestamp'])

    def test_battlepass_boundary_resets_once_and_keeps_other_progress(self):
        before = game_cycles.ensure_battlepass(self.db, UID, NOW)
        protected = self.protected()
        self.db.set_currency(UID, 14, 70000)
        next_cycle = game_cycles.ensure_battlepass(self.db, UID, before['end_timestamp'])
        self.assertEqual((next_cycle['pay_level'], next_cycle['weekly_gain_exp'], next_cycle['receive_info']), (0, 0, '[]'))
        self.assertEqual(self.scalar('SELECT num FROM currency WHERE uid=? AND id=14', (UID,)), 0)
        self.assertEqual(self.protected(), protected)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM battlepass_cycle_history WHERE uid=?', (UID,)), 1)
        self.db.set_currency(UID, 14, 1234)
        game_cycles.ensure_battlepass(self.db, UID, before['end_timestamp'] + 1)
        self.assertEqual(self.scalar('SELECT num FROM currency WHERE uid=? AND id=14', (UID,)), 1234)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM battlepass_cycle_history WHERE uid=?', (UID,)), 1)

    def test_offline_skip_and_clock_rollback_do_not_replay_cycles(self):
        first = game_cycles.ensure_battlepass(self.db, UID, NOW)
        later = first['start_timestamp'] + 3 * 42 * DAY
        current = game_cycles.ensure_battlepass(self.db, UID, later)
        self.assertEqual(current['start_timestamp'], later)
        self.db.set_currency(UID, 14, 3333)
        rollback = game_cycles.ensure_battlepass(self.db, UID, NOW)
        self.assertEqual(rollback['start_timestamp'], current['start_timestamp'])
        self.assertEqual(rollback['next_refresh_timestamp'], current['next_refresh_timestamp'])
        self.assertEqual(self.scalar('SELECT num FROM currency WHERE uid=? AND id=14', (UID,)), 3333)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM battlepass_cycle_history WHERE uid=?', (UID,)), 1)

    def test_battlepass_duration_edit_takes_effect_next_cycle_and_survives_restart(self):
        first = game_cycles.ensure_battlepass(self.db, UID, NOW)
        game_cycles.save_settings(self.db, {'battlepass': {'duration_days': 7}}, NOW + DAY)
        self.assertEqual(game_cycles.ensure_battlepass(self.db, UID, NOW + DAY)['end_timestamp'], first['end_timestamp'])
        reopened = AccountDB(str(self.path))
        try:
            next_cycle = game_cycles.ensure_battlepass(reopened, UID, first['end_timestamp'])
            self.assertEqual(next_cycle['end_timestamp'] - next_cycle['start_timestamp'], 7 * DAY)
            self.assertIsNone(game_cycles.get_settings(reopened, first['end_timestamp'])['battlepass']['next_duration_days'])
        finally:
            reopened.close()

    def test_polyhedron_window_follows_configured_week_from_monday_five_am(self):
        import res_version_manager
        start, stop = game_cycles.polyhedron_window(self.db, NOW)
        self.assertEqual(start, get_weekly_mon_5am_ts(NOW))
        self.assertEqual(stop - start, 7 * DAY)
        self.assertEqual(stop, get_weekly_mon_5am_ts(NOW) + 7 * DAY)
        self.assertLess(stop, res_version_manager.FAR_FUTURE_TIMESTAMP)
        advanced = game_cycles.polyhedron_window(self.db, stop)
        self.assertEqual(advanced, (stop, stop + 7 * DAY))

    def test_polyhedron_packets_use_real_window_and_other_activities_keep_far_future(self):
        import res_version_manager
        start, stop = game_cycles.polyhedron_window(self.db, NOW)
        payload = decode(generator.gen_payload(11001, uid=UID, db=self.db), 'sc_11001')
        rows = {row['activity_id']: row for row in payload['activity_list']}
        activity_id = int(res_version_manager.get_version_config('311')['polyhedron_activity_id'])
        self.assertEqual((rows[activity_id]['start_time'], rows[activity_id]['stop_time']), (start, stop))
        self.assertTrue(all((row['start_time'], row['stop_time']) == (start, stop)
                            for row in payload['activity_list'] if row['template'] == 73))
        shelter = rows[int(res_version_manager.get_version_config('311')['entry_activity_id'])]
        self.assertEqual(shelter['stop_time'], res_version_manager.FAR_FUTURE_TIMESTAMP)

    def test_polyhedron_duration_edit_takes_effect_next_cycle_and_survives_restart(self):
        start, stop = game_cycles.polyhedron_window(self.db, NOW)
        game_cycles.save_settings(self.db, {'polyhedron': {'duration_days': 3}}, NOW + DAY)
        self.assertEqual(game_cycles.polyhedron_window(self.db, NOW + DAY), (start, stop))
        reopened = AccountDB(str(self.path))
        try:
            self.assertEqual(game_cycles.polyhedron_window(reopened, stop), (stop, stop + 3 * DAY))
            self.assertIsNone(game_cycles.get_settings(reopened, stop)['polyhedron']['next_duration_days'])
            self.assertEqual(game_cycles.get_settings(reopened, stop)['battlepass']['duration_days'], 42)
        finally:
            reopened.close()

    def test_polyhedron_validation_rejects_bad_days_and_legacy_config_is_backfilled(self):
        import json
        before = game_cycles.get_settings(self.db, NOW)
        for invalid in ({'duration_days': 0}, {'duration_days': 1.5}, {'duration_days': 366},
                        {'duration_days': 7, 'anchor_ts': NOW}):
            with self.assertRaises(ValueError):
                game_cycles.save_settings(self.db, {'polyhedron': invalid}, NOW)
            self.assertEqual(game_cycles.get_settings(self.db, NOW), before)
        legacy = {key: value for key, value in before.items() if key != 'polyhedron'}
        self.db.execute('UPDATE server_global_config SET value=? WHERE key=?',
                        (json.dumps(legacy, ensure_ascii=False), game_cycles.KEY))
        restored = game_cycles.get_settings(self.db, NOW)
        self.assertEqual(restored['polyhedron']['duration_days'], 7)
        self.assertEqual(restored['polyhedron']['anchor_ts'], get_weekly_mon_5am_ts(NOW))
        stored = json.loads(self.db.query('SELECT value FROM server_global_config WHERE key=?', (game_cycles.KEY,))[0]['value'])
        self.assertIn('polyhedron', stored)

    def test_multi_dim_down_frame_carries_the_configured_window(self):
        import operations
        start, stop = game_cycles.polyhedron_window(self.db, NOW)
        frames = operations._build_18003_frame(gm_actions.context(self.db), {}, UID)
        activity = decode(next(frame for frame in frames if frame.cmd == 11003).payload, 'sc_11003')['activity']
        self.assertEqual((activity['start_time'], activity['stop_time']), (start, stop))
        self.assertEqual(activity['template'], 73)
        self.assertLess(stop, 2440962000)

    def test_polyhedron_api_reports_window_and_saves_next_cycle_days(self):
        status = self.client.get('/api/gm/automation/status').get_json()
        self.assertEqual(status['code'], 0, status)
        window = status['data']['polyhedron']
        self.assertEqual(window['end_ts'] - window['start_ts'], 7 * DAY)
        saved = self.client.post('/api/gm/automation/schedule', json={'polyhedron': {'duration_days': 10}}).get_json()
        self.assertEqual(saved['code'], 0, saved)
        self.assertEqual(saved['data']['settings']['polyhedron']['next_duration_days'], 10)
        self.assertEqual(saved['data']['polyhedron'], window)
        rejected = self.client.post('/api/gm/automation/schedule', json={'polyhedron': {'duration_days': 0}}).get_json()
        self.assertNotEqual(rejected['code'], 0)
        self.assertEqual(self.client.get('/api/gm/automation/status').get_json()['data']['settings']['polyhedron'],
                         saved['data']['settings']['polyhedron'])

    def test_policy_cycle_follows_the_configured_polyhedron_window(self):
        from polyhedron_service import PolyhedronRunManager
        start, stop = game_cycles.polyhedron_window(self.db, NOW)
        self.assertEqual(PolyhedronRunManager.get_policy_cycle_id(self.db, NOW), start)
        self.assertEqual(PolyhedronRunManager.get_policy_cycle_id(self.db, stop), stop)
        game_cycles.save_settings(self.db, {'polyhedron': {'duration_days': 3}}, NOW + DAY)
        self.assertEqual(PolyhedronRunManager.get_policy_cycle_id(self.db, stop), stop)

    def test_first_policy_cycle_contact_anchors_legacy_accounts_without_resetting_progress(self):
        from polyhedron_service import PolyhedronRunManager
        start, _ = game_cycles.polyhedron_window(self.db, NOW)
        self.assertEqual(self.db.get('polyhedron_meta', UID, 'AND activity_id=?', (4307301,))['cycle_id'], 0)
        self.assertFalse(PolyhedronRunManager.check_and_refresh_policy_cycle(UID, self.db))
        self.assertEqual(self.db.get('polyhedron_meta', UID, 'AND activity_id=?', (4307301,))['cycle_id'], start)
        self.assertEqual(self.scalar('SELECT num FROM currency WHERE uid=? AND id=45', (UID,)), 2900)
        self.assertGreater(self.scalar('SELECT COUNT(*) FROM polyhedron_policy_claimed WHERE uid=? AND activity_id=4307301', (UID,)), 0)
        self.assertFalse(PolyhedronRunManager.check_and_refresh_policy_cycle(UID, self.db))

    def test_policy_cycle_resets_experience_and_claims_once_at_the_window_boundary(self):
        from polyhedron_service import PolyhedronRunManager
        start, stop = game_cycles.polyhedron_window(self.db, NOW)
        self.assertFalse(PolyhedronRunManager.check_and_refresh_policy_cycle(UID, self.db))
        self.db.set_currency(UID, 46, 117046)
        meta_before = self.db.get('polyhedron_meta', UID, 'AND activity_id=?', (4307301,))
        with patch('time.time', return_value=stop):
            self.assertTrue(PolyhedronRunManager.check_and_refresh_policy_cycle(UID, self.db))
            self.assertFalse(PolyhedronRunManager.check_and_refresh_policy_cycle(UID, self.db))
        self.assertEqual(self.scalar('SELECT num FROM currency WHERE uid=? AND id=45', (UID,)), 0)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM polyhedron_policy_claimed WHERE uid=? AND activity_id=4307301', (UID,)), 0)
        self.assertEqual(self.scalar('SELECT num FROM currency WHERE uid=? AND id=46', (UID,)), 117046)
        meta = self.db.get('polyhedron_meta', UID, 'AND activity_id=?', (4307301,))
        self.assertEqual(meta['cycle_id'], stop)
        self.assertEqual((meta['game_state'], meta['reset_times']), (meta_before['game_state'], meta_before['reset_times']))

    def test_policy_cycle_refresh_does_not_touch_other_players_or_activity_rows(self):
        from polyhedron_service import PolyhedronRunManager
        self.assertFalse(PolyhedronRunManager.check_and_refresh_policy_cycle(UID, self.db))
        _, stop = game_cycles.polyhedron_window(self.db, NOW)
        self.db.set_currency(OTHER, 45, 4321)
        self.db.execute('INSERT OR REPLACE INTO polyhedron_meta(uid,activity_id,cycle_id,update_ts) VALUES(?,?,?,?)',
                        (OTHER, 4307301, stop - DAY, NOW))
        self.db.execute('INSERT OR REPLACE INTO polyhedron_policy_claimed(uid,activity_id,level,update_ts) VALUES(?,?,?,?)',
                        (UID, 4407301, 1, NOW))
        with patch('time.time', return_value=stop):
            self.assertTrue(PolyhedronRunManager.check_and_refresh_policy_cycle(UID, self.db))
        self.assertEqual(self.scalar('SELECT num FROM currency WHERE uid=? AND id=45', (OTHER,)), 4321)
        self.assertEqual(self.db.get('polyhedron_meta', OTHER, 'AND activity_id=?', (4307301,))['cycle_id'], stop - DAY)
        self.assertEqual([r['level'] for r in self.db.query(
            'SELECT level FROM polyhedron_policy_claimed WHERE uid=? AND activity_id=4407301', (UID,))], [1])
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM polyhedron_policy_claimed WHERE uid=? AND activity_id=4307301', (UID,)), 0)

    def test_battlepass_reset_failure_rolls_back_history_and_assets(self):
        first = game_cycles.ensure_battlepass(self.db, UID, NOW)
        saved = self.db.query('SELECT * FROM battlepass WHERE uid=?', (UID,))
        xp = self.db.query('SELECT * FROM currency WHERE uid=? AND id=14', (UID,))
        with patch('game_cycles._reset_tasks', side_effect=ValueError('reset failed')):
            with self.assertRaises(ValueError):
                game_cycles.ensure_battlepass(self.db, UID, first['end_timestamp'])
        self.assertEqual(self.db.query('SELECT * FROM battlepass WHERE uid=?', (UID,)), saved)
        self.assertEqual(self.db.query('SELECT * FROM currency WHERE uid=? AND id=14', (UID,)), xp)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM battlepass_cycle_history WHERE uid=?', (UID,)), 0)

    def test_daily_boundary_clears_recurring_claims_and_keeps_permanent_and_other_uid(self):
        game_cycles.sync_tasks(self.db, UID, NOW)
        for tid, kind, owner in ((6002, 6, UID), (81004, 8, UID), (999001, 0, UID), (6002, 6, OTHER)):
            self.claimed_task(tid, kind, owner)
        saved_weekly = self.task(81004)
        permanent = self.task(999001)
        other = self.task(6002, OTHER)
        self.db.set_currency(UID, 22, 100)
        next_day = get_daily_5am_ts(NOW) + DAY
        result = game_cycles.sync_tasks(self.db, UID, next_day)
        self.assertTrue(result['daily_reset'])
        self.assertFalse(result['weekly_reset'])
        self.assertEqual((self.task(6002)['progress'], self.task(6002)['claimed_ts']), (0, 0))
        self.assertFalse(self.db.is_claimed(UID, 'task', 6002))
        self.assertEqual(self.task(81004), saved_weekly)
        self.assertEqual(self.task(999001), permanent)
        self.assertEqual(self.task(6002, OTHER), other)
        self.assertEqual(self.scalar('SELECT num FROM currency WHERE uid=? AND id=22', (UID,)), 0)
        self.claimed_task(6002, 6, timestamp=next_day + 1)
        game_cycles.sync_tasks(self.db, UID, next_day + 2)
        self.assertEqual(self.task(6002)['claimed_ts'], next_day + 1)

    def test_weekly_boundary_resets_weekly_claims_cap_and_points(self):
        game_cycles.sync_tasks(self.db, UID, NOW)
        self.claimed_task(81004, 8)
        self.claimed_task(999001, 0)
        self.db.execute('UPDATE battlepass SET weekly_gain_exp=5000 WHERE uid=?', (UID,))
        self.db.set_currency(UID, 35, 100)
        self.db.set_currency(UID, 14, 4000)
        next_week = get_weekly_mon_5am_ts(NOW) + 7 * DAY
        result = game_cycles.sync_tasks(self.db, UID, next_week)
        self.assertTrue(result['weekly_reset'])
        self.assertEqual(self.task(81004)['claimed_ts'], 0)
        self.assertFalse(self.db.is_claimed(UID, 'task', 81004))
        self.assertEqual(result['battlepass']['weekly_gain_exp'], 0)
        self.assertEqual(self.scalar('SELECT num FROM currency WHERE uid=? AND id=35', (UID,)), 0)
        self.assertEqual(self.scalar('SELECT num FROM currency WHERE uid=? AND id=14', (UID,)), 4000)
        self.assertTrue(self.db.is_claimed(UID, 'task', 999001))

    def test_task_watermarks_survive_restart_and_clock_rollback(self):
        game_cycles.sync_tasks(self.db, UID, NOW)
        later = get_daily_5am_ts(NOW) + 8 * DAY
        game_cycles.sync_tasks(self.db, UID, later)
        self.claimed_task(6002, 6, timestamp=later + 1)
        saved = self.task(6002)
        reopened = AccountDB(str(self.path))
        try:
            result = game_cycles.sync_tasks(reopened, UID, NOW)
            self.assertFalse(result['daily_reset'])
            self.assertFalse(result['weekly_reset'])
            self.assertEqual(reopened.query('SELECT * FROM task WHERE uid=? AND task_id=6002', (UID,))[0], saved)
        finally:
            reopened.close()

    def test_legacy_far_future_task_deadlines_and_claims_are_repaired(self):
        self.claimed_task(6002, 6, timestamp=get_daily_5am_ts(NOW) - DAY)
        self.db.execute('UPDATE task SET expired_ts=2440962000 WHERE uid=? AND task_id=6002', (UID,))
        self.claimed_task(999001, 0)
        result = game_cycles.sync_tasks(self.db, UID, NOW)
        self.assertTrue(result['daily_reset'])
        self.assertEqual(self.task(6002)['expired_ts'], get_daily_5am_ts(NOW) + DAY)
        self.assertFalse(self.db.is_claimed(UID, 'task', 6002))
        self.assertTrue(self.db.is_claimed(UID, 'task', 999001))

    def test_missing_recurring_tasks_are_seeded_without_unlocking_permanent_tasks(self):
        self.db.execute('DELETE FROM task WHERE uid=? AND task_id=71002', (OTHER,))
        before = {r['task_id'] for r in self.db.query('SELECT task_id FROM task WHERE uid=? AND task_type NOT IN(5,6,7,8)', (OTHER,))}
        game_cycles.sync_tasks(self.db, OTHER, NOW)
        row = self.task(71002, OTHER)
        self.assertEqual((row['progress'], row['claimed_ts']), (0, 0))
        self.assertEqual(row['expired_ts'], get_daily_5am_ts(NOW) + DAY)
        self.assertEqual({r['task_id'] for r in self.db.query('SELECT task_id FROM task WHERE uid=? AND task_type NOT IN(5,6,7,8)', (OTHER,))}, before)

    def test_core_cannot_claim_new_battlepass_with_old_cycle_experience(self):
        first = game_cycles.ensure_battlepass(self.db, UID, NOW)
        self.db.set_currency(UID, 14, 70000)
        engine = core.Core(db=self.db, generator=generator, codec_encode=encode, codec_decode=decode)
        with patch('time.time', return_value=first['end_timestamp']):
            response = engine.dispatch(core.CoreRequest(34034, encode('cs_34034', {}), uid=UID))
        packet = decode(next(f['payload'] for f in response.frames if f['sc'] == 34035), 'sc_34035')
        self.assertEqual(packet.get('reward_list', []), [])
        self.assertEqual(self.scalar('SELECT num FROM currency WHERE uid=? AND id=14', (UID,)), 0)
        self.assertTrue(any(f['sc'] == 34031 for f in response.frames))
        self.assertTrue(any(f['sc'] == 11001 for f in response.frames))
        self.assertTrue(any(f['sc'] == 16015 for f in response.frames))

    def test_manual_mail_respects_duration_and_absolute_deadline(self):
        for payload, deadline in (({'valid_days': 3}, NOW + 3 * DAY), ({'expire_time': NOW + 120}, NOW + 120)):
            result = self.post('mail/send', {'uid': OTHER, 'title': '测试邮件', 'content': '测试正文', 'attachments': [{'id': 38, 'number': 20}], **payload})
            self.assertEqual(result['code'], 0, result)
            self.assertEqual(result['data']['expire_time'], deadline)
            mid = result['data']['mail_id']
            self.assertEqual(self.scalar('SELECT expire_time FROM mail WHERE uid=? AND mail_id=?', (OTHER, mid)), deadline)
            packet = decode(generator.gen_payload(30003, uid=OTHER, db=self.db), 'sc_30003')
            self.assertEqual(next(r['timeout_timestamp'] for r in packet['mail_list'] if r['id'] == mid), deadline)

    def test_invalid_mail_expiry_and_attachments_do_not_send(self):
        before = self.scalar('SELECT COUNT(*) FROM mail WHERE uid=?', (OTHER,))
        for value in ({'valid_days': 0}, {'valid_days': True}, {'valid_days': 1.5}, {'expire_time': NOW},
                      {'valid_days': 3, 'expire_time': NOW + DAY}, {'attachments': [{'id': 38, 'number': True}]},
                      {'attachments': [{'id': 99999999, 'number': 1}]}, {'attachments': [{'id': 310001, 'number': 1}]}, {'attachments': False}):
            result = self.post('mail/send', {'uid': OTHER, 'title': '测试邮件', 'content': '正文', **value})
            self.assertNotEqual(result['code'], 0, (value, result))
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM mail WHERE uid=?', (OTHER,)), before)

    def test_expired_mail_is_hidden_and_cannot_be_claimed_or_revived(self):
        service = MailService.get_instance(self.db)
        mid = service.send_mail(OTHER, '测试', '正文', [{'id': 38, 'number': 20}], expire_time=NOW + 60, db=self.db)
        before = self.scalar('SELECT num FROM currency WHERE uid=? AND id=38', (OTHER,))
        with patch('time.time', return_value=NOW + 60):
            self.assertFalse(any(r['id'] == mid for r in service.get_inbox_list(OTHER, self.db)))
            self.assertIsNone(service.get_mail_detail(OTHER, mid, self.db))
            with self.assertRaises(ValueError):
                service.claim_mail_attachments(OTHER, mid, db=self.db)
            self.assertEqual(service.get_login_summary(OTHER, self.db), {'unread_number': 0, 'total_number': 0})
        self.assertEqual(self.scalar('SELECT num FROM currency WHERE uid=? AND id=38', (OTHER,)), before)
        self.assertEqual(self.scalar('SELECT expire_time FROM mail WHERE uid=? AND mail_id=?', (OTHER, mid)), NOW + 60)

    def test_bulk_claim_skips_expired_favorites_and_claims_valid_mail_once(self):
        service = MailService.get_instance(self.db)
        expired = service.send_mail(OTHER, '过期测试', '正文', [{'id': 38, 'number': 20}], expire_time=NOW + 10, db=self.db)
        service.toggle_collect(OTHER, expired, 1, self.db)
        valid = service.send_mail(OTHER, '有效测试', '正文', [{'id': 38, 'number': 7}], expire_time=NOW + DAY, db=self.db)
        before = self.scalar('SELECT num FROM currency WHERE uid=? AND id=38', (OTHER,))
        with patch('time.time', return_value=NOW + 10):
            result = service.claim_mail_attachments(OTHER, 0, db=self.db)
            self.assertIn(valid, result['success_mail_ids'])
            self.assertNotIn(expired, result['success_mail_ids'])
            with self.assertRaises(ValueError):
                service.claim_mail_attachments(OTHER, expired, db=self.db)
            service.claim_mail_attachments(OTHER, 0, db=self.db)
        self.assertEqual(self.scalar('SELECT num FROM currency WHERE uid=? AND id=38', (OTHER,)), before + 7)

    def test_mail_sequence_is_not_reused_after_deletion_or_restart(self):
        service = MailService.get_instance(self.db)
        first = service.send_mail(OTHER, '测试', '正文', db=self.db)
        self.db.execute('DELETE FROM mail WHERE uid=? AND mail_id=?', (OTHER, first))
        reopened = AccountDB(str(self.path))
        try:
            second = MailService.get_instance(reopened).send_mail(OTHER, '测试二', '正文', db=reopened)
            self.assertGreater(second, first)
        finally:
            reopened.close()

    def test_mail_history_failure_reports_committed_delivery_as_success(self):
        with patch('gm_api._append_gm_mail_history', side_effect=OSError('disk error')):
            result = self.post('mail/send', {'uid': OTHER, 'title': '测试邮件', 'content': '正文', 'valid_days': 1})
        self.assertEqual(result['code'], 0, result)
        self.assertFalse(result['data']['audit_saved'])
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM mail WHERE uid=? AND mail_id=?', (OTHER, result['data']['mail_id'])), 1)

    def test_level_range_delivers_once_without_changing_assets_before_claim(self):
        rule = self.rule(level_min=11, level_max=20)
        before = self.db.query('SELECT * FROM currency WHERE uid=?', (OTHER,))
        self.assertFalse(mail_automation.deliver_for_user(self.db, UID, NOW)['sent'])
        sent = mail_automation.deliver_for_user(self.db, OTHER, NOW)['sent']
        self.assertEqual(len(sent), 1)
        self.assertFalse(mail_automation.deliver_for_user(self.db, OTHER, NOW + DAY)['sent'])
        self.assertEqual(self.db.query('SELECT * FROM currency WHERE uid=?', (OTHER,)), before)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM gm_level_mail_delivery WHERE rule_id=?', (rule['id'],)), 1)
        row = self.db.query('SELECT * FROM mail WHERE uid=? AND mail_id=?', (OTHER, sent[0]['mail_id']))[0]
        self.assertEqual(row['expire_time'], NOW + 30 * DAY)
        self.assertEqual(json.loads(row['attachment_json']), [{'id': 38, 'number': 20}])

    def test_default_gift_contains_twenty_precision_tickets_and_training_materials(self):
        self.db.execute('UPDATE gm_level_mail_rule SET enabled=1 WHERE id=1')
        result = mail_automation.run_pending(self.db, NOW)
        self.assertEqual({row['uid'] for row in result['sent']}, {UID, OTHER})
        for entry in result['sent']:
            attachments = json.loads(self.db.query('SELECT attachment_json FROM mail WHERE uid=? AND mail_id=?', (entry['uid'], entry['mail_id']))[0]['attachment_json'])
            items = {row['id']: row['number'] for row in attachments}
            self.assertEqual(items[38], 20)
            self.assertTrue({2, 40103, 40301, 41301} <= set(items))

    def test_rule_disable_edit_delete_mail_and_restart_keep_receipts(self):
        rule = self.rule(level_min=11, level_max=20)
        sent = mail_automation.deliver_for_user(self.db, OTHER, NOW)['sent'][0]
        self.db.execute('DELETE FROM mail WHERE uid=? AND mail_id=?', (OTHER, sent['mail_id']))
        mail_automation.save_rule(self.db, {'id': rule['id'], 'enabled': False}, NOW)
        mail_automation.save_rule(self.db, {'id': rule['id'], 'enabled': True, 'attachments': [{'id': 38, 'number': 30}]}, NOW)
        reopened = AccountDB(str(self.path))
        try:
            self.assertFalse(mail_automation.deliver_for_user(reopened, OTHER, NOW + DAY)['sent'])
        finally:
            reopened.close()

    def test_new_accounts_only_excludes_existing_eligible_players(self):
        rule = self.rule(include_existing=False, level_min=11, level_max=20)
        self.assertFalse(mail_automation.deliver_for_user(self.db, OTHER, NOW)['sent'])
        fresh_uid = 987654321
        self.db.execute("INSERT INTO users(uid,nick,level,register_ts) VALUES(?,'新用户',11,?)", (fresh_uid, NOW + 1))
        self.assertEqual(len(mail_automation.deliver_for_user(self.db, fresh_uid, NOW + 1)['sent']), 1)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM gm_level_mail_delivery WHERE rule_id=? AND uid=? AND status='skipped'", (rule['id'], OTHER)), 1)

    def test_disabled_accounts_are_not_sent_automatic_mail(self):
        self.rule()
        self.db.execute("INSERT INTO auth_accounts(uid,username,password_hash,disabled,created_at) VALUES(?,'cycle-test-disabled','unused-test-hash',1,?) ON CONFLICT(uid) DO UPDATE SET disabled=1", (OTHER, NOW))
        self.assertFalse(mail_automation.deliver_for_user(self.db, OTHER, NOW)['sent'])

    def test_background_scheduler_sends_level_mail_without_player_requests(self):
        self.rule(level_min=11, level_max=20)
        delivered = threading.Event()
        with patch('server_net.push_mail_notice', side_effect=lambda uid: delivered.set()):
            scheduler = runtime_scheduler.RuntimeScheduler(self.db).start()
            try:
                self.assertTrue(delivered.wait(timeout=5), scheduler.status())
                self.assertEqual(self.scalar('SELECT COUNT(*) FROM gm_level_mail_delivery WHERE uid=?', (OTHER,)), 1)
            finally:
                scheduler.stop()
        self.assertFalse(scheduler.status()['running'])

    def test_concurrent_level_delivery_creates_only_one_mail_and_receipt(self):
        rule = self.rule(level_min=11, level_max=20)
        def worker():
            try:
                return mail_automation.deliver_for_user(self.db, OTHER, NOW)
            finally:
                self.db.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: worker(), range(2)))
        self.assertEqual(sum(len(r['sent']) for r in results), 1, results)
        self.assertFalse(any(r['failures'] for r in results), results)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM gm_level_mail_delivery WHERE rule_id=? AND uid=?', (rule['id'], OTHER)), 1)

    def test_failed_level_delivery_is_retryable_without_partial_receipt(self):
        rule = self.rule(level_min=11, level_max=20)
        before = self.scalar('SELECT COUNT(*) FROM mail WHERE uid=?', (OTHER,))
        with patch.object(MailService, 'send_mail', side_effect=ValueError('temporary failure')):
            result = mail_automation.deliver_for_user(self.db, OTHER, NOW)
        self.assertEqual(len(result['failures']), 1)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM gm_level_mail_delivery WHERE rule_id=?', (rule['id'],)), 0)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM mail WHERE uid=?', (OTHER,)), before)
        sent = mail_automation.deliver_for_user(self.db, OTHER, NOW + 30)['sent']
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM gm_level_mail_failure WHERE rule_id=?', (rule['id'],)), 0)

    def test_failure_after_mail_insert_rolls_back_mail_and_receipt(self):
        rule = self.rule(level_min=11, level_max=20)
        before = self.scalar('SELECT COUNT(*) FROM mail WHERE uid=?', (OTHER,))
        execute = self.db.execute
        def failing(sql, args=()):
            if sql.startswith('UPDATE gm_level_mail_delivery SET mail_id='):
                raise ValueError('receipt failed')
            return execute(sql, args)
        with patch.object(self.db, 'execute', side_effect=failing):
            self.assertTrue(mail_automation.deliver_for_user(self.db, OTHER, NOW)['failures'])
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM mail WHERE uid=?', (OTHER,)), before)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM gm_level_mail_delivery WHERE rule_id=?', (rule['id'],)), 0)
        self.assertEqual(len(mail_automation.deliver_for_user(self.db, OTHER, NOW + 30)['sent']), 1)

    def test_upgrade_mail_and_notice_rollback_with_owning_core_operation(self):
        self.rule(level_min=15, level_max=100)
        self.db.execute('UPDATE users SET level=14 WHERE uid=?', (OTHER,))
        class Upgrade(core.Operation):
            cmd, sc, prepare_response_in_transaction = 987771, 987772, True
            def apply(op, data, ctx):
                ctx.db.execute('UPDATE users SET level=15 WHERE uid=?', (op.uid,))
                from event_bus import bus, Events
                bus.emit(Events.PLAYER_LEVEL_UP, ctx, op.uid, old_lv=14, new_lv=15)
                return {}
            def respond(op, result, data, ctx):
                raise ValueError('response encoding failed')
        engine = core.Core(db=self.db, generator=generator, codec_encode=encode, codec_decode=decode)
        before = self.scalar('SELECT COUNT(*) FROM mail WHERE uid=?', (OTHER,))
        with patch.dict(core.OPERATIONS, {Upgrade.cmd: Upgrade()}):
            response = engine.dispatch(core.CoreRequest(Upgrade.cmd, {}, uid=OTHER))
        self.assertEqual(self.scalar('SELECT level FROM users WHERE uid=?', (OTHER,)), 14)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM mail WHERE uid=?', (OTHER,)), before)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM gm_level_mail_delivery WHERE uid=?', (OTHER,)), 0)
        self.assertFalse(any(frame['sc'] == 30001 for frame in response.frames))

    def test_real_experience_upgrade_triggers_level_mail_and_respects_grant_rollback(self):
        self.rule(level_min=15, level_max=100)
        before = self.scalar('SELECT COUNT(*) FROM mail WHERE uid=?', (OTHER,))
        old_level = self.scalar('SELECT level FROM users WHERE uid=?', (OTHER,))
        with patch('gm_actions.asset_frames', side_effect=ValueError('encoding failed')):
            with self.assertRaises(ValueError):
                gm_actions.grant(self.db, OTHER, 12, 1000000)
        self.assertEqual(self.scalar('SELECT level FROM users WHERE uid=?', (OTHER,)), old_level)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM mail WHERE uid=?', (OTHER,)), before)
        result = gm_actions.grant(self.db, OTHER, 12, 1000000)
        self.assertGreater(self.scalar('SELECT level FROM users WHERE uid=?', (OTHER,)), old_level)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM mail WHERE uid=?', (OTHER,)), before + 1)

    def test_rotation_boundary_persists_cycle_and_preserves_pity_and_targets(self):
        cfg = self.rotate()
        self.draw.set_pity(UID, 'hero_precision_70', 65, is_up_guaranteed=1)
        self.draw.set_pool_up(UID, 5030601, 1044)
        saved = {table: self.db.query('SELECT * FROM ' + table + ' WHERE uid=?', (UID,)) for table in ('draw_group_state', 'draw_state', 'draw_record')}
        initial = game_cycles.apply_rotation(self.db, cfg['start_ts'])
        self.assertIn(5020301, initial['pool_ids'])
        next_rotation = game_cycles.apply_rotation(self.db, cfg['start_ts'] + 7 * DAY)
        self.assertIn(4050301, next_rotation['pool_ids'])
        self.assertNotIn(5020301, next_rotation['pool_ids'])
        self.assertTrue(set(cfg['fixed_pool_ids']) <= set(next_rotation['pool_ids']))
        self.assertEqual({table: self.db.query('SELECT * FROM ' + table + ' WHERE uid=?', (UID,)) for table in saved}, saved)
        reopened = AccountDB(str(self.path))
        try:
            self.assertFalse(game_cycles.apply_rotation(reopened, cfg['start_ts'] + 7 * DAY + 1)['changed'])
            self.assertEqual(game_cycles.apply_rotation(reopened, cfg['start_ts'])['step_index'], 1)
        finally:
            reopened.close()

    def test_rotation_wait_disable_and_wrap_have_expected_pool_membership(self):
        before = self.draw.get_active_pools()
        cfg = self.rotate(start_ts=NOW + DAY)
        self.assertFalse(game_cycles.apply_rotation(self.db, NOW)['changed'])
        self.assertEqual(self.draw.get_active_pools(), before)
        first = game_cycles.apply_rotation(self.db, cfg['start_ts'])
        wrapped = game_cycles.apply_rotation(self.db, cfg['start_ts'] + 14 * DAY)
        self.assertEqual(first['pool_ids'], wrapped['pool_ids'])
        game_cycles.save_settings(self.db, {'rotation': {'enabled': False}}, cfg['start_ts'] + 14 * DAY)
        self.assertFalse(game_cycles.apply_rotation(self.db, cfg['start_ts'] + 21 * DAY)['changed'])
        self.assertEqual(set(self.draw.get_active_pools()), set(first['pool_ids']))

    def test_rotating_activity_packets_use_real_deadlines_and_removed_pool_is_not_charged(self):
        cfg = self.rotate()
        game_cycles.apply_rotation(self.db, cfg['start_ts'])
        at = cfg['start_ts'] + 7 * DAY
        with patch('time.time', return_value=at):
            game_cycles.apply_rotation(self.db, at)
            payload = decode(generator.gen_payload(11001, uid=UID, db=self.db), 'sc_11001')
            activity_id = self.draw.load_pool_activity_cfg()['4050301']['activity_id']
            entry = next(row for row in payload['activity_list'] if row['activity_id'] == activity_id)
            self.assertEqual((entry['start_time'], entry['stop_time']), (at, at + 7 * DAY))
            before = self.scalar('SELECT num FROM currency WHERE uid=? AND id=38', (UID,))
            with self.db.transaction():
                with self.assertRaises(ValueError):
                    self.draw.draw(gm_actions.context(self.db), UID, 5020301, 1)
            self.assertEqual(self.scalar('SELECT num FROM currency WHERE uid=? AND id=38', (UID,)), before)

    def test_rotation_sync_failure_does_not_partially_change_active_pools(self):
        cfg = self.rotate()
        before = self.db.query('SELECT * FROM active_draw_pool ORDER BY pool_id')
        with patch.object(self.draw, 'sync_active_pool_activities', side_effect=ValueError('activity failure')):
            with self.assertRaises(ValueError):
                game_cycles.apply_rotation(self.db, cfg['start_ts'])
        self.assertEqual(self.db.query('SELECT * FROM active_draw_pool ORDER BY pool_id'), before)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM server_global_config WHERE key=?', (game_cycles.ROTATION_KEY,)), 0)

    def test_rotation_notification_survives_restart_and_is_not_repeated(self):
        self.rotate()
        game_cycles.apply_rotation(self.db, NOW)
        with patch('runtime_scheduler.notify_rotation', side_effect=ValueError('network failure')):
            with self.assertRaises(ValueError):
                runtime_scheduler.run_once(self.db, NOW)
        self.assertTrue(game_cycles.rotation_status(self.db, NOW)['notify_required'])
        with patch('runtime_scheduler.notify_rotation', return_value={'clients': 1}) as notified:
            runtime_scheduler.run_once(self.db, NOW + 30)
            runtime_scheduler.run_once(self.db, NOW + 60)
        self.assertEqual(notified.call_count, 1)
        self.assertFalse(game_cycles.rotation_status(self.db, NOW)['notify_required'])

    def test_schedule_validation_is_atomic_and_respects_version_compatibility(self):
        before = game_cycles.get_settings(self.db, NOW)
        for invalid in ({'duration_days': 0}, {'duration_days': 1.5}, {'fixed_pool_ids': [True]},
                        {'steps': []}, {'fixed_pool_ids': [9999999]}, {'enabled': 'true'}):
            with self.assertRaises(ValueError):
                game_cycles.save_settings(self.db, {'battlepass': {'duration_days': 7}, 'rotation': invalid}, NOW)
            self.assertEqual(game_cycles.get_settings(self.db, NOW), before)
        with patch('res_version_manager.get_current_version', return_value='229'):
            with self.assertRaises(ValueError):
                self.rotate()

    def test_api_reports_periods_rules_and_rejects_invalid_rule_without_changes(self):
        result = self.client.get('/api/gm/automation/status').get_json()
        self.assertEqual(result['code'], 0, result)
        self.assertEqual(result['data']['battlepass']['end_ts'] - result['data']['battlepass']['start_ts'], 42 * DAY)
        for invalid in ({'level_min': 21, 'level_max': 20}, {'enabled': True, 'valid_days': 0},
                        {'enabled': 'yes'}, {'attachments': [{'id': 310001, 'number': 1}]}):
            before = self.db.query('SELECT * FROM gm_level_mail_rule ORDER BY id')
            result = self.post('mail/rules', {'name': '测试规则', 'attachments': [{'id': 38, 'number': 20}], **invalid})
            self.assertNotEqual(result['code'], 0, result)
            self.assertEqual(self.db.query('SELECT * FROM gm_level_mail_rule ORDER BY id'), before)
