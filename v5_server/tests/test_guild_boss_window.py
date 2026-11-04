"""A temporary Boss opening must agree with client time and expire by week."""
import datetime
import os
import unittest
from unittest.mock import patch

import guild_cycle


WEEK = int(datetime.datetime(2026, 11, 2, 5).timestamp())
OPEN = WEEK + 2 * guild_cycle.DAY + 4 * 3600
NEXT_WEEK = WEEK + 7 * guild_cycle.DAY


class GuildBossWindowTests(unittest.TestCase):
    def test_early_opening_switches_client_activity_and_server_phase_together(self):
        with patch.dict(os.environ, {guild_cycle.TEST_START_ENV: str(OPEN)}):
            for now, expected, activity in ((OPEN - 1, 'prepare', 102),
                                            (OPEN, 'challenge', 103),
                                            (NEXT_WEEK - 1, 'challenge', 103)):
                with self.subTest(now=now):
                    self.assertEqual(guild_cycle.phase(now), expected)
                    active = [row for row in guild_cycle.activities(now)[:2]
                              if row['start_time'] <= now < row['stop_time']]
                    self.assertEqual([row['activity_id'] for row in active], [activity])
                    self.assertGreater(active[0]['stop_time'], now)
                    self.assertEqual(guild_cycle.window(now)[0], WEEK)
                    self.assertEqual(guild_cycle.window(now)[2], NEXT_WEEK)

    def test_retained_setting_does_not_change_previous_or_next_week(self):
        with patch.dict(os.environ, {guild_cycle.TEST_START_ENV: str(OPEN)}):
            for week in (WEEK - 7 * guild_cycle.DAY, NEXT_WEEK,
                         NEXT_WEEK + 7 * guild_cycle.DAY):
                with self.subTest(week=week):
                    self.assertEqual(guild_cycle.window(week),
                                     (week, week + 5 * guild_cycle.DAY,
                                      week + 7 * guild_cycle.DAY))
                    self.assertEqual(guild_cycle.phase(week), 'prepare')
                    self.assertEqual(guild_cycle.phase(week + 5 * guild_cycle.DAY),
                                     'challenge')

    def test_invalid_or_inapplicable_setting_keeps_normal_schedule(self):
        for value in ('', 'bad', '1793755200.5', '0', '-1', str(WEEK),
                      str(WEEK + 5 * guild_cycle.DAY), str(NEXT_WEEK + 1)):
            with self.subTest(value=value):
                with patch.dict(os.environ, {guild_cycle.TEST_START_ENV: value}):
                    self.assertEqual(guild_cycle.window(OPEN),
                                     (WEEK, WEEK + 5 * guild_cycle.DAY, NEXT_WEEK))
        with patch.dict(os.environ):
            os.environ.pop(guild_cycle.TEST_START_ENV, None)
            self.assertEqual(guild_cycle.phase(OPEN), 'prepare')

    def test_npc_boss_damage_starts_at_opening_without_preparation_catch_up(self):
        guild = {'created_at': WEEK - guild_cycle.DAY,
                 'boss_started_at': WEEK - guild_cycle.DAY}
        with patch.dict(os.environ, {guild_cycle.TEST_START_ENV: str(OPEN)}):
            self.assertEqual(guild_cycle.boss_target(guild, 400000, 48, OPEN - 1), 0)
            self.assertEqual(guild_cycle.boss_target(guild, 400000, 48, OPEN), 0)
            self.assertEqual(guild_cycle.boss_target(guild, 400000, 48,
                                                   OPEN + guild_cycle.DAY), 200000)
            self.assertEqual(guild_cycle.boss_target(guild, 400000, 48,
                                                   OPEN + 2 * guild_cycle.DAY), 400000)
            self.assertEqual(guild_cycle.boss_target(guild, 400000, 48,
                                                   NEXT_WEEK + guild_cycle.DAY), 0)


if __name__ == '__main__':
    unittest.main()
