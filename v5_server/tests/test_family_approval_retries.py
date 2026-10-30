"""Native request removals and approval retries on isolated player saves."""
import unittest
from unittest.mock import patch

import test_family_flows as fixtures
from codec import decode
from core import CoreRequest
import social_protocol as protocol
import social_service as social

UID, THIRD, GID, NOW = fixtures.UID, fixtures.THIRD, fixtures.GID, fixtures.NOW
TARGET = 1422042220


class FamilyApprovalRetryTests(unittest.TestCase):
    def setUp(self):
        fixtures.FamilyFlowTests.setUp(self)
        self.db.execute('INSERT OR IGNORE INTO users(uid,nick,level) VALUES(?,\'审批测试\',30)', (TARGET,))
        self.db.update_user_basic(TARGET,level=30)
    social_request = fixtures.FamilyFlowTests.social_request
    join = fixtures.FamilyFlowTests.join
    pushed_to = fixtures.FamilyFlowTests.pushed_to

    def queue(self):
        self.join()
        self.social_request(31018,{'setting':{'examine_recruit_join_request_switch':2,'join_request_limit':30}})
        self.join(TARGET)
        self.assertIsNone(social.membership(self.db,TARGET))

    def decide(self, action=1, actor=UID):
        return self.social_request(31032,{'id':TARGET,'type':action},actor)

    def state(self):
        return {name:self.db.query('SELECT * FROM '+name) for name in
                ('users','task','social_members','social_requests','social_player_contributions',
                 'social_prepose_daily','social_prepose_runs')}

    def removals(self, frames):
        return [body for cmd,body in frames if cmd==31999]

    def test_first_approval_sends_native_type_two_request_removal(self):
        self.queue()
        ack,frames=self.decide()
        self.assertEqual(ack['result'],0)
        self.assertEqual(self.removals(frames),[{'id':TARGET,'type':2}])
        self.assertEqual(social.membership(self.db,TARGET)['guild_id'],GID)
        self.assertFalse(self.db.query('SELECT 1 FROM social_requests WHERE uid=?',(TARGET,)))

    def test_first_refusal_sends_native_type_three_request_removal(self):
        self.queue()
        ack,frames=self.decide(action=2)
        self.assertEqual(ack['result'],0)
        self.assertEqual(self.removals(frames),[{'id':TARGET,'type':3}])
        self.assertIsNone(social.membership(self.db,TARGET))

    def test_removal_reaches_other_online_managers(self):
        self.join()
        self.join(THIRD)
        self.social_request(31016,{'id':THIRD,'post':2})
        self.social_request(31018,{'setting':{'examine_recruit_join_request_switch':2,'join_request_limit':30}})
        self.join(TARGET)
        self.push.reset_mock()
        self.assertEqual(self.decide()[0]['result'],0)
        self.assertEqual(self.removals(self.pushed_to(THIRD)),[{'id':TARGET,'type':2}])

    def test_repeated_approval_succeeds_without_new_member_event_or_writes(self):
        self.queue()
        self.assertEqual(self.decide()[0]['result'],0)
        before=self.state()
        self.push.reset_mock()
        ack,frames=self.decide()
        self.assertEqual(ack['result'],0)
        self.assertEqual(self.removals(frames),[{'id':TARGET,'type':2}])
        self.assertFalse(any(cmd==31985 for cmd,_ in frames))
        self.assertEqual(self.state(),before)
        self.assertEqual(next(body for cmd,body in frames if cmd==31001)['club']['member_count'],8)

    def test_retry_after_closed_setting_does_not_join_or_change_existing_member(self):
        self.queue()
        self.decide()
        self.social_request(31018,{'setting':{'examine_recruit_join_request_switch':3,'join_request_limit':30}})
        before=self.state()
        self.assertEqual(self.decide()[0]['result'],0)
        self.assertEqual(self.state(),before)

    def test_regular_member_cannot_approve_by_retrying_an_existing_member(self):
        self.queue()
        self.decide()
        ack,_=self.decide(actor=TARGET)
        self.assertNotEqual(ack['result'],0)

    def test_other_guild_manager_cannot_approve_existing_member(self):
        self.queue()
        self.decide()
        social.create_guild(self.db,THIRD,'其他审批公会',1,'测试',NOW)
        before=self.state()
        self.assertNotEqual(self.decide(actor=THIRD)[0]['result'],0)
        self.assertEqual(self.state(),before)

    def test_missing_request_cannot_admit_a_nonmember(self):
        self.join()
        before=self.state()
        self.assertNotEqual(self.decide()[0]['result'],0)
        self.assertIsNone(social.membership(self.db,TARGET))
        self.assertEqual(self.state(),before)

    def test_retry_after_target_leaves_does_not_recreate_membership(self):
        self.queue()
        self.decide()
        social.leave_guild(self.db,TARGET,GID)
        before=self.state()
        self.assertNotEqual(self.decide()[0]['result'],0)
        self.assertEqual(self.state(),before)

    def test_stale_request_for_existing_member_is_removed_without_resetting_tasks(self):
        self.queue()
        self.decide()
        self.db.execute('UPDATE task SET progress=50 WHERE uid=? AND task_id=120004',(TARGET,))
        self.db.claim_task(TARGET,120004,NOW)
        self.db.execute('INSERT INTO social_requests VALUES(?,?,?)',(GID,TARGET,NOW))
        member_before=social.membership(self.db,TARGET)
        tasks_before=self.db.query('SELECT * FROM task WHERE uid=?',(TARGET,))
        vitality_before=social.member_wire(self.db,member_before,NOW)['total_vitality']
        ack,frames=self.decide()
        self.assertEqual(ack['result'],0)
        self.assertFalse(any(cmd==31985 for cmd,_ in frames))
        self.assertFalse(self.db.query('SELECT 1 FROM social_requests WHERE uid=?',(TARGET,)))
        self.assertEqual(social.membership(self.db,TARGET),member_before)
        self.assertEqual(self.db.query('SELECT * FROM task WHERE uid=?',(TARGET,)),tasks_before)
        self.assertEqual(social.member_wire(self.db,member_before,NOW)['total_vitality'],vitality_before)

    def test_removal_encoding_failure_rolls_back_approval(self):
        self.queue()
        before=self.state()
        original=protocol.encode
        with patch.object(protocol,'encode',side_effect=lambda name,body:
                          None if name=='sc_31999' else original(name,body)):
            ack,_=self.decide()
        self.assertNotEqual(ack['result'],0)
        self.assertEqual(self.state(),before)
        self.assertEqual(self.decide()[0]['result'],0)

    def test_removal_encoding_failure_keeps_refused_request_pending(self):
        self.queue()
        before=self.state()
        original=protocol.encode
        with patch.object(protocol,'encode',side_effect=lambda name,body:
                          None if name=='sc_31999' else original(name,body)):
            ack,_=self.decide(action=2)
        self.assertNotEqual(ack['result'],0)
        self.assertEqual(self.state(),before)

    def test_processing_one_request_keeps_other_applications(self):
        self.queue()
        self.join(THIRD)
        self.assertEqual(self.decide()[0]['result'],0)
        rows=decode(protocol.request_frame(self.db,UID).payload,'sc_31979')
        self.assertEqual([r['id'] for r in rows['join_list']],[THIRD])

    def test_literal_client_approval_retry_uses_production_dispatch(self):
        self.queue()
        self.decide()
        # Native GuildAgreeRequest sends id=1422042220, type=1 (p31 fields 1/2).
        request=CoreRequest(31032,bytes.fromhex('08ecc88aa6051001'),uid=UID)
        reply=self.core.dispatch(request)
        ack=decode(next(f['payload'] for f in reply.frames if f['sc']==31033),'sc_31033')
        self.assertEqual(ack['result'],0)
        removal=decode(next(f['payload'] for f in reply.frames if f['sc']==31999),'sc_31999')
        self.assertEqual(removal,{'id':TARGET,'type':2})

    def test_creating_guild_removes_old_application_from_manager_cache(self):
        self.queue()
        self.push.reset_mock()
        ack,_=self.social_request(31008,{'name':'新建审批测试','icon':1,'notice':'测试'},TARGET)
        self.assertEqual(ack['result'],0)
        self.assertEqual(self.removals(self.pushed_to(UID)),[{'id':TARGET,'type':3}])

    def test_direct_join_removes_previous_other_guild_application(self):
        self.queue()
        gid=social.create_guild(self.db,THIRD,'另一个申请',1,'测试',NOW)
        self.push.reset_mock()
        self.join(TARGET,gid)
        self.assertEqual(self.removals(self.pushed_to(UID)),[{'id':TARGET,'type':3}])
        self.assertEqual(social.membership(self.db,TARGET)['guild_id'],gid)

    def second_application(self):
        gid=social.create_guild(self.db,THIRD,'第二个申请',1,'测试',NOW)
        self.db.execute('UPDATE social_guilds SET examine_switch=2 WHERE id=?',(gid,))
        self.join(TARGET,gid)
        return gid

    def test_approval_removes_other_pending_guild_applications(self):
        self.queue()
        self.second_application()
        self.push.reset_mock()
        self.assertEqual(self.decide()[0]['result'],0)
        other=self.pushed_to(THIRD)
        self.assertEqual(self.removals(other),[{'id':TARGET,'type':3}])
        self.assertFalse(any(cmd==31985 for cmd,_ in other))
        self.assertFalse(self.db.query('SELECT 1 FROM social_requests WHERE uid=?',(TARGET,)))

    def test_refusal_keeps_other_guild_application_pending(self):
        self.queue()
        gid=self.second_application()
        self.push.reset_mock()
        self.assertEqual(self.decide(action=2)[0]['result'],0)
        self.assertTrue(self.db.query('SELECT 1 FROM social_requests WHERE guild_id=? AND uid=?',(gid,TARGET)))
        self.assertFalse(self.pushed_to(THIRD))

    def test_cancel_notification_failure_rolls_back_new_guild(self):
        self.queue()
        before=self.state()
        original=protocol.encode
        with patch.object(protocol,'encode',side_effect=lambda name,body:
                          None if name=='sc_31999' else original(name,body)):
            ack,_=self.social_request(31008,{'name':'取消回滚测试','icon':1,'notice':'测试'},TARGET)
        self.assertNotEqual(ack['result'],0)
        self.assertEqual(self.state(),before)


if __name__=='__main__':
    unittest.main()
