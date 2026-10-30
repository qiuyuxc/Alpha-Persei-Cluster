"""Native guild-task rewards and rift wins on isolated real-player saves."""
import unittest
from unittest.mock import patch

import test_family_flows as fixtures
from account_db import AccountDB
from codec import decode, encode
from core import CoreRequest
from inventory_service import InventoryService
from middleware import CSRequest
import guild_contributions as contributions
import operations
import social_protocol as protocol
import social_service as social

UID, OTHER, GID, NOW = fixtures.UID, fixtures.OTHER, fixtures.GID, fixtures.NOW


class FamilyTaskContributionTests(unittest.TestCase):
    def setUp(self):
        fixtures.FamilyFlowTests.setUp(self)
        self.db.execute('UPDATE task SET progress=0,complete_flag=0,claimed_ts=0 WHERE uid=? '
                        'AND task_id IN (6002,6004,120001,120002,120003,120004,120005,120006)', (UID,))
    social_request = fixtures.FamilyFlowTests.social_request
    join = fixtures.FamilyFlowTests.join
    battle_request = fixtures.FamilyFlowTests.battle_request
    start_prepare = fixtures.FamilyFlowTests.start_prepare
    finish = fixtures.FamilyFlowTests.finish
    pushed_to = fixtures.FamilyFlowTests.pushed_to

    def progress(self, tid):
        return self.db.query('SELECT progress FROM task WHERE uid=? AND task_id=?', (UID, tid))[0]['progress']

    def finish_task(self, tid):
        need = self.db.query('SELECT need FROM task_cfg WHERE task_id=?', (tid,))[0]['need']
        self.db.execute('UPDATE task SET progress=? WHERE uid=? AND task_id=?', (need, UID, tid))

    def member(self, now=NOW, gid=GID):
        row = social.membership(self.db, UID)
        row['guild_id'] = gid
        return social.member_wire(self.db, row, now)

    def claim(self, tid):
        return self.task_request(28010, {'id': tid})

    def task_request(self, cmd, data):
        self.ctx.reset_operation_state()
        req = CSRequest(cmd, encode('cs_'+str(cmd), data), uid=UID)
        op = operations.TaskSubmitOp() if cmd == 28010 else operations.TaskOneKeySubmitOp()
        result = self.core._run_operation(op, req, self.ctx)
        frames = [(f['sc'],decode(f['payload'],'sc_'+str(f['sc']))) for f in result.frames]
        return dict(frames)[cmd+1], frames

    def test_rift_win_updates_daily_and_guild_any_stage_tasks(self):
        self.join()
        bid, _ = self.start_prepare()
        ack, frames = self.finish(bid)
        self.assertEqual(ack['result'], 0)
        self.assertEqual([self.progress(t) for t in (6002,6004,120004)], [1,1,1])
        changed = {r['id']: r['progress'] for cmd, body in frames if cmd == 28007
                   for r in body['progress_list']}
        self.assertEqual({tid: changed[tid] for tid in (6002,6004,120004)}, {6002:1,6004:1,120004:1})
        self.assertEqual(self.member()['vitality'], 0)

    def test_rift_replay_does_not_advance_tasks_again(self):
        self.join()
        bid, _ = self.start_prepare()
        self.finish(bid)
        ack, frames = self.finish(bid)
        self.assertEqual(ack['result'], 0)
        self.assertEqual(self.progress(120004), 1)
        self.assertFalse(any(cmd == 28007 for cmd, _ in frames))
        self.assertEqual(self.member()['vitality'], 0)

    def test_failed_or_interrupted_rift_does_not_advance_tasks(self):
        self.join()
        for result in (2,3):
            bid, _ = self.start_prepare()
            ack, _ = self.finish(bid, result=result)
            self.assertEqual(ack['result'], 0)
        self.assertEqual([self.progress(t) for t in (6002,6004,120004)], [0,0,0])

    def test_rift_task_notification_failure_rolls_back_quota_score_and_tasks(self):
        self.join()
        bid, _ = self.start_prepare()
        original = protocol.encode
        with patch.object(protocol, 'encode', side_effect=lambda name, body:
                          None if name == 'sc_28007' else original(name, body)):
            ack, _ = self.finish(bid)
        self.assertNotEqual(ack['result'], 0)
        self.assertEqual(self.progress(120004), 0)
        self.assertEqual(social.boss_info(self.db, UID, GID, NOW)['day_clear_prepose_times'], 0)
        ack, _ = self.finish(bid)
        self.assertEqual(ack['result'], 0)
        self.assertEqual(self.progress(120004), 1)

    def test_rift_packet_failure_can_retry_task_progress_once(self):
        self.join()
        bid, _ = self.start_prepare()
        def fail(*args, **kwargs):
            raise ValueError('injected wire failure')
        ack, _ = self.finish(bid, frame_packer=fail)
        self.assertNotEqual(ack['result'], 0)
        self.assertEqual(self.progress(120004), 0)
        self.assertEqual(self.finish(bid)[0]['result'], 0)
        self.assertEqual(self.progress(120004), 1)

    def test_rift_slot_does_not_unlock_a_same_number_main_stage_task(self):
        self.join()
        tid = 38999001
        self.db.execute('INSERT INTO task_cfg(task_id,name,task_type,condition,need,additional_parameter) '
                        'VALUES(?,\'slot collision\',3,401,1,\'[1]\')', (tid,))
        self.db.execute('INSERT INTO task(uid,task_id,progress) VALUES(?,?,0)', (UID, tid))
        bid, _ = self.start_prepare(level=1)
        self.assertEqual(self.finish(bid)[0]['result'], 0)
        self.assertEqual(self.progress(tid), 0)

    def test_guild_task_claim_books_native_vitality_and_shared_resources(self):
        self.join()
        self.finish_task(120004)
        before = {iid: InventoryService.get_item_balance(self.db, UID, iid)[1]
                  for iid in (40,52001,52002,52003)}
        ack, _ = self.claim(120004)
        self.assertEqual(ack['result'], 0)
        self.assertEqual(self.member()['vitality'], 150)
        self.assertEqual(self.member()['total_vitality'], 150)
        wire = social.guild_wire(self.db, GID, UID, NOW)
        self.assertEqual((wire['club_exp'], wire['weekly_gain_exp'], wire['weekly_gain_coin']), (100,100,15))
        self.assertEqual(InventoryService.get_item_balance(self.db, UID, 40)[1], before[40]+200)
        for iid in (52001,52002,52003):
            self.assertEqual(InventoryService.get_item_balance(self.db, UID, iid)[1], before[iid])

    def test_single_claim_retry_does_not_duplicate_member_vitality(self):
        self.join()
        self.finish_task(120004)
        self.assertEqual(self.claim(120004)[0]['result'], 0)
        self.assertEqual(self.claim(120004)[0]['result'], 0)
        self.assertEqual(self.member()['vitality'], 150)

    def test_bulk_claim_and_retry_book_each_task_once(self):
        self.join()
        for tid in (120003,120004):
            self.finish_task(tid)
        for _ in range(2):
            ack, _ = self.task_request(28014, {'id_list':[120003,120004]})
            self.assertEqual(ack['result'], 0)
        self.assertEqual(self.member()['vitality'], 300)
        self.assertEqual(social.guild_wire(self.db, GID, UID, NOW)['club_exp'], 200)

    def test_daily_task_points_are_not_member_vitality(self):
        self.join()
        self.finish_task(6002)
        before = self.db.get_activity_pt_dict(UID)[1]['active_point']
        self.assertEqual(self.claim(6002)[0]['result'], 0)
        self.assertEqual(self.db.get_activity_pt_dict(UID)[1]['active_point'], before+20)
        self.assertEqual(self.member()['vitality'], 0)

    def test_claim_ledger_failure_rolls_back_personal_rewards_and_task_flags(self):
        self.join()
        self.finish_task(120004)
        before = InventoryService.get_item_balance(self.db, UID, 40)[1]
        original = self.db.execute
        def fail(sql, args=()):
            if sql.startswith('INSERT INTO social_player_contributions '):
                raise ValueError('injected guild contribution write failure')
            return original(sql, args)
        with patch.object(self.db, 'execute', side_effect=fail):
            ack, _ = self.claim(120004)
        self.assertNotEqual(ack['result'], 0)
        self.assertFalse(self.db.is_task_claimed(UID,120004))
        self.assertEqual(InventoryService.get_item_balance(self.db, UID,40)[1], before)
        self.assertEqual(self.member()['vitality'], 0)
        self.assertEqual(self.claim(120004)[0]['result'], 0)
        self.assertEqual(self.member()['vitality'], 150)

    def test_claim_reply_failure_rolls_back_shared_resources(self):
        self.join()
        self.finish_task(120004)
        original = self.ctx.codec_encode
        with patch.object(self.ctx, 'codec_encode', side_effect=lambda name, body:
                          b'' if name == 'sc_28011' else original(name, body)):
            ack, _ = self.claim(120004)
        self.assertNotEqual(ack['result'], 0)
        self.assertFalse(self.db.is_task_claimed(UID,120004))
        self.assertEqual(self.member()['vitality'], 0)
        self.assertEqual(social.guild_wire(self.db, GID, UID, NOW)['club_exp'], 0)

    def test_direct_claim_retry_keeps_original_claim_identity(self):
        self.join()
        self.finish_task(120004)
        self.assertTrue(self.db.claim_task(UID,120004,NOW))
        self.assertFalse(self.db.claim_task(UID,120004,NOW+60))
        self.assertEqual(self.member()['vitality'], 150)
        self.assertEqual(self.db.query('SELECT claimed_ts FROM task WHERE uid=? AND task_id=120004', (UID,))[0]['claimed_ts'], NOW)

    def test_missing_task_cannot_create_claim_or_contribution(self):
        self.join()
        self.db.execute('DELETE FROM task WHERE uid=? AND task_id=120004', (UID,))
        self.assertFalse(self.db.claim_task(UID,120004,NOW))
        self.assertFalse(self.db.is_claimed(UID,'task',120004))
        self.assertEqual(self.member()['vitality'], 0)

    def test_membership_is_required_for_shared_contribution(self):
        self.finish_task(120004)
        self.assertTrue(self.db.claim_task(UID,120004,NOW-1))
        self.join()
        self.assertEqual(self.member()['vitality'], 0)

    def test_pre_join_claim_is_not_imported_into_new_membership(self):
        self.join()
        self.db.execute('UPDATE task SET claimed_ts=?,complete_flag=1 WHERE uid=? AND task_id=120004', (NOW-1, UID))
        self.db.execute('DELETE FROM server_global_config WHERE key=?', (contributions.KEY,))
        contributions.initialize(self.db,NOW)
        self.assertEqual(self.member()['vitality'], 0)

    def test_legacy_four_claims_restore_600_without_reawarding_personal_items(self):
        self.join()
        self.db.execute('UPDATE social_members SET joined_at=? WHERE uid=?', (NOW-3, UID))
        self.db.execute('UPDATE task SET progress=50,claimed_ts=?,complete_flag=1 WHERE uid=? '
                        'AND task_id IN (120003,120004,120005,120006)', (NOW, UID))
        self.db.execute('DELETE FROM server_global_config WHERE key=?', (contributions.KEY,))
        tables = ('users','hero','currency','material','task','social_members','social_requests','claim_ledger','social_guilds')
        before = {name: self.db.query('SELECT * FROM '+name) for name in tables}
        for _ in range(2):
            contributions.initialize(self.db,NOW)
        self.assertEqual(self.member()['vitality'], 600)
        self.assertEqual(social.guild_wire(self.db,GID,UID,NOW)['club_exp'], 400)
        self.assertEqual({name: self.db.query('SELECT * FROM '+name) for name in tables}, before)

    def test_weekly_reset_keeps_total_and_previous_week_vitality(self):
        self.join()
        self.finish_task(120004)
        self.assertEqual(self.claim(120004)[0]['result'], 0)
        next_week = NOW+7*social.DAY
        self.assertEqual(self.member(next_week)['vitality'], 0)
        self.assertEqual(self.member(next_week)['total_vitality'], 150)
        self.assertEqual(social.guild_wire(self.db,GID,UID,next_week)['last_week_total_vitality'], 150)

    def test_leaving_does_not_move_old_contribution_to_another_guild(self):
        self.join()
        self.finish_task(120004)
        self.assertEqual(self.claim(120004)[0]['result'], 0)
        social.leave_guild(self.db,UID,GID)
        gid = social.create_guild(self.db,UID,'贡献隔离',1,'测试',NOW+1)
        self.assertEqual(self.member(NOW+1,gid)['vitality'], 0)
        self.assertEqual(contributions.member_totals(self.db,GID,UID,NOW)['total'], 150)

    def test_guild_experience_weekly_cap_does_not_cap_member_vitality(self):
        self.join()
        bot = self.db.query('SELECT uid FROM social_bots LIMIT 1')[0]['uid']
        self.db.execute('INSERT INTO social_contributions(guild_id,uid,day_id,vitality,updated_at) VALUES(?,?,?,?,?)',
                        (GID,bot,NOW,11980,NOW))
        self.db.execute('UPDATE social_guilds SET exp=11980 WHERE id=?',(GID,))
        self.finish_task(120004)
        self.assertEqual(self.claim(120004)[0]['result'], 0)
        wire = social.guild_wire(self.db,GID,UID,NOW)
        self.assertEqual((wire['club_exp'],wire['weekly_gain_exp']), (12000,12000))
        self.assertEqual(self.member()['vitality'], 150)

    def test_guild_coin_weekly_cap_preserves_member_vitality(self):
        self.join()
        bot = self.db.query('SELECT uid FROM social_bots LIMIT 1')[0]['uid']
        self.db.execute('INSERT INTO social_contributions(guild_id,uid,day_id,donation,updated_at) VALUES(?,?,?,?,?)',
                        (GID,bot,NOW,1795,NOW))
        self.finish_task(120004)
        self.assertEqual(self.claim(120004)[0]['result'], 0)
        self.assertEqual(social.guild_wire(self.db,GID,UID,NOW)['weekly_gain_coin'], 1800)
        self.assertEqual(self.member()['vitality'], 150)

    def test_claimed_member_contribution_survives_database_reopen(self):
        self.join()
        self.finish_task(120004)
        self.assertEqual(self.claim(120004)[0]['result'], 0)
        path = self.db.path
        self.db.close()
        self.db = AccountDB(path)
        self.addCleanup(self.db.close)
        social.initialize(self.db,NOW)
        self.assertEqual(self.member()['vitality'], 150)

    def test_production_dispatch_uses_claim_operation_and_books_personal_reward(self):
        self.join()
        self.finish_task(120004)
        before = InventoryService.get_item_balance(self.db, UID,40)[1]
        request = CoreRequest(28010, encode('cs_28010',{'id':120004}), uid=UID)
        result = self.core.dispatch(request)
        ack = decode(next(f['payload'] for f in result.frames if f['sc']==28011),'sc_28011')
        self.assertEqual(ack['result'],0)
        self.assertEqual(InventoryService.get_item_balance(self.db,UID,40)[1],before+200)
        self.assertEqual(self.member()['vitality'],150)

    def test_new_member_receives_zero_progress_guild_tasks(self):
        self.db.execute('DELETE FROM task WHERE uid=? AND task_id BETWEEN 120001 AND 120006', (UID,))
        frames = self.join()
        tasks = next(body for cmd, body in frames if cmd == 28001)
        self.assertEqual(tasks['send_type'],1)
        self.assertEqual({r['id'] for r in tasks['assignment_list']},set(range(120001,120007)))
        self.assertTrue(all(r['progress']==0 and r['complete_flag']==0 for r in tasks['assignment_list']))
        self.assertEqual(self.member()['vitality'],0)
        bid, _ = self.start_prepare()
        self.assertEqual(self.finish(bid)[0]['result'],0)
        self.assertEqual(self.progress(120004),1)

    def test_new_guild_creation_seeds_tasks_without_rewards(self):
        self.db.execute('DELETE FROM task WHERE uid=? AND task_id BETWEEN 120001 AND 120006', (UID,))
        ack, frames = self.social_request(31008,{'name':'新建任务','icon':1,'notice':'测试'})
        self.assertEqual(ack['result'],0)
        tasks = next(body for cmd, body in frames if cmd==28001)
        self.assertEqual(len(tasks['assignment_list']),6)
        self.assertTrue(all(r['progress']==0 for r in tasks['assignment_list']))

    def test_approval_seeds_offline_member_tasks_for_next_login(self):
        self.join()
        self.db.execute('DELETE FROM task WHERE uid=? AND task_id BETWEEN 120001 AND 120006', (OTHER,))
        self.social_request(31018,{'setting':{'examine_recruit_join_request_switch':2,'join_request_limit':1}})
        self.join(OTHER)
        with patch('server_net.get_online_uids',return_value=[UID]):
            ack, _ = self.social_request(31032,{'id':OTHER,'type':1})
        self.assertEqual(ack['result'],0)
        tasks = decode(protocol.guild_tasks_frame(self.db,OTHER).payload,'sc_28001')
        self.assertEqual(len(tasks['assignment_list']),6)
        self.assertTrue(all(r['progress']==0 for r in tasks['assignment_list']))

    def test_join_task_notification_failure_rolls_back_membership_and_new_tasks(self):
        self.db.execute('DELETE FROM task WHERE uid=? AND task_id BETWEEN 120001 AND 120006', (UID,))
        original = protocol.encode
        with patch.object(protocol,'encode',side_effect=lambda name,body:
                          None if name=='sc_28001' else original(name,body)):
            ack, _ = self.social_request(31010,{'id':GID})
        self.assertNotEqual(ack['result'],0)
        self.assertIsNone(social.membership(self.db,UID))
        self.assertFalse(self.db.query('SELECT 1 FROM task WHERE uid=? AND task_id BETWEEN 120001 AND 120006',(UID,)))

    def test_rift_first_reward_adds_60_vitality_once(self):
        self.join()
        bid, _ = self.start_prepare()
        self.assertEqual(self.finish(bid)[0]['result'],0)
        body={'club_id':GID,'type':3,'award_id':1}
        self.assertEqual(self.social_request(33008,body)[0]['result'],0)
        self.assertEqual(self.member()['vitality'],60)
        self.assertNotEqual(self.social_request(33008,body)[0]['result'],0)
        self.assertEqual(self.member()['vitality'],60)

    def test_rift_first_reward_failure_rolls_back_vitality_and_claim_flag(self):
        self.join()
        bid, _ = self.start_prepare()
        self.finish(bid)
        body={'club_id':GID,'type':3,'award_id':1}
        with patch('gm_actions.asset_frames',side_effect=ValueError('injected award notification failure')):
            ack, _ = self.social_request(33008,body)
        self.assertNotEqual(ack['result'],0)
        self.assertEqual(self.member()['vitality'],0)
        self.assertEqual(social.boss_info(self.db,UID,GID,NOW)['day_first_clear_award_admitted'],0)
        self.assertEqual(self.social_request(33008,body)[0]['result'],0)
        self.assertEqual(self.member()['vitality'],60)

    def test_legacy_task_and_first_reward_restore_660_without_reawarding(self):
        self.join()
        bid, _ = self.start_prepare()
        self.finish(bid)
        self.db.execute('UPDATE social_prepose_daily SET claimed=1 WHERE uid=?',(UID,))
        self.db.execute('UPDATE task SET progress=50,claimed_ts=?,complete_flag=1 WHERE uid=? '
                        'AND task_id IN (120003,120004,120005,120006)',(NOW,UID))
        self.db.execute('DELETE FROM server_global_config WHERE key=?',(contributions.KEY,))
        before={name:self.db.query('SELECT * FROM '+name) for name in
                ('users','currency','material','task','social_prepose_daily','social_prepose_runs')}
        for _ in range(2):
            contributions.initialize(self.db,NOW)
        self.assertEqual(self.member()['vitality'],660)
        self.assertEqual({name:self.db.query('SELECT * FROM '+name) for name in before},before)

    def test_ambiguous_old_guild_daily_reward_is_not_backfilled(self):
        self.join()
        bid, _ = self.start_prepare()
        self.finish(bid)
        self.db.execute('UPDATE social_prepose_daily SET claimed=1 WHERE uid=?',(UID,))
        self.db.execute('INSERT INTO social_prepose_runs(uid,battle_id,guild_id,week_id,day_id,level_id,heroes,state,result,finished_at) '
                        'SELECT uid,battle_id+1,guild_id+1,week_id,day_id,level_id,heroes,1,1,finished_at '
                        'FROM social_prepose_runs WHERE uid=? AND battle_id=?',(UID,bid))
        self.db.execute('DELETE FROM server_global_config WHERE key=?',(contributions.KEY,))
        contributions.initialize(self.db,NOW)
        self.assertEqual(self.member()['vitality'],0)


if __name__ == '__main__':
    unittest.main()
