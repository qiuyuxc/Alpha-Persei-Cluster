"""Actual guild settings and presence fields on isolated player saves."""
import unittest
from unittest.mock import patch

import test_family_flows as family_fixtures
import social_service as social
import social_protocol as protocol
from codec import decode

UID, OTHER, GID, NOW = (family_fixtures.UID, family_fixtures.OTHER,
                        family_fixtures.GID, family_fixtures.NOW)


class FamilyPresencePolicyTests(unittest.TestCase):
    def setUp(self):
        family_fixtures.FamilyFlowTests.setUp(self)
        self.db.update_user_basic(OTHER, level=30)
    social_request = family_fixtures.FamilyFlowTests.social_request
    join = family_fixtures.FamilyFlowTests.join
    pushed_to = family_fixtures.FamilyFlowTests.pushed_to

    def setting(self, policy):
        return self.social_request(31018, {'setting': {
            'examine_recruit_join_request_switch': policy, 'join_request_limit': 30}})

    def test_offline_member_uses_recent_logout_instead_of_join_time(self):
        self.join()
        self.db.execute('UPDATE social_members SET joined_at=? WHERE uid=?', (NOW - 3 * social.DAY, UID))
        self.db.update_timer_watermark(UID, last_heartbeat_ts=NOW - 7300, last_logout_ts=NOW - 7200)
        with patch('server_net.get_online_uids', return_value=[]):
            ack, _ = self.social_request(31004)
        row = next(m for m in ack['members'] if m['id'] == UID)
        self.assertEqual(row['online_info'], NOW - 7200)

    def test_offline_member_uses_heartbeat_when_logout_was_not_recorded(self):
        self.join()
        self.db.execute('UPDATE social_members SET joined_at=? WHERE uid=?', (NOW - 3 * social.DAY, UID))
        self.db.update_timer_watermark(UID, last_heartbeat_ts=NOW - 3600, last_logout_ts=0, is_online=1)
        with patch('server_net.get_online_uids', return_value=[]):
            row = social.member_wire(self.db, social.membership(self.db, UID), NOW)
        self.assertEqual(row['online_info'], NOW - 3600)

    def test_active_connection_overrides_persisted_offline_flag(self):
        self.join()
        self.db.update_timer_watermark(UID, is_online=0, last_logout_ts=NOW - 3600)
        row = social.member_wire(self.db, social.membership(self.db, UID), NOW)
        self.assertEqual(row['online_info'], 0)

    def test_missing_presence_falls_back_to_join_time_without_creating_watermark(self):
        self.join()
        self.db.execute('DELETE FROM user_timer_watermark WHERE uid=?', (UID,))
        with patch('server_net.get_online_uids', return_value=[]):
            row = social.member_wire(self.db, social.membership(self.db, UID), NOW)
        self.assertEqual(row['online_info'], NOW)
        self.assertFalse(self.db.query('SELECT 1 FROM user_timer_watermark WHERE uid=?', (UID,)))

    def test_future_presence_is_clamped_after_clock_rollback(self):
        self.join()
        self.db.update_timer_watermark(UID, last_heartbeat_ts=NOW + 300, last_logout_ts=NOW + 600)
        with patch('server_net.get_online_uids', return_value=[]):
            row = social.member_wire(self.db, social.membership(self.db, UID), NOW)
        self.assertEqual(row['online_info'], NOW)

    def test_native_review_policy_saves_and_queues_application(self):
        self.join()
        ack, _ = self.setting(2)
        self.assertEqual(ack['result'], 0)
        self.assertEqual(social.guild_wire(self.db, GID, UID, NOW)['club_setting']['examine_recruit_join_request_switch'], 2)
        self.join(OTHER)
        self.assertIsNone(social.membership(self.db, OTHER))
        self.assertTrue(self.db.query('SELECT 1 FROM social_requests WHERE guild_id=? AND uid=?', (GID, OTHER)))

    def test_native_closed_policy_blocks_application(self):
        self.join()
        ack, _ = self.setting(3)
        self.assertEqual(ack['result'], 0)
        ack, _ = self.social_request(31010, {'id': GID}, OTHER)
        self.assertNotEqual(ack['result'], 0)
        self.assertIsNone(social.membership(self.db, OTHER))
        self.assertFalse(self.db.query('SELECT 1 FROM social_requests WHERE uid=?', (OTHER,)))

    def test_closed_policy_blocks_approval_of_an_existing_request(self):
        self.join()
        self.setting(2)
        self.join(OTHER)
        self.setting(3)
        ack, _ = self.social_request(31032, {'id': OTHER, 'type': 1})
        self.assertNotEqual(ack['result'], 0)
        self.assertIsNone(social.membership(self.db, OTHER))
        self.assertTrue(self.db.query('SELECT 1 FROM social_requests WHERE uid=?', (OTHER,)))

    def test_free_policy_restores_direct_join(self):
        self.join()
        self.setting(3)
        ack, _ = self.setting(1)
        self.assertEqual(ack['result'], 0)
        self.join(OTHER)
        self.assertEqual(social.membership(self.db, OTHER)['guild_id'], GID)

    def test_review_policy_keeps_minimum_level_requirement(self):
        self.join()
        self.setting(2)
        self.db.update_user_basic(OTHER, level=29)
        ack, _ = self.social_request(31010, {'id': GID}, OTHER)
        self.assertNotEqual(ack['result'], 0)
        self.assertFalse(self.db.query('SELECT 1 FROM social_requests WHERE uid=?', (OTHER,)))

    def test_offline_application_approval_survives_relogin(self):
        self.join()
        self.setting(2)
        self.join(OTHER)
        with patch('server_net.get_online_uids', return_value=[UID]), patch('server_net.push_gm_frames', return_value=0):
            ack, _ = self.social_request(31032, {'id': OTHER, 'type': 1})
        self.assertEqual(ack['result'], 0)
        self.assertEqual(self.db.get('users', OTHER)['club_id'], GID)
        own = decode(protocol.own_guild_frame(self.db, OTHER, NOW).payload, 'sc_31001')
        self.assertEqual(own['club']['id'], GID)
        self.assertEqual(own['club']['post'], 3)

    def test_legacy_zero_policy_is_read_as_review_and_preserves_requests(self):
        self.join()
        self.db.execute('UPDATE social_guilds SET examine_switch=0 WHERE id=?', (GID,))
        info = social.guild_wire(self.db, GID, UID, NOW)
        self.assertEqual(info['club_setting']['examine_recruit_join_request_switch'], 2)
        self.join(OTHER)
        self.assertIsNone(social.membership(self.db, OTHER))

    def test_invalid_policy_does_not_change_saved_settings(self):
        self.join()
        for policy in (0, 4):
            with self.subTest(policy=policy):
                ack, _ = self.setting(policy)
                self.assertNotEqual(ack['result'], 0)
                self.assertEqual(self.db.query('SELECT examine_switch FROM social_guilds WHERE id=?', (GID,))[0]['examine_switch'], 1)

    def test_required_snapshot_failure_rolls_back_policy(self):
        self.join()
        original = protocol.encode
        with patch.object(protocol, 'encode', side_effect=lambda name, data: None if name == 'sc_31001' else original(name, data)):
            ack, _ = self.setting(2)
        self.assertNotEqual(ack['result'], 0)
        self.assertEqual(self.db.query('SELECT examine_switch FROM social_guilds WHERE id=?', (GID,))[0]['examine_switch'], 1)


if __name__ == '__main__':
    unittest.main()
