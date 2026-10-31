"""GM contracts tested against an isolated copy of the complete database."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from test_seed import get_seed_path

SEED = get_seed_path()
SANDBOX = tempfile.TemporaryDirectory(prefix='ag-gm-tests-')
BOOTSTRAP = Path(SANDBOX.name) / 'bootstrap.db'
shutil.copyfile(SEED, BOOTSTRAP)
os.environ['ACCOUNT_DB_PATH'] = str(BOOTSTRAP)

from flask import Flask
from account_db import AccountDB
from codec import decode
import draw_rules
from draw_service import DrawService
import gm_actions
import gm_api
from gm_reader import GMReader
from enchant_slots_util import default_slot_list

UID = 2174928301
OTHER = 10001


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=SANDBOX.name)
        self.path = Path(self.folder.name) / 'player.db'
        shutil.copyfile(SEED, self.path)
        self.events = patch('event_bus.bus.emit').start()
        self.db = AccountDB(str(self.path))
        self.db.execute("DELETE FROM server_global_config WHERE key=?", (draw_rules.KEY,))
        DrawService._instance = None
        self.frames = []

        def pushed(uid, frames):
            self.assertEqual(uid, UID)
            self.frames.extend(frames)
            return {'clients': 1 if frames else 0, 'requires_relogin': False}

        self.push = patch('gm_actions.sync', side_effect=pushed).start()
        patch('gm_api.get_db', return_value=self.db).start()
        patch('gm_api.get_reader', side_effect=lambda: GMReader(str(self.path))).start()
        self.app = Flask(__name__)
        self.app.register_blueprint(gm_api.gm_bp, url_prefix='/api/gm')
        self.client = self.app.test_client()

    def tearDown(self):
        patch.stopall()
        self.db.close()
        self.folder.cleanup()

    def post(self, path, body):
        return self.client.post('/api/gm/' + path, json={'uid': UID, **body}).get_json()

    def scalar(self, sql, args=()):
        return next(iter(self.db.query(sql, args)[0].values()))

    def test_default_draw_curve_and_boundaries(self):
        for group in draw_rules.GROUPS:
            d = draw_rules.defaults(group)
            start, hard = (81, 90) if group == 'hero_precision_90' else (66, 70)
            for n in range(1, hard + 2):
                expected = 1.0 if n >= hard else .016 if n < start else .016 + (n - start + 1) * .984 / (hard - start + 1)
                self.assertAlmostEqual(DrawService.calc_ssr_prob(group, n), expected)
            self.assertEqual(DrawService.calc_sr_prob(group, 10), 1)
            self.assertEqual(d['cost_multiplier'], 1)

    def test_rules_persist_without_resetting_progress_or_other_group(self):
        service = DrawService.get_instance(self.db)
        service.set_pity(UID, 'hero_precision_70', 65, is_up_guaranteed=1)
        before = service.get_draw_state(UID, 'hero_precision_70')
        result = self.post('gacha/rules', {'group': 'hero_precision_70', 'rules': {'hard_pity': 20, 'soft_pity_start': 15, 'ssr_rate': .05}})
        self.assertEqual(result['code'], 0, result)
        self.assertEqual(service.get_draw_state(UID, 'hero_precision_70'), before)
        self.assertEqual(draw_rules.get_rules(self.db, 'hero_precision_90')['hard_pity'], 90)
        new_db = AccountDB(str(self.path))
        self.assertEqual(draw_rules.get_rules(new_db, 'hero_precision_70')['hard_pity'], 20)
        new_db.close()

    def test_invalid_rules_do_not_write(self):
        bad = [{'ssr_rate': 1.1}, {'ssr_rate': True}, {'hard_pity': 0}, {'soft_pity_start': 91},
               {'hard_pity': 20.5}, {'typo': 2}, {'cost_multiplier': -1}]
        for values in bad:
            result = self.post('gacha/rules', {'group': 'hero_precision_70', 'rules': values})
            self.assertNotEqual(result['code'], 0, (values, result))
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM server_global_config WHERE key=?', (draw_rules.KEY,)), 0)
        self.assertNotEqual(self.post('gacha/rules', {'group': 'bogus', 'rules': {'ssr_rate': .1}})['code'], 0)

    def test_draw_cost_decimal_rounding(self):
        rule = draw_rules.defaults('hero_precision_70')
        rule['cost_multiplier'] = 1.1
        self.assertEqual(draw_rules.draw_cost(100, rule), 110)
        rule['cost_multiplier'] = .15
        self.assertEqual(draw_rules.draw_cost(1, rule), 1)
        rule['cost_multiplier'] = 0
        self.assertEqual(draw_rules.draw_cost(10, rule), 0)

    def test_custom_pity_drives_actual_draw(self):
        service = DrawService.get_instance(self.db)
        group = service.get_pool_group(10001)
        draw_rules.save(self.db, group, {'ssr_rate': 0, 'hard_pity': 5, 'soft_pity_start': 5,
                                      'sr_rate': 0, 'sr_pity': 1000, 'cost_multiplier': 0})
        service.set_pity(UID, group, 0, since_sr=0)
        # History is intentionally bounded per series. Isolate these ten draws
        # from the seeded history so eviction does not affect the assertion.
        self.db.execute('DELETE FROM draw_record WHERE uid=?', (UID,))
        initial = 0
        ctx = gm_actions.context(self.db)
        with patch('draw_service.random.random', return_value=.999999), self.db.transaction():
            service.draw(ctx, UID, 10001, 10)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM draw_record WHERE uid=?', (UID,)), initial + 10)
        self.assertEqual(service.get_draw_state(UID, group)['since_ssr'], 0)
        from draw_service import STANDARD_S_HEROES
        records = self.db.query('SELECT item_id FROM draw_record WHERE uid=? ORDER BY id DESC LIMIT 10', (UID,))
        self.assertEqual(sum(r['item_id'] in STANDARD_S_HEROES for r in records), 2)

    def test_grant_is_scoped_and_returns_real_balance(self):
        before = self.scalar('SELECT num FROM currency WHERE uid=? AND id=1', (UID,))
        other = self.db.query('SELECT num FROM currency WHERE uid=? AND id=1', (OTHER,))
        result = self.post('inventory/add_item', {'item_id': 1, 'num': 7})
        self.assertEqual(result['code'], 0, result)
        self.assertEqual(result['data']['balance'], before + 7)
        self.assertEqual(self.db.query('SELECT num FROM currency WHERE uid=? AND id=1', (OTHER,)), other)
        packet = decode(next(frame.payload for frame in self.frames if frame.cmd == 17023), 'sc_17023')
        self.assertTrue(any(x['id'] == 1 and x['num'] == before + 7 for x in packet['normal_items']))

    def test_grant_failure_rolls_back_instead_of_creating_currency(self):
        before = self.scalar('SELECT num FROM currency WHERE uid=? AND id=1', (UID,))
        with patch('gm_actions.asset_frames', side_effect=ValueError('encoding failed')):
            result = self.post('inventory/add_item', {'item_id': 1, 'num': 7})
        self.assertNotEqual(result['code'], 0)
        self.assertEqual(self.scalar('SELECT num FROM currency WHERE uid=? AND id=1', (UID,)), before)
        self.assertNotEqual(self.post('inventory/add_item', {'item_id': 999999999, 'num': 1})['code'], 0)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM currency WHERE uid=? AND id=999999999', (UID,)), 0)

    def test_grant_numeric_validation(self):
        for count in (True, 1.5, 0, -1, 1000000000):
            self.assertNotEqual(self.post('inventory/add_item', {'item_id': 1, 'num': count})['code'], 0)

    def test_full_suit_creates_six_correct_slots(self):
        before = self.scalar('SELECT COUNT(*) FROM equip WHERE uid=?', (UID,))
        result = self.post('inventory/add_suit', {'suit_id': 1, 'star': 5, 'count': 2})
        self.assertEqual(result['code'], 0, result)
        self.assertEqual(result['data']['generated_count'], 12)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM equip WHERE uid=?', (UID,)), before + 12)
        ids = result['data']['instance_ids']
        self.assertEqual(len(set(ids)), 12)
        packet = decode(next(f.payload for f in self.frames if f.cmd == 17023), 'sc_17023')
        self.assertEqual(len(packet['equip_list']), 12)
        self.assertTrue(all(item.get('num') == 1 for item in packet['equip_list']))

    def test_cleanup_only_previewed_unprotected_instances(self):
        item = 310001
        for owner, eid, locked, exp in [(UID, 900001, 0, 0), (UID, 900002, 1, 0), (UID, 900003, 0, 100), (OTHER, 900001, 0, 0)]:
            self.db.execute('INSERT INTO equip(uid,id,prefab_id,exp,hero_id,is_lock,now_break_level,enchant_slots) VALUES(?,?,?,?,0,?,0,?)',
                            (owner, eid, item, exp, locked, json.dumps(default_slot_list())))
        body = {'category': 'equip', 'max_star': 3, 'instance_ids': [900001, 900002, 900003]}
        self.assertNotEqual(self.post('inventory/clear', body)['code'], 0)
        result = self.post('inventory/clear', {**body, 'confirm': True})
        self.assertEqual(result['code'], 0, result)
        self.assertEqual(result['data']['deleted_count'], 1)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM equip WHERE uid=? AND id IN (900002,900003)', (UID,)), 2)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM equip WHERE uid=? AND id=900001', (OTHER,)), 1)
        packet = decode(self.frames[0].payload, 'sc_17023')
        self.assertEqual(packet['equip_list'][0]['equip_id'], 900001)
        self.assertEqual(packet['equip_list'][0].get('num', 0), 0)

    def test_unlock_is_idempotent_and_preserves_other_hero_data(self):
        before = self.db.query('SELECT * FROM hero WHERE uid=? AND id=1066', (UID,))[0]
        pieces = self.db.query('SELECT * FROM hero_piece WHERE uid=? AND hero_id=1066', (UID,))
        result = self.post('heroes/unlock', {'hero_id': 1066})
        self.assertEqual(result['code'], 0, result)
        self.assertEqual(result['data']['unlocked_count'], 0)
        self.assertEqual(self.db.query('SELECT * FROM hero WHERE uid=? AND id=1066', (UID,))[0], before)
        self.assertEqual(self.db.query('SELECT * FROM hero_piece WHERE uid=? AND hero_id=1066', (UID,)), pieces)
        self.db.execute('DELETE FROM hero WHERE uid=? AND id=1066', (UID,))
        result = self.post('heroes/unlock', {'hero_id': 1066})
        self.assertEqual(result['code'], 0, result)
        self.assertEqual(result['data']['unlocked_count'], 1)
        self.assertTrue(any(frame.cmd == 14007 for frame in self.frames))

    def test_graduate_preserves_equipment_and_servant(self):
        old = self.db.query('SELECT equip_slot,weapon_servant_uid FROM hero WHERE uid=? AND id=1066', (UID,))[0]
        result = self.post('heroes/graduate', {'hero_id': 1066})
        self.assertEqual(result['code'], 0, result)
        row = self.db.query('SELECT * FROM hero WHERE uid=? AND id=1066', (UID,))[0]
        self.assertEqual((row['level'], row['star']), (80, 600))
        self.assertEqual(row['equip_slot'], old['equip_slot'])
        self.assertEqual(row['weapon_servant_uid'], old['weapon_servant_uid'])
        self.assertTrue(all(s[1] == 35 for s in json.loads(row['skill_list'])))

    def test_full_unlock_backfills_portraits_for_owned_heroes(self):
        from peripheral_service import PeripheralService
        gm_actions.unlock(self.db, UID, all_heroes=True)
        self.db.execute("DELETE FROM player_card WHERE uid=? AND kind='portrait'", (UID,))
        saved = {table: self.db.query(f'SELECT * FROM {table} WHERE uid=?', (UID,))
                 for table in ('hero', 'equip', 'servant')}
        other = self.db.query('SELECT * FROM player_card WHERE uid=?', (OTHER,))
        self.frames.clear()
        result = gm_actions.unlock(self.db, UID, all_heroes=True)
        valid = PeripheralService.get_valid_decorations('portraits')
        owned = {r['item_id'] for r in self.db.query("SELECT item_id FROM player_card WHERE uid=? AND kind='portrait' AND obtained=1", (UID,))}
        self.assertEqual(owned, valid)
        self.assertEqual(result['unlocked_count'], 0)
        self.assertEqual(result['unlocked_portrait_count'], len(valid))
        card = decode(next(frame.payload for frame in self.frames if frame.cmd == 32009), 'sc_32009')
        self.assertEqual({row['id'] for row in card['icon_list']}, valid)
        again = gm_actions.unlock(self.db, UID, all_heroes=True)
        self.assertEqual(again['unlocked_portrait_count'], 0)
        self.assertEqual({table: self.db.query(f'SELECT * FROM {table} WHERE uid=?', (UID,)) for table in saved}, saved)
        self.assertEqual(self.db.query('SELECT * FROM player_card WHERE uid=?', (OTHER,)), other)

    def test_single_unlock_backfills_portrait_without_changing_hero(self):
        self.db.execute("DELETE FROM player_card WHERE uid=? AND kind='portrait' AND item_id=2110661", (UID,))
        before = self.db.query('SELECT * FROM hero WHERE uid=? AND id=1066', (UID,))
        result = gm_actions.unlock(self.db, UID, hero_id=1066)
        self.assertEqual(result['unlocked_count'], 0)
        self.assertEqual(result['changed_portraits'], [2110661])
        self.assertEqual(self.db.query('SELECT * FROM hero WHERE uid=? AND id=1066', (UID,)), before)
        self.assertEqual(self.scalar("SELECT obtained FROM player_card WHERE uid=? AND kind='portrait' AND item_id=2110661", (UID,)), 1)

    def test_portrait_backfill_rolls_back_if_profile_encoding_fails(self):
        gm_actions.unlock(self.db, UID, all_heroes=True)
        self.db.execute("DELETE FROM player_card WHERE uid=? AND kind='portrait'", (UID,))
        with patch('generator.gen_payload', side_effect=ValueError('profile encoding failed')):
            with self.assertRaises(ValueError):
                gm_actions.unlock(self.db, UID, all_heroes=True)
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM player_card WHERE uid=? AND kind='portrait'", (UID,)), 0)

    def test_choice_pool_defaults_preserve_existing_state_and_config(self):
        self.db.execute('DELETE FROM draw_pool WHERE pool_id IN (5030601,5030301)')
        self.db.execute("INSERT INTO draw_state(uid,pool_id,since_ssr,total_draws,up_id,is_up_guaranteed,pool_group) VALUES(?,5030601,12,20,1044,0,'hero_precision_90') ON CONFLICT(uid,pool_id) DO UPDATE SET since_ssr=12,total_draws=20,up_id=1044", (UID,))
        saved = self.db.query('SELECT * FROM draw_state WHERE uid=? AND pool_id=5030601', (UID,))
        service = DrawService.get_instance(self.db)
        for pool, group in ((5030601, 'hero_precision_90'), (5030301, 'hero_precision_70')):
            rows = self.db.query('SELECT detail_json FROM draw_pool WHERE pool_id=?', (pool,))
            self.assertTrue(rows, pool)
            config = json.loads(rows[0]['detail_json'])
            self.assertEqual(service.get_pool_group(pool, config), group)
            self.assertEqual(config['cost_once'], [38, 1])
            self.assertEqual(config['cost_ten_times'], [38, 10])
        custom = json.dumps({'name': 'custom', 'cost_once': [38, 2]})
        self.db.execute('UPDATE draw_pool SET detail_json=? WHERE pool_id=5030601', (custom,))
        service._init_db_schema()
        self.assertEqual(self.db.query('SELECT detail_json FROM draw_pool WHERE pool_id=5030601')[0]['detail_json'], custom)
        migrated = self.db.query('SELECT * FROM draw_state WHERE uid=? AND pool_id=5030601', (UID,))
        self.assertEqual([{key: row[key] for key in saved[0]} for row in migrated], saved)
        self.assertEqual(migrated[0]['since_sr'], 0)

    def test_anchor_choice_draw_uses_selected_hero_and_charges_once(self):
        import core
        import operations
        from codec import encode
        service = DrawService.get_instance(self.db)
        service.set_pool_up(UID, 5030301, 1089)
        service.set_pool_up(UID, 5030601, 1044)
        other_group = service.get_draw_state(UID, 'hero_precision_70')
        draw_rules.save(self.db, 'hero_precision_90', {'ssr_rate': 0, 'hard_pity': 1, 'soft_pity_start': 1})
        service.set_pity(UID, 'hero_precision_90', 0)
        self.db.set_currency(UID, 38, 20)
        other = self.db.query('SELECT * FROM currency WHERE uid=?', (OTHER,))
        engine = core.Core(db=self.db, generator=gm_actions.generator, codec_encode=encode,
                           codec_decode=decode, log=lambda *a, **kw: None)
        response = engine.dispatch(core.CoreRequest(16010, encode('cs_16010', {'pool': 5030601, 'type': 1}), uid=UID))
        result = decode(next(f['payload'] for f in response.frames if f['sc'] == 16011), 'sc_16011')
        self.assertEqual(result['result'], 0, result)
        # The default account owns this S hero, so its drop is converted to shards.
        self.assertEqual(result['item'], [{'id': 11044, 'num': 30, 'convert_from': {'id': 1044, 'num': 1}}])
        self.assertEqual(service._item_balance(UID, 38), 19)
        self.assertEqual(service.get_pool_user_up(UID, 5030601), 1044)
        self.assertEqual(service.get_pool_user_up(UID, 5030301), 1089)
        self.assertEqual(service.get_draw_state(UID, 'hero_precision_70'), other_group)
        self.assertEqual(self.db.query('SELECT * FROM currency WHERE uid=?', (OTHER,)), other)

    def test_anchor_selection_and_login_keep_chosen_hero(self):
        import core
        import operations
        from codec import encode
        service = DrawService.get_instance(self.db)
        engine = core.Core(db=self.db, generator=gm_actions.generator, codec_encode=encode,
                           codec_decode=decode, log=lambda *a, **kw: None)
        response = engine.dispatch(core.CoreRequest(16016, encode('cs_16016', {'id': 5030601, 'up': 1044}), uid=UID))
        ack = decode(next(f['payload'] for f in response.frames if f['sc'] == 16017), 'sc_16017')
        self.assertEqual(ack['result'], 0, ack)
        self.assertEqual(service.get_pool_user_up(UID, 5030601), 1044)
        self.assertEqual(self.db.query('SELECT up_id FROM draw_state WHERE uid=? AND pool_id=5030601', (UID,))[0]['up_id'], 1044)
        packet = service.build_16015_payload(UID, res_version='311')
        self.assertEqual(next(p for p in packet['draw_info_list'] if p['id'] == 5030601)['up'], 1044)

    def test_inventory_query_includes_servants_and_fragments(self):
        result = self.client.get(f'/api/gm/inventory/items?uid={UID}').get_json()
        self.assertEqual(result['code'], 0, result)
        kinds = {item['type'] for item in result['data']['items']}
        self.assertTrue({'servant', 'hero_piece', 'equip'}.issubset(kinds))
        self.assertTrue(all('item_type' in item for item in result['data']['items']))

    def test_online_backup_includes_wal_changes(self):
        self.db._conn().execute('PRAGMA journal_mode=WAL')
        self.db.execute('UPDATE currency SET num=123456789 WHERE uid=? AND id=1', (UID,))
        with patch.object(gm_api, '__file__', str(self.path.with_name('gm_api.py'))):
            result = self.post('server/backup_db', {})
        self.assertEqual(result['code'], 0, result)
        backup = Path(result['data']['backup_path'])
        try:
            connection = sqlite3.connect(str(backup))
            self.assertEqual(connection.execute('SELECT num FROM currency WHERE uid=? AND id=1', (UID,)).fetchone()[0], 123456789)
            connection.close()
        finally:
            backup.unlink()

    def test_servant_grant_and_locked_cleanup(self):
        self.db.execute('UPDATE hero SET weapon_servant_uid=0 WHERE uid=?', (UID,))
        self.db.execute('DELETE FROM servant WHERE uid=?', (UID,))
        result = self.post('inventory/add_item', {'item_id': 2410001, 'num': 2})
        self.assertEqual(result['code'], 0, result)
        created = result['data']['changes']['servants']
        self.assertEqual(len(created), 2)
        packet = decode(self.frames[0].payload, 'sc_17023')
        self.assertEqual(len(packet['weapon_list']), 2)
        self.assertTrue(all(row['item']['num'] == 1 for row in packet['weapon_list']))
        self.db.execute('UPDATE servant SET is_locked=1 WHERE uid=? AND id=?', (UID, created[1]['id']))
        self.frames.clear()
        result = self.post('inventory/clear', {'category': 'servant', 'max_star': 4,
                           'instance_ids': [row['id'] for row in created], 'confirm': True})
        self.assertEqual(result['code'], 0, result)
        self.assertEqual(result['data']['deleted_count'], 1)
        self.assertEqual(self.scalar('SELECT COUNT(*) FROM servant WHERE uid=?', (UID,)), 1)

    def test_pity_false_string_and_dynamic_cap(self):
        draw_rules.save(self.db, 'hero_precision_70', {'hard_pity': 20, 'soft_pity_start': 15})
        result = self.post('gacha/set_pity', {'series': 'hero_precision_70', 'count': 19, 'is_up_guaranteed': 'false'})
        self.assertEqual(result['code'], 0, result)
        self.assertEqual(DrawService.get_instance(self.db).get_draw_state(UID, 'hero_precision_70')['is_up_guaranteed'], 0)
        self.assertNotEqual(self.post('gacha/set_pity', {'series': 'hero_precision_70', 'count': 21})['code'], 0)
        self.assertNotEqual(self.post('gacha/set_pity', {'series': 'hero_precision_70', 'count': 1.5})['code'], 0)
        before = DrawService.get_instance(self.db).get_draw_state(UID, 'hero_precision_70')
        result = self.post('gacha/rules', {'group': 'hero_precision_70', 'reset': True})
        self.assertEqual(result['code'], 0, result)
        self.assertEqual(result['data']['rules']['hard_pity'], 70)
        self.assertEqual(DrawService.get_instance(self.db).get_draw_state(UID, 'hero_precision_70'), before)

    def test_default_choice_pool_details_and_gm_rule_view(self):
        service = DrawService.get_instance(self.db)
        with patch.object(service, 'get_pool_user_up', return_value=0), patch.object(service, 'load_pool_up_cfg', return_value={}):
            details = service.get_pool_details(4080101, uid=UID)
            self.assertTrue(details['pool_details']['s_up_item'])
        draw_rules.save(self.db, 'hero_precision_70', {'hard_pity': 20, 'soft_pity_start': 15})
        result = self.client.get(f'/api/gm/gacha/pools?uid={UID}').get_json()
        self.assertEqual(result['code'], 0, result)
        self.assertEqual(result['data']['pity_states']['hero_precision_70']['pity_cap'], 20)

    def test_sync_skips_uninitialized_and_other_users(self):
        from unittest.mock import Mock
        from middleware import DownFrame
        import server_net
        ready = Mock(initialized=True)
        ready.send_push.return_value = True
        pending = Mock(initialized=False)
        other = Mock(initialized=True)
        failed = Mock(initialized=True)
        failed.send_push.return_value = False
        with patch.object(server_net, '_online_game_conns', {UID: {ready, pending, failed}, OTHER: {other}}):
            self.assertEqual(server_net.push_gm_frames(UID, [DownFrame(17023, b'test')]), 1)
        pending.send_push.assert_not_called()
        other.send_push.assert_not_called()


if __name__ == '__main__':
    unittest.main()
