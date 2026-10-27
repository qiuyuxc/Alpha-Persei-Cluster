"""Explore timeline IDs must resolve in the client's ExploringEventCfg."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import random
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

SEED = Path(os.environ['GM_TEST_DB_SEED']).resolve()
SANDBOX = tempfile.TemporaryDirectory(prefix='ag-explore-event-tests-')
BOOTSTRAP = Path(SANDBOX.name) / 'bootstrap.db'
shutil.copyfile(SEED, BOOTSTRAP)
os.environ['ACCOUNT_DB_PATH'] = str(BOOTSTRAP)

from account_db import AccountDB
from admin_cat_explore_service import AdminCatExploreService
from codec import decode, encode
from gm_actions import context
from inventory_service import InventoryService

UID = 2174928301
OTHER = 264084870
NOW = int(time.time())


class ExploreEventTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=SANDBOX.name)
        self.path = Path(self.folder.name) / 'player.db'
        shutil.copyfile(SEED, self.path)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('time.time', return_value=NOW))
        self.stack.enter_context(patch('event_bus.bus.emit'))
        self.db = AccountDB(str(self.path))
        self.ctx = context(self.db)
        self.service = AdminCatExploreService()
        self.db.execute('DELETE FROM admin_cat_explore_queue WHERE uid=?', (UID,))
        self.service.get_or_create_explore_data(self.db, UID)

    def tearDown(self):
        self.stack.close()
        self.db.close()
        self.folder.cleanup()

    def save_queue(self, events, uid=UID, area=10001, cat=10001):
        self.db.save_admin_cat_queue(uid, area, cat, NOW - 72000, NOW - 1, 20, events)

    def packet_queue(self, area=10001):
        payload = encode('sc_67001', self.service.generate_sc_67001_dict(self.db, UID))
        queues = decode(payload, 'sc_67001')['explore_queue']
        return next(queue for queue in queues if queue['area_id'] == area)

    def state(self):
        return {table: sorted(self.db.query('SELECT * FROM ' + table),
                              key=lambda row: json.dumps(row, sort_keys=True))
                for table in ('currency', 'material', 'admin_cat_explore_queue')}

    def test_old_invalid_events_filtered_without_changing_queue_or_assets(self):
        valid = {'time': 60, 'address_id': 101, 'content_id': 201}
        self.save_queue([valid, {'time': 3063, 'address_id': 105, 'content_id': 291},
                         {'time': 6367, 'address_id': 102, 'content_id': 238}])
        before = self.state()
        queue = self.packet_queue()
        self.assertEqual(queue['explore_event'], [valid])
        self.assertEqual(queue['mimir_id'], 10001)
        self.assertEqual(queue['stop_time'], NOW - 1)
        self.assertEqual(queue['target_explore_hour'], 20)
        self.assertEqual(self.state(), before)

    def test_client_content_boundaries_survive_binary_login(self):
        ids = (201, 224, 225, 278, 300, 301, 306)
        self.save_queue([{'time': n * 60, 'address_id': 110, 'content_id': value}
                         for n, value in enumerate(ids)])
        self.assertEqual([e['content_id'] for e in self.packet_queue()['explore_event']],
                         [201, 224, 301, 306])

    def test_all_invalid_events_keep_the_dispatch_visible(self):
        self.save_queue([{'time': 243, 'address_id': 109, 'content_id': 278}])
        queue = self.packet_queue()
        self.assertEqual(queue['area_id'], 10001)
        self.assertEqual(queue.get('explore_event', []), [])
        self.assertEqual(len(self.db.get_admin_cat_queues(UID)), 1)

    def test_new_dispatches_only_generate_existing_client_event_ids(self):
        InventoryService.grant_items(self.ctx, UID, [(53, 1000000)], source='test_explore_level')
        for cat in range(10001, 10006):
            self.db.unlock_admin_cat(UID, cat, skill_level=1)
        with patch('admin_cat_explore_service.random', random.Random(17)):
            for area in range(10001, 10006):
                self.assertEqual(self.service.start_explore(self.ctx, UID, area, 20, area), 0)
        queues = self.db.get_admin_cat_queues(UID)
        self.assertEqual(len(queues), 5)
        for queue in queues:
            self.assertTrue(queue['explore_event'])
            for event in queue['explore_event']:
                self.assertTrue(101 <= event['address_id'] <= 110)
                self.assertTrue(201 <= event['content_id'] <= 224 or 301 <= event['content_id'] <= 306)
                self.assertGreater(event['time'], 0)
                self.assertLess(event['time'], 72000)
            self.assertEqual(self.packet_queue(queue['area_id'])['explore_event'], queue['explore_event'])

    def test_valid_narrative_and_reward_events_are_preserved(self):
        events = [{'time': 60, 'address_id': 101, 'content_id': 201},
                  {'time': 300, 'address_id': 105, 'content_id': 224},
                  {'time': 1800, 'address_id': 110, 'content_id': 301},
                  {'time': 3600, 'address_id': 102, 'content_id': 306}]
        self.save_queue(events)
        self.assertEqual(self.packet_queue()['explore_event'], events)

    def test_malformed_legacy_events_do_not_break_the_login_packet(self):
        self.save_queue([None, 3, {'time': 60, 'address_id': 111, 'content_id': 201},
                         {'address_id': 101, 'content_id': 201},
                         {'time': -1, 'address_id': 101, 'content_id': 201}])
        before = self.state()
        self.assertEqual(self.packet_queue().get('explore_event', []), [])
        self.assertEqual(self.state(), before)
        self.save_queue({'invalid': 'timeline'})
        self.assertEqual(self.packet_queue().get('explore_event', []), [])

    def test_legacy_queue_settlement_credits_rewards_exactly_once(self):
        self.save_queue([{'time': 243, 'address_id': 109, 'content_id': 278}])
        before = {item: InventoryService.get_item_balance(self.ctx, UID, item)[1] for item in (53, 54, 55, 56)}
        with patch('admin_cat_explore_service.random.randint', return_value=100), self.db.transaction():
            result, event, rewards = self.service.finish_explore(self.ctx, UID, 10001)
        self.assertEqual(result, 0)
        self.assertEqual(event, 0)
        self.assertTrue(rewards)
        for reward in rewards:
            self.assertEqual(InventoryService.get_item_balance(self.ctx, UID, reward['id'])[1],
                             before[reward['id']] + reward['num'])
        self.assertEqual(self.db.get_admin_cat_queues(UID), [])
        after = self.state()
        with self.db.transaction():
            result, event, rewards = self.service.finish_explore(self.ctx, UID, 10001)
        self.assertNotEqual(result, 0)
        self.assertEqual(rewards, [])
        self.assertEqual(self.state(), after)

    def test_other_players_dispatch_data_is_preserved(self):
        self.save_queue([{'time': 243, 'address_id': 109, 'content_id': 278}], uid=OTHER)
        before = self.db.query('SELECT * FROM admin_cat_explore_queue WHERE uid=?', (OTHER,))
        self.save_queue([{'time': 60, 'address_id': 101, 'content_id': 201}])
        self.packet_queue()
        self.assertEqual(self.db.query('SELECT * FROM admin_cat_explore_queue WHERE uid=?', (OTHER,)), before)

    def test_final_bonus_event_and_rewards_still_resolve(self):
        self.save_queue([{'time': 60, 'address_id': 101, 'content_id': 301}])
        with patch('admin_cat_explore_service.random.randint', return_value=1), \
             patch('admin_cat_explore_service.random.choice', return_value=10001), self.db.transaction():
            result, event, rewards = self.service.finish_explore(self.ctx, UID, 10001)
        self.assertEqual(result, 0)
        self.assertEqual(event, 10001)
        self.assertTrue(rewards)
        response = decode(encode('sc_67007', {'result': result, 'event_id': event, 'reward_list': rewards}), 'sc_67007')
        self.assertEqual(response['event_id'], 10001)
        self.assertEqual(response['reward_list'], rewards)


if __name__ == '__main__':
    unittest.main()
