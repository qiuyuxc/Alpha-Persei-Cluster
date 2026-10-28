"""A31/A33/A34 regression cases using real packets and isolated player saves."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

SEED = Path(os.environ['GM_TEST_DB_SEED']).resolve()
SANDBOX = tempfile.TemporaryDirectory(prefix='ag-hero-growth-tests-')
BOOTSTRAP = Path(SANDBOX.name) / 'bootstrap.db'
shutil.copyfile(SEED, BOOTSTRAP)
os.environ['ACCOUNT_DB_PATH'] = str(BOOTSTRAP)

from account_db import AccountDB
from codec import decode, encode
from core import Core
import event_bus
from gm_actions import context
from hero_service import HeroService
from middleware import CSRequest
from operations import OneClickHeroUpOp, SkillUpOp, SkillElementOp

UID = 2174928301
OTHER = 264084870
HERO = 1011
SKILLS = [1011101, 1011201, 1011202, 1011203, 1011209, 1011305]
EXP_VALUES = {40101: 500, 40102: 1000, 40103: 5000, 40104: 10000}


class HeroGrowthTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(dir=SANDBOX.name)
        self.addCleanup(self.folder.cleanup)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(HeroService, '_instance', None))
        self.stack.enter_context(patch.object(event_bus.EventBus, '_handlers', {}))
        self.events = self.stack.enter_context(patch('event_bus.bus.emit'))
        self.path = Path(self.folder.name) / 'player.db'
        shutil.copyfile(SEED, self.path)
        self.db = AccountDB(str(self.path))
        self.addCleanup(self.db.close)
        self.ctx = context(self.db)
        self.ctx.uid = UID
        self.ctx.log = lambda *args, **kwargs: None
        self.engine = Core()
        self.curve = {row['level']: row['hero_exp_need']
                      for row in self.db.query('SELECT level,hero_exp_need FROM game_level_setting')}
        self.db.set_currency(UID, 2, 1000000)
        for item in (*EXP_VALUES, 40301, 40302, *range(40411, 40466)):
            self.db.set_material(UID, item, 10000)
        self.set_hero()
        self.other_state = self.state(OTHER)

    def exp_at(self, level):
        return sum(self.curve[n] for n in range(1, level))

    def set_hero(self, *, level=10, exp=None, break_level=0, skill_level=1, attr_level=0):
        self.db.execute('''UPDATE hero SET level=?,exp=?,break_level=?,skill_list=?,
            skill_intensify=? WHERE uid=? AND id=?''',
            (level, self.exp_at(level) if exp is None else exp, break_level,
             json.dumps([[sid, skill_level] for sid in SKILLS]),
             json.dumps([[1, attr_level]]) if attr_level else '[]', UID, HERO))

    def state(self, uid=UID):
        return {table: self.db.query('SELECT * FROM ' + table + ' WHERE uid=? ORDER BY id', (uid,))
                for table in ('hero', 'currency', 'material')}

    def balance(self, item):
        table = 'currency' if item == 2 else 'material'
        return self.db.query('SELECT num FROM ' + table + ' WHERE uid=? AND id=?', (UID, item))[0]['num']

    def hero(self):
        return self.db.query('SELECT * FROM hero WHERE uid=? AND id=?', (UID, HERO))[0]

    def request(self, operation, data):
        request = CSRequest(operation.cmd, encode('cs_' + str(operation.cmd), data), uid=UID)
        response = self.engine._run_operation(operation(), request, self.ctx)
        replies = [frame for frame in response.frames if frame['sc'] == operation.sc]
        self.assertEqual(len(replies), 1)
        self.assertEqual(self.state(OTHER), self.other_state)
        return decode(replies[0]['payload'], 'sc_' + str(operation.sc))

    def rejected(self, operation, data):
        before = self.state()
        self.ctx.touched_items.clear()
        self.events.reset_mock()
        result = self.request(operation, data)
        self.assertEqual(result['result'], 2, result)
        self.assertEqual(self.state(), before)
        self.assertFalse(self.ctx.touched_items)
        self.events.assert_not_called()

    def test_lower_target_rejected_before_break_costs_or_old_exp_refunds(self):
        self.set_hero(exp=self.exp_at(10) + 123)
        self.rejected(OneClickHeroUpOp, {'hero_id': HERO, 'level': 5, 'break_list': [1]})

    def test_same_level_target_cannot_refund_existing_partial_exp(self):
        self.set_hero(exp=self.exp_at(10) + 123)
        self.rejected(OneClickHeroUpOp, {'hero_id': HERO, 'level': 10})

    def test_old_save_above_stage_cap_cannot_be_downgraded(self):
        self.set_hero(level=30, break_level=0)
        self.rejected(OneClickHeroUpOp, {'hero_id': HERO, 'level': 0})

    def test_old_save_with_inconsistent_exp_cannot_be_downgraded(self):
        self.set_hero(level=10, exp=0)
        self.rejected(OneClickHeroUpOp, {
            'hero_id': HERO, 'level': 15, 'item_list': [{'id': 40101, 'num': 1}]})

    def test_higher_target_refunds_only_new_exp_and_persists_after_reopen(self):
        old_exp = self.exp_at(10) + 123
        self.set_hero(exp=old_exp)
        before = {item: self.balance(item) for item in EXP_VALUES}
        result = self.request(OneClickHeroUpOp, {
            'hero_id': HERO, 'level': 15, 'item_list': [{'id': 40104, 'num': 4}]})
        self.assertEqual(result['result'], 0)
        self.assertEqual((self.hero()['level'], self.hero()['exp']), (15, self.exp_at(15)))
        refund = {item['id']: item['num'] for item in result['item_list']}
        refunded_exp = sum(EXP_VALUES[item] * num for item, num in refund.items())
        excess = old_exp + 40000 - self.exp_at(15)
        self.assertGreater(refunded_exp, 0)
        self.assertLessEqual(refunded_exp, excess)
        self.assertLess(excess - refunded_exp, 500)
        for item in EXP_VALUES:
            self.assertEqual(self.balance(item), before[item] - (4 if item == 40104 else 0)
                             + refund.get(item, 0))
        expected = self.state()
        self.db.close()
        self.db = AccountDB(str(self.path))
        self.addCleanup(self.db.close)
        self.assertEqual(self.state(), expected)

    def test_missing_exp_material_rolls_back_prior_break_costs(self):
        self.set_hero(level=20)
        self.db.set_material(UID, 40101, 0)
        before = self.state()
        result = self.request(OneClickHeroUpOp, {
            'hero_id': HERO, 'level': 30, 'break_list': [1],
            'item_list': [{'id': 40101, 'num': 1}]})
        self.assertEqual(result['result'], 3)
        self.assertEqual(self.state(), before)
        self.assertFalse(self.ctx.touched_items)

    def test_skill_upgrade_beyond_cap_is_rejected_without_debit(self):
        self.set_hero(skill_level=34)
        self.rejected(SkillUpOp, {'hero_id': HERO, 'skill_id': SKILLS[0], 'num': 2})

    def test_zero_upgrade_count_is_rejected_by_both_protocols(self):
        for operation, fields in ((SkillUpOp, {'skill_id': SKILLS[0]}),
                                  (SkillElementOp, {'index': 1})):
            with self.subTest(operation=operation.cmd):
                self.rejected(operation, {'hero_id': HERO, 'num': 0, **fields})

    def test_skill_upgrade_exactly_to_cap_matches_ack_and_cost(self):
        self.set_hero(skill_level=34)
        before = self.balance(40301)
        result = self.request(SkillUpOp, {'hero_id': HERO, 'skill_id': SKILLS[0], 'num': 1})
        self.assertEqual(result['result'], 0)
        self.assertEqual(json.loads(self.hero()['skill_list'])[0], [SKILLS[0], 35])
        self.assertEqual(self.balance(40301), before - 100)
        self.rejected(SkillUpOp, {'hero_id': HERO, 'skill_id': SKILLS[0], 'num': 1})

    def test_attribute_upgrade_beyond_cap_is_rejected_without_debit(self):
        self.set_hero(attr_level=14)
        self.rejected(SkillElementOp, {'hero_id': HERO, 'index': 1, 'num': 2})

    def test_attribute_upgrade_exactly_to_cap_matches_ack_and_cost(self):
        self.set_hero(attr_level=14)
        before = {item: self.balance(item) for item in (40301, 40302)}
        result = self.request(SkillElementOp, {'hero_id': HERO, 'index': 1, 'num': 1})
        self.assertEqual(result['result'], 0)
        self.assertEqual(json.loads(self.hero()['skill_intensify']), [[1, 15]])
        self.assertEqual(self.balance(40301), before[40301] - 250)
        self.assertEqual(self.balance(40302), before[40302] - 38)

    def test_attribute_material_shortage_rolls_back_earlier_debits(self):
        self.set_hero(attr_level=13)
        self.db.set_material(UID, 40301, 479)
        before = self.state()
        result = self.request(SkillElementOp, {'hero_id': HERO, 'index': 1, 'num': 2})
        self.assertEqual(result['result'], 3)
        self.assertEqual(self.state(), before)
        self.assertFalse(self.ctx.touched_items)

    def test_unknown_and_other_hero_skills_cannot_be_written(self):
        for sid in (999999999, 1012101):
            with self.subTest(skill=sid):
                self.rejected(SkillUpOp, {'hero_id': HERO, 'skill_id': sid, 'num': 1})

    def test_foreign_skill_in_legacy_list_cannot_be_upgraded(self):
        self.db.execute('UPDATE hero SET skill_list=? WHERE uid=? AND id=?',
                        (json.dumps([[sid, 1] for sid in SKILLS] + [[999999999, 1]]), UID, HERO))
        self.rejected(SkillUpOp, {'hero_id': HERO, 'skill_id': 999999999, 'num': 1})

    def test_configured_skill_missing_from_new_hero_uses_base_level_and_correct_slot(self):
        self.db.execute("UPDATE hero SET skill_list='[]' WHERE uid=? AND id=?", (UID, HERO))
        before = self.balance(40301)
        result = self.request(SkillUpOp, {'hero_id': HERO, 'skill_id': SKILLS[1], 'num': 2})
        self.assertEqual(result['result'], 0)
        self.assertEqual(json.loads(self.hero()['skill_list']), [[SKILLS[1], 3]])
        self.assertEqual(self.balance(40301), before - 3)

    def test_reordered_skill_list_uses_configured_cost_slot(self):
        self.db.execute('UPDATE hero SET skill_list=? WHERE uid=? AND id=?',
                        (json.dumps([[SKILLS[1], 2], [SKILLS[0], 2]]), UID, HERO))
        before = self.balance(40301)
        result = self.request(SkillUpOp, {'hero_id': HERO, 'skill_id': SKILLS[1], 'num': 1})
        self.assertEqual(result['result'], 0)
        self.assertEqual(self.balance(40301), before - 2)

    def test_missing_skill_configuration_rejects_instead_of_granting_cheap_skills(self):
        with patch('hero_codec._get_hero_skills_cfg', return_value={}):
            self.rejected(SkillUpOp, {'hero_id': HERO, 'skill_id': SKILLS[0], 'num': 1})

    def test_required_skill_ack_failure_rolls_back_levels_and_cost(self):
        before = self.state()
        real_encode = self.ctx.codec_encode
        self.ctx.codec_encode = lambda name, data: b'' if name == 'sc_14031' else real_encode(name, data)
        result = self.request(SkillUpOp, {'hero_id': HERO, 'skill_id': SKILLS[0], 'num': 1})
        self.assertNotEqual(result['result'], 0)
        self.assertEqual(self.state(), before)
        self.assertFalse(self.ctx.touched_items)


if __name__ == '__main__':
    unittest.main()
