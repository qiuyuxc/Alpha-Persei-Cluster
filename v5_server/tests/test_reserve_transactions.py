"""Saved teams must share the owning request's commit and rollback boundary."""
from contextlib import ExitStack
from copy import deepcopy
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from test_seed import get_seed_path

SEED = get_seed_path()
SANDBOX = tempfile.TemporaryDirectory(prefix='apc-reserve-transactions-')
BOOT = Path(SANDBOX.name) / 'bootstrap.db'
shutil.copyfile(SEED, BOOT)
os.environ['ACCOUNT_DB_PATH'] = str(BOOT)

from account_db import AccountDB
from codec import decode
from core import Core
import event_bus
from gm_actions import context
from middleware import CSRequest
import reserve_codec

UID, PEER, TEAM_TYPE, NOW = 2174928301, 264084870, 314, 1793614200
TABLES = ('reserve_team', 'reserve_team_hero', 'currency',
          'equip_seizure_progress', 'rogueteam_session')


def _native_reserve_payload(data):
    # The client formation message shares names with incompatible hero-profile
    # definitions. Use its native field numbers rather than the generic codec.
    def varint(value):
        result = bytearray()
        while value > 127:
            result.append((value & 127) | 128)
            value >>= 7
        result.append(value)
        return bytes(result)

    def scalar(number, value):
        return varint(number << 3) + varint(value)

    def message(number, body):
        return varint((number << 3) | 2) + varint(len(body)) + body

    container = data['cont_team']
    contents = scalar(1, container['cont_id'])
    for team in container['teams']:
        body = scalar(1, team['team_index'])
        for hero in team['hero_list']:
            body += message(2, scalar(1, hero['hero_id']) + scalar(3, hero['hero_type']))
        body += scalar(3, team['cooperate_unique_skill_id'])
        body += message(4, scalar(1, team['mimir_info']['mimir_id']))
        contents += message(2, body)
    return scalar(1, data['team_type']) + message(2, contents)


class ReserveTransactions(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=SANDBOX.name)
        self.addCleanup(self.folder.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('time.time', return_value=NOW))
        self.stack.enter_context(patch.object(event_bus.EventBus, '_handlers', {}))
        self.stack.enter_context(patch('event_bus.bus.emit'))
        self.path = Path(self.folder.name) / 'player.db'
        shutil.copyfile(SEED, self.path)
        self.db = AccountDB(str(self.path))
        self.addCleanup(self.db.close)
        self.ctx = context(self.db)
        self.ctx.uid = UID
        self.messages = []
        self.ctx.log = lambda message, *a, **k: self.messages.append(str(message))
        self.heroes = [row['id'] for row in self.db.query(
            'SELECT id FROM hero WHERE uid=? ORDER BY id LIMIT 3', (UID,))]
        self.assertGreaterEqual(len(self.heroes), 2)
        self.db.execute('INSERT OR IGNORE INTO equip_seizure_progress(uid) VALUES (?)', (UID,))
        self.db.execute('''INSERT OR REPLACE INTO reserve_team
            (uid,team_type,cont_id,team_index,cooperate_skill,update_ts)
            VALUES (?,?,0,1,77,1)''', (UID, TEAM_TYPE))
        self.db.execute('''INSERT OR REPLACE INTO reserve_team_hero
            (uid,team_type,cont_id,team_index,slot,hero_id,hero_type,update_ts)
            VALUES (?,?,0,1,1,?,1,1)''', (UID, TEAM_TYPE, self.heroes[-1]))
        self.peer = self.state(PEER)

    def tearDown(self):
        self.assertEqual(self.state(PEER), self.peer)
        self.assertEqual(self.db.query('PRAGMA integrity_check')[0]['integrity_check'], 'ok')
        with sqlite3.connect('file:' + str(self.path) + '?mode=ro', uri=True) as saved:
            saved.row_factory = sqlite3.Row
            self.assertEqual(self.state(connection=saved), self.state())

    def state(self, uid=UID, connection=None):
        result = {}
        for table in TABLES:
            sql = 'SELECT * FROM ' + table + ' WHERE uid=? ORDER BY rowid'
            result[table] = ([dict(row) for row in connection.execute(sql, (uid,))]
                             if connection else self.db.query(sql, (uid,)))
        return result

    def data(self, team_type=TEAM_TYPE):
        return {'team_type': team_type, 'cont_team': {'cont_id': 0, 'teams': [{
            'team_index': 1, 'cooperate_unique_skill_id': 84,
            'hero_list': [{'hero_id': hid, 'hero_type': 1} for hid in self.heroes],
            'mimir_info': {'mimir_id': 0, 'chip_list': [], 'hero_chip_list': []},
        }]}}

    def request(self, data=None, frame_packer=None):
        req = CSRequest(63000, _native_reserve_payload(self.data() if data is None else data),
                        uid=UID, frame_packer=frame_packer)
        response = Core(db=self.db)._run_handler(req, self.ctx, (), ())
        frame = next(frame for frame in response.frames if frame['sc'] == 63001)
        return decode(frame['payload'], 'sc_63001')['result']

    def fail_second_hero(self):
        self.db.execute('''CREATE TRIGGER reserve_save_fault BEFORE INSERT ON reserve_team_hero
            WHEN NEW.uid=2174928301 AND NEW.team_type=314 AND NEW.slot=2
            BEGIN SELECT RAISE(ABORT, 'reserve-save-fault'); END''')

    def assert_saved(self, connection=None):
        state = self.state(connection=connection)
        team = next(row for row in state['reserve_team'] if row['team_type'] == TEAM_TYPE
                    and row['cont_id'] == 0 and row['team_index'] == 1)
        self.assertEqual(team['cooperate_skill'], 84)
        self.assertEqual([row['hero_id'] for row in state['reserve_team_hero']
                          if row['team_type'] == TEAM_TYPE and row['team_index'] == 1], self.heroes)

    def test_save_request_succeeds_and_persists(self):
        self.assertEqual(self.request(), 0, self.messages)
        self.assert_saved()
        self.assertFalse(self.db._in_txn())
        self.assertFalse(self.db._checked_conn().in_transaction)

    def test_empty_team_list_does_not_break_reply(self):
        before = self.state()
        self.assertEqual(self.request({'team_type': TEAM_TYPE, 'cont_team': {'cont_id': 0, 'teams': []}}), 0,
                         self.messages)
        self.assertEqual(self.state(), before)

    def test_wire_failure_rolls_back_saved_team(self):
        before = self.state()
        def fail_wire(*a, **k):
            raise RuntimeError('test packet preparation failed')
        self.assertNotEqual(self.request(frame_packer=fail_wire), 0)
        self.assertEqual(self.state(), before)

    def test_sql_failure_returns_error_and_rolls_back_all_slots(self):
        self.fail_second_hero()
        before = self.state()
        self.assertNotEqual(self.request(), 0)
        self.assertEqual(self.state(), before)

    def test_multiple_teams_are_one_atomic_save(self):
        self.db.execute('''CREATE TRIGGER reserve_team_fault BEFORE INSERT ON reserve_team
            WHEN NEW.uid=2174928301 AND NEW.team_type=314 AND NEW.team_index=2
            BEGIN SELECT RAISE(ABORT, 'second-team-fault'); END''')
        data = self.data()
        second = deepcopy(data['cont_team']['teams'][0])
        second['team_index'] = 2
        data['cont_team']['teams'].append(second)
        before = self.state()
        self.assertNotEqual(self.request(data), 0)
        self.assertEqual(self.state(), before)

    def test_standalone_accountdb_save_commits(self):
        self.assertTrue(reserve_codec.save_team_from_cs_63000(self.db, UID, self.data()))
        with sqlite3.connect('file:' + str(self.path) + '?mode=ro', uri=True) as saved:
            saved.row_factory = sqlite3.Row
            self.assert_saved(saved)

    def test_nested_save_respects_outer_rollback(self):
        before = self.state()
        with self.assertRaisesRegex(RuntimeError, 'cancel owner'):
            with self.db.transaction():
                self.db.execute('UPDATE currency SET num=num+5 WHERE uid=? AND id=31', (UID,))
                reserve_codec.save_team_from_cs_63000(self.db, UID, self.data())
                self.assertTrue(self.db._checked_conn().in_transaction)
                raise RuntimeError('cancel owner')
        self.assertEqual(self.state(), before)

    def test_mirrored_equipment_team_and_metadata_follow_outer_rollback(self):
        before = self.state()
        with self.assertRaisesRegex(RuntimeError, 'cancel mirrored save'):
            with self.db.transaction():
                reserve_codec.save_team_from_cs_63000(self.db, UID, self.data(team_type=11))
                raise RuntimeError('cancel mirrored save')
        self.assertEqual(self.state(), before)

    def test_raw_sqlite_save_respects_existing_transaction(self):
        with sqlite3.connect(self.path) as raw:
            raw.row_factory = sqlite3.Row
            before = self.state(connection=raw)
            raw.execute('BEGIN')
            raw.execute('UPDATE currency SET num=num+5 WHERE uid=? AND id=31', (UID,))
            reserve_codec.save_team_from_cs_63000(raw, UID, self.data())
            self.assertTrue(raw.in_transaction)
            raw.rollback()
            self.assertEqual(self.state(connection=raw), before)

    def test_standalone_raw_sqlite_save_commits(self):
        with sqlite3.connect(self.path) as raw:
            raw.row_factory = sqlite3.Row
            reserve_codec.save_team_from_cs_63000(raw, UID, self.data())
            self.assertFalse(raw.in_transaction)
        self.assert_saved()

    def test_raw_sqlite_failure_rolls_back_partial_save(self):
        self.fail_second_hero()
        with sqlite3.connect(self.path) as raw:
            raw.row_factory = sqlite3.Row
            before = self.state(connection=raw)
            with self.assertRaises(sqlite3.IntegrityError):
                reserve_codec.save_team_from_cs_63000(raw, UID, self.data())
            self.assertFalse(raw.in_transaction)
            self.assertEqual(self.state(connection=raw), before)

    def test_failed_save_does_not_poison_next_request(self):
        self.fail_second_hero()
        self.assertNotEqual(self.request(), 0)
        self.db.execute('DROP TRIGGER reserve_save_fault')
        self.assertEqual(self.request(), 0, self.messages)
        self.assert_saved()
