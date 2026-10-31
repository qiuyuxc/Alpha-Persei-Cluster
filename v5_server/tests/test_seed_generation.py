"""Public fixture reproducibility, isolation, and failure handling."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_seed import ADMIN, OTHER, SERVER, UID, generate_seed, get_seed_path


class SeedGenerationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix='apc-seed-contract-')
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)

    def test_repeated_builds_have_identical_data_and_manifests(self):
        first = generate_seed(self.root / 'first.db')
        with patch.dict(os.environ, {'TZ': 'UTC', 'GM_TEST_DB_SEED': str(self.root / 'absent.db')}):
            second = generate_seed(self.root / 'second.db')
        self.assertEqual(first, second)
        self.assertEqual((self.root / 'first.db').read_bytes(), (self.root / 'second.db').read_bytes())

    def test_generated_seed_has_complete_schema_and_synthetic_accounts(self):
        manifest = generate_seed(self.root / 'seed.db')
        with sqlite3.connect('file:' + str(self.root / 'seed.db') + '?mode=ro', uri=True) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual({row[0] for row in db.execute('SELECT uid FROM users')}, {UID, OTHER, ADMIN})
            self.assertTrue(all(row[0].startswith('Synthetic ') for row in db.execute('SELECT nick FROM users')))
            for table in ('auth_accounts', 'auth_sessions', 'auth_bootstrap', 'sdk'):
                self.assertEqual(db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM login_push').fetchone()[0], 192)
            for table in ('hero_cfg', 'game_level_setting', 'hero_break_cfg', 'item_catalog', 'task_cfg', 'draw_pool', 'shop_goods'):
                self.assertGreater(db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)
        self.assertTrue(manifest['synthetic'])
        self.assertEqual(manifest['database_sha256'], hashlib.sha256((self.root / 'seed.db').read_bytes()).hexdigest())
        for name, expected in manifest['inputs'].items():
            self.assertEqual(hashlib.sha256((SERVER / name).read_bytes()).hexdigest(), expected)

    def test_existing_database_is_never_replaced(self):
        path = self.root / 'existing.db'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE marker(value TEXT)')
            db.execute("INSERT INTO marker VALUES('keep this save')")
        before = path.read_bytes()
        with self.assertRaises(FileExistsError):
            generate_seed(path)
        self.assertEqual(path.read_bytes(), before)
        worker = subprocess.run([sys.executable, str(SERVER / 'test_seed.py'), '--build', str(path)],
                                capture_output=True, text=True)
        self.assertNotEqual(worker.returncode, 0)
        self.assertIn('Destination already exists', worker.stderr)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(path.with_name(path.name + '.manifest.json').exists())

    def test_existing_manifest_is_preserved_without_creating_database(self):
        path = self.root / 'seed.db'
        manifest = path.with_name(path.name + '.manifest.json')
        manifest.write_text('{"keep": true}', encoding='utf-8')
        with self.assertRaises(FileExistsError):
            generate_seed(path)
        self.assertEqual(json.loads(manifest.read_text()), {'keep': True})
        self.assertFalse(path.exists())

    def test_failed_generation_leaves_no_partial_output(self):
        server = self.root / 'incomplete-source'
        server.mkdir()
        path = self.root / 'failed.db'
        with self.assertRaises(RuntimeError):
            generate_seed(path, server=server)
        self.assertFalse(path.exists())
        self.assertFalse(path.with_name(path.name + '.manifest.json').exists())
        self.assertFalse(list(self.root.glob('.apc-seed-*')))

    def test_generator_ignores_database_environment_variables(self):
        original = self.root / 'unrelated.db'
        original.write_bytes(b'An unrelated save is never read or modified.')
        with patch.dict(os.environ, {'ACCOUNT_DB_PATH': str(original), 'GM_TEST_DB_SEED': str(original)}):
            generated = generate_seed(self.root / 'generated.db')
        self.assertTrue(generated['synthetic'])
        self.assertEqual(original.read_bytes(), b'An unrelated save is never read or modified.')

    def test_import_opens_no_database_and_missing_override_is_clear(self):
        code = """
import sys
def audit(event, args):
    if event == 'sqlite3.connect' or event == 'socket.connect':
        raise AssertionError('Import must not open databases or sockets')
sys.addaudithook(audit)
import test_seed
print('ok')
"""
        env = dict(os.environ, PYTHONPATH=str(SERVER), ACCOUNT_DB_PATH=str(self.root / 'never.db'))
        imported = subprocess.run([sys.executable, '-c', code], env=env, cwd=self.root,
                                  capture_output=True, text=True)
        self.assertEqual(imported.returncode, 0, imported.stderr)
        self.assertEqual(imported.stdout.strip(), 'ok')
        self.assertFalse((self.root / 'never.db').exists())
        with patch.dict(os.environ, {'GM_TEST_DB_SEED': str(self.root / 'missing.db')}):
            with self.assertRaisesRegex(FileNotFoundError, 'GM_TEST_DB_SEED'):
                get_seed_path()
