"""Protocol adapter for world chat and the supported guild functions."""
import logging
import time

from codec import encode
from middleware import CSResponse, DownFrame
from operation_notifications import checkpoint_notifications, restore_notifications
import social_service as social

log = logging.getLogger(__name__)
COMMANDS = {27010, 27012, 27014, 27020, 27030, 27500, 27400, 27402,
            *range(31002, 31036, 2), 33000, 33002, 33004, 33006, 33008, 33010}


def frame(cmd, data):
    payload = encode('sc_' + str(cmd), data)
    if payload is None:
        raise ValueError('协议编码失败')
    return DownFrame(cmd, payload)


def chat_frame(db, row):
    wire = social.message_wire(db, row)
    if row['channel'] == 1:
        return frame(27007, {'chat_msg_list': [{'msg': wire, 'room_id': row['scope_id']}]})
    return frame(27023, {'msg': [wire]})


def recipients(db, channel, target, online=None):
    if online is None:
        from server_net import get_online_uids
        online = get_online_uids()
    if channel == 1:
        return [uid for uid in online if social.room(db, uid) == target]
    members = {r['uid'] for r in db.query('SELECT uid FROM social_members WHERE guild_id=? AND is_bot=0', (target,))}
    return [uid for uid in online if uid in members]


def publish(db, row, exclude_uid=None):
    from server_net import push_gm_frames
    message = chat_frame(db, row)
    delivered = 0
    for uid in recipients(db, row['channel'], row['scope_id']):
        if uid != exclude_uid:
            delivered += push_gm_frames(uid, [message])
    return delivered


def own_guild_frame(db, uid, now):
    member = social.membership(db, uid)
    if not member:
        raise ValueError('未入会玩家不能接收公会主界面数据')
    body = {'last_share_timestamp': 0, 'assist_hero_id_list': []}
    if member:
        body['club'] = social.guild_wire(db, member['guild_id'], uid, now)
        import json
        body['assist_hero_id_list'] = json.loads(member['assist_heroes'])
    return frame(31001, body)


def guild_tasks_frame(db, uid):
    from fresh_profile import task_phases
    tasks = db.query('SELECT task_id FROM task WHERE uid=?', (uid,))
    phases = task_phases(db, uid, tasks)
    rows = db.query('SELECT t.* FROM task t JOIN task_cfg c ON c.task_id=t.task_id '
                    'WHERE t.uid=? AND c.task_type=11 AND COALESCE(c.activity_id,0)=0', (uid,))
    return frame(28001, {'send_type': 1, 'newbie_phase': phases[3], 'assignment_phase': phases[4],
                        'assignment_list': [dict(id=r['task_id'], progress=r['progress'] or 0,
                            complete_flag=int(bool(r['claimed_ts']) or bool(r['complete_flag'])),
                            expired_timestamp=r['expired_ts'] or 0) for r in rows]})


def _guild_changed(db, guild_id, now, exclude_uid=None, events=()):
    from server_net import push_gm_frames
    for uid in recipients(db, 3, guild_id):
        if uid != exclude_uid:
            try:
                push_gm_frames(uid, [*events, own_guild_frame(db, uid, now), request_frame(db, uid)])
            except Exception:
                log.exception('公会数据推送失败，客户端可重新读取')


def request_frame(db, uid):
    member = social.membership(db, uid)
    requests = []
    if member and member['post'] in (1, 2):
        for row in db.query('SELECT r.uid,u.level FROM social_requests r JOIN users u ON u.uid=r.uid WHERE r.guild_id=? ORDER BY r.created_at', (member['guild_id'],)):
            requests.append(dict(id=row['uid'], base_info=social.profile(db, row['uid']), online_info=0, level=row['level'] or 1))
    return frame(31979, {'join_list': requests})


def cancelled_requests(uid, guild_ids, events, changed, accepted_guild=None):
    # Native sc_31979 only appends entries. Each removed request also needs
    # sc_31999, including applications cancelled by joining/creating a guild.
    for gid in guild_ids:
        if gid != accepted_guild:
            events.setdefault(gid, []).append(frame(31999, {'id': uid, 'type': 3}))
            changed.add(gid)


def _claim_reward(db, ctx, uid, data, now):
    import guild_cycle
    import guild_prepose
    gid = social.integer(data.get('club_id'), '公会编号', 1, 4294967295)
    kind = social.integer(data.get('type'), '奖励类型', 1, 3)
    award = social.integer(data.get('award_id'), '奖励档位', 1, 4294967295)
    social.require_member(db, uid, gid)
    if kind == 3:
        if guild_cycle.phase(now) != 'prepare' or award != 1:
            raise ValueError('前置挑战首通奖励档位或阶段无效')
        daily = guild_prepose.daily(db, uid, now)
        if not daily['clear_times'] or daily['claimed']:
            raise ValueError('需要本人完成前置挑战，且今日尚未领取奖励')
        reward = social.CATALOG['prepose']['1']['personal_reward']
    else:
        if guild_cycle.phase(now) != 'challenge':
            raise ValueError('裂隙开放阶段才能领取首领奖励')
        info = social.boss_info(db, uid, gid, now)['info']
        difficulty = info['difficulty']
        cfg = social.CATALOG['bosses'][str(difficulty)]
        options = cfg['personal_reward'] if kind == 1 else cfg['reward']
        reward = next((r[1] for r in options if r[0] == award), None)
        score = info['personal_score'] if kind == 1 else info['collective_damage_award']
        if not reward or score < award:
            raise ValueError('尚未达到奖励条件')
        week = social.get_weekly_mon_5am_ts(now)
        key = (uid, week, difficulty, kind, award)
        if db.query('SELECT 1 FROM social_reward_claims WHERE uid=? AND week_id=? AND difficulty=? AND kind=? AND award_id=?', key):
            raise ValueError('奖励已领取')
    from inventory_service import InventoryService
    import gm_actions
    for item, count in reward:
        gm_actions.require_item(db, item, count)
    summary = InventoryService.grant_items(ctx, uid, reward, source='guild_boss')
    if kind == 3:
        db.execute('UPDATE social_prepose_daily SET claimed=1 WHERE uid=? AND day_id=?',
                   (uid, social.get_daily_5am_ts(now)))
    else:
        db.execute('INSERT INTO social_reward_claims VALUES(?,?,?,?,?,?,?)', (*key, gid, now))
    import guild_contributions
    guild_contributions.record_rift_claim(db, uid, gid, kind, reward, now,
        day_id=social.get_daily_5am_ts(now), week_id=0 if kind==3 else week,
        difficulty=0 if kind==3 else difficulty, award_id=award)
    return gm_actions.asset_frames(ctx, summary)


def dispatch(req, ctx):
    db, uid, cmd, now = ctx.db, int(req.uid), int(req.cmd), int(time.time())
    ack = cmd + 1
    notifications, guild_changed, direct, guild_events = [], set(), [], {}
    checkpoint = checkpoint_notifications(ctx)
    try:
        social.initialize(db, now)
        ctx.uid = uid
        data = ctx.codec_decode(req.payload, 'cs_' + str(cmd)) if ctx.codec_decode else {}
        if not isinstance(data, dict) or not db.query('SELECT 1 FROM users WHERE uid=?', (uid,)):
            raise ValueError('账号或请求无效')
        with db.transaction(immediate=True):
            response = {'result': 0}
            extra = []
            if cmd == 27010:
                target = social.integer(data.get('room_id'), '频道编号', 1, 999)
                db.execute('INSERT INTO social_rooms VALUES(?,?) ON CONFLICT(uid) DO UPDATE SET room_id=excluded.room_id', (uid, target))
                extra = [frame(27005, {'room_id': target}), frame(27007, {'chat_msg_list': social.history(db, uid, 1)})]
            elif cmd == 27012:
                # This is a client Push, not an acknowledged RPC.
                frames = [frame(27005, {'room_id': social.room(db, uid)}),
                          frame(27007, {'chat_msg_list': social.history(db, uid, 1)})]
                if social.membership(db, uid):
                    frames.append(frame(27023, {'msg': [m['msg'] for m in social.history(db, uid, 3)]}))
                response = CSResponse(frames)
                return req.prepare_response(response) if req.prepare_response else response
            elif cmd in (27014, 27020, 27030):
                channel = 1 if cmd == 27014 else 3 if cmd == 27020 else data.get('channel_type')
                row = social.send_message(db, uid, channel, data.get('content'), data.get('type'), now)
                # Generic channels use their generic push format for the caller.
                extra = [frame(27033, {'channel_type': channel, 'msg': [{'msg': social.message_wire(db, row), 'room_id': row['scope_id']}]})] if cmd == 27030 else [chat_frame(db, row)]
                notifications.append(row)
            elif cmd == 27500:
                channel = social.integer(data.get('channel_type'), '频道类型', 1, 3)
                since = social.integer(data.get('timestamp', 0), '时间戳', 0, 4294967295)
                response['msg_list'] = social.history(db, uid, channel, since)
            elif cmd == 27400:
                # Keep recruitment truthful: no success response for a message
                # that this implementation did not publish.
                raise ValueError('请使用公会推荐列表')
            elif cmd == 27402:
                response['msg_list'] = []
            elif cmd == 31002:
                extra.append(request_frame(db, uid))
                member = social.membership(db, uid)
                response['is_join'] = int(bool(member))
                if member:
                    response['club_base_info'] = social.guild_wire(db, member['guild_id'], uid, now)
                    response['last_share_timestamp'] = 0
                else:
                    # A client with stale membership must not navigate to the
                    # guild main page after this response. Clear its cache too.
                    response['result'] = 1
                    extra.append(frame(31989, {'id': uid}))
            elif cmd == 31004:
                member = social.require_member(db, uid)
                response['members'] = [social.member_wire(db, m, now) for m in db.query(
                    'SELECT * FROM social_members WHERE guild_id=? ORDER BY post,uid', (member['guild_id'],))]
            elif cmd == 31006:
                response['recommend_club_list'] = [
                    {'club_info': social.guild_wire(db, g['id'], uid, now),
                     'is_requested': int(bool(db.query('SELECT 1 FROM social_requests WHERE guild_id=? AND uid=?', (g['id'], uid))))}
                    for g in db.query('SELECT id FROM social_guilds ORDER BY id LIMIT 30')]
            elif cmd == 31008:
                previous = {r['guild_id'] for r in db.query('SELECT guild_id FROM social_requests WHERE uid=?', (uid,))}
                gid = social.create_guild(db, uid, data.get('name'), data.get('icon'), data.get('notice'), now)
                cancelled_requests(uid, previous, guild_events, guild_changed)
                guild_changed.add(gid)
                extra = [own_guild_frame(db, uid, now), guild_tasks_frame(db, uid),
                         frame(33001, social.boss_info(db, uid, gid, now))]
            elif cmd == 31010:
                gid = social.integer(data.get('id'), '公会编号', 1, 4294967295)
                previous = {r['guild_id'] for r in db.query('SELECT guild_id FROM social_requests WHERE uid=?', (uid,))}
                if social.join_guild(db, uid, gid, now):
                    extra = [own_guild_frame(db, uid, now), guild_tasks_frame(db, uid),
                             frame(33001, social.boss_info(db, uid, gid, now))]
                    guild_events[gid] = [frame(31985, {'member': social.member_wire(db, social.membership(db, uid), now)})]
                    if gid in previous:
                        guild_events[gid].append(frame(31999, {'id': uid, 'type': 2}))
                    cancelled_requests(uid, previous, guild_events, guild_changed, gid)
                guild_changed.add(gid)
            elif cmd == 31012:
                gid = social.integer(data.get('id'), '公会编号', 1, 4294967295)
                successor = social.leave_guild(db, uid, gid)
                extra = [frame(31989, {'id': uid})]
                guild_events[gid] = list(extra)
                if successor:
                    guild_events[gid].append(frame(31997, {'id': successor['uid'], 'nick': social.profile(db, successor['uid'])['nick']}))
                guild_changed.add(gid)
            elif cmd in (31014, 31016):
                member = social.require_member(db, uid, manager=True)
                target = social.integer(data.get('id'), '成员编号', 1, 4294967295)
                gid = member['guild_id']
                rows = db.query('SELECT * FROM social_members WHERE guild_id=? AND uid=?', (gid, target))
                if not rows or target == uid or rows[0]['post'] <= member['post']:
                    raise ValueError('无法操作该成员')
                other = rows[0]
                if cmd == 31014:
                    import guild_prepose
                    guild_prepose.cancel(db, target, gid)
                    db.execute('DELETE FROM social_members WHERE guild_id=? AND uid=?', (gid, target))
                    if not other['is_bot']:
                        db.execute('UPDATE users SET club_id=0,club_post=0 WHERE uid=?', (target,))
                        direct.append((target, frame(31989, {'id': target})))
                    # Notify the removed member after the transaction.
                    extra.append(frame(31989, {'id': target}))
                    guild_events[gid] = list(extra)
                else:
                    if member['post'] != 1:
                        raise ValueError('需要会长权限')
                    post = social.integer(data.get('post'), '职位', 1, 3)
                    if post == 1 and other['is_bot']:
                        raise ValueError('支援人机不能接任真人家族会长')
                    if post == 1:
                        db.execute('UPDATE social_members SET post=3 WHERE guild_id=? AND post=1', (gid,))
                        db.execute('UPDATE users SET club_post=3 WHERE uid=?', (uid,))
                    db.execute('UPDATE social_members SET post=? WHERE guild_id=? AND uid=?', (post, gid, target))
                    if not other['is_bot']:
                        db.execute('UPDATE users SET club_post=? WHERE uid=?', (post, target))
                    changed = (frame(31997, {'id': target, 'nick': social.profile(db, target)['nick']}) if post == 1
                               else frame(31995 if post == 2 else 31993, {'id': target}))
                    extra.append(changed)
                    guild_events[gid] = [changed]
                extra.append(own_guild_frame(db, uid, now))
                guild_changed.add(gid)
            elif cmd in (31018, 31022, 31024, 31026):
                member = social.require_member(db, uid, manager=True)
                gid = member['guild_id']
                if cmd == 31018:
                    cfg = data.get('setting') or {}
                    switch = social.integer(cfg.get('examine_recruit_join_request_switch'), '入会方式',
                                             social.JOIN_FREE, social.JOIN_CLOSED)
                    level = social.integer(cfg.get('join_request_limit'), '入会等级', 1, 100)
                    db.execute('UPDATE social_guilds SET examine_switch=?,join_min_level=? WHERE id=?', (switch, level, gid))
                elif cmd == 31022:
                    name = social.clean_text(data.get('name'), 12)
                    if db.query('SELECT 1 FROM social_guilds WHERE name=? AND id<>?', (name, gid)):
                        raise ValueError('公会名称已存在')
                    db.execute('UPDATE social_guilds SET name=? WHERE id=?', (name, gid))
                elif cmd == 31024:
                    db.execute('UPDATE social_guilds SET notice=? WHERE id=?', (social.clean_text(data.get('notice'), 120), gid))
                else:
                    db.execute('UPDATE social_guilds SET icon=? WHERE id=?', (social.integer(data.get('icon'), '公会图标', 1, 5), gid))
                extra = [own_guild_frame(db, uid, now)]
                guild_changed.add(gid)
            elif cmd == 31028:
                gid = social.integer(data.get('id'), '公会编号', 1, 4294967295)
                response['club_base_info'] = social.guild_wire(db, gid, uid, now)
            elif cmd == 31032:
                member = social.require_member(db, uid, manager=True)
                gid = member['guild_id']
                target = social.integer(data.get('id'), '成员编号', 1, 4294967295)
                action = social.integer(data.get('type'), '处理方式', 1, 2)
                previous = {r['guild_id'] for r in db.query('SELECT guild_id FROM social_requests WHERE uid=?', (target,))}
                existing = social.membership(db, target)
                replay = action == 1 and existing is not None and existing['guild_id'] == gid
                if gid not in previous and not replay:
                    raise ValueError('申请不存在')
                removed = frame(31999, {'id': target, 'type': 2 if action == 1 else 3})
                extra.append(removed)
                guild_events[gid] = [removed]
                if replay:
                    extra.append(own_guild_frame(db, uid, now))
                    ctx.log(f'cs_31032 已入会申请重复确认 uid={uid} target={target}', 'INFO')
                elif action == 1:
                    social.join_guild(db, target, gid, now, approved=True)
                    direct.append((target, frame(33001, social.boss_info(db, target, gid, now))))
                    direct.append((target, guild_tasks_frame(db, target)))
                    added = frame(31985, {'member': social.member_wire(db, social.membership(db, target), now)})
                    extra.extend([added, own_guild_frame(db, uid, now)])
                    guild_events[gid].append(added)
                if action == 1:
                    db.execute('DELETE FROM social_requests WHERE uid=?', (target,))
                    cancelled_requests(target, previous, guild_events, guild_changed, gid)
                else:
                    db.execute('DELETE FROM social_requests WHERE guild_id=? AND uid=?', (gid, target))
                extra.append(request_frame(db, uid))
                guild_changed.add(gid)
            elif cmd == 33000:
                response = social.boss_info(db, uid, social.integer(data.get('club_id'), '公会编号', 1, 4294967295), now)
            elif cmd == 33002:
                response['assist_info_list'] = social.assist_info(
                    db, uid, social.integer(data.get('club_id'), '公会编号', 1, 4294967295))
            elif cmd == 33004:
                import guild_cycle
                if guild_cycle.phase(now) != 'prepare':
                    raise ValueError('裂隙征兆阶段才能调整首领难度')
                member = social.require_member(db, uid, manager=True)
                difficulty = social.integer(data.get('difficulty'), '难度', 1, 4)
                info = social.boss_info(db, uid, member['guild_id'], now)['info']
                unlocked = max([1] + [min(4, d + 1) for d in info['cleared_difficulty_list']])
                if difficulty > unlocked:
                    raise ValueError('尚未解锁该难度')
                if difficulty != info['difficulty']:
                    db.execute('UPDATE social_guilds SET difficulty=?,boss_started_at=?,last_tick_slot=0 WHERE id=?',
                               (difficulty, now, member['guild_id']))
                extra = [frame(33001, social.boss_info(db, uid, member['guild_id'], now))]
            elif cmd == 33006:
                gid = social.integer(data.get('club_id'), '公会编号', 1, 4294967295)
                response['info_list'] = social.boss_rank(db, uid, gid, now)
            elif cmd == 33008:
                extra = _claim_reward(db, ctx, uid, data, now)
                extra.append(frame(33001, social.boss_info(db, uid, now_ts=now)))
            elif cmd == 33010:
                gid = social.integer(data.get('club_id'), '公会编号', 1, 4294967295)
                social.require_member(db, uid, gid)
                heroes = data.get('hero_id_list') or []
                if len(heroes) > 3 or len(set(heroes)) != len(heroes):
                    raise ValueError('助战角色数量无效')
                for hid in heroes:
                    if not db.query('SELECT 1 FROM hero WHERE uid=? AND id=? AND unlock=1', (uid, hid)):
                        raise ValueError('尚未拥有该角色')
                import json
                db.execute('UPDATE social_members SET assist_heroes=? WHERE guild_id=? AND uid=?', (json.dumps(heroes), gid, uid))
            else:
                raise ValueError('暂不支持该公会操作')
            frames = [frame(ack, response), *extra]
            prepared = CSResponse(frames)
            if req.prepare_response:
                prepared = req.prepare_response(prepared)
        # Socket work happens only after commit; failures must not undo or replay
        # a committed membership change, grant, or chat message.
        for row in notifications:
            try:
                publish(db, row, exclude_uid=uid)
            except Exception:
                log.exception('聊天已保存，在线推送失败')
        for gid in guild_changed:
            try:
                _guild_changed(db, gid, now, exclude_uid=uid, events=guild_events.get(gid, ()))
            except Exception:
                log.exception('公会变更已提交，通知失败')
        for target, packet in direct:
            try:
                from server_net import push_gm_frames
                push_gm_frames(target, [packet])
            except Exception:
                log.exception('公会成员通知失败')
        return prepared
    except ValueError as error:
        restore_notifications(ctx, checkpoint)
        ctx.log('社交请求被拒绝: ' + str(error), 'WARN')
        response = CSResponse([frame(ack, {'result': 1})])
        return req.prepare_response(response, failure=True) if req.prepare_response else response
    except Exception:
        restore_notifications(ctx, checkpoint)
        log.exception('社交请求处理失败 cmd=%s', cmd)
        response = CSResponse([frame(ack, {'result': 1})])
        return req.prepare_response(response, failure=True) if req.prepare_response else response


def login_payload(db, uid, cmd):
    social.initialize(db)
    now = int(time.time())
    if cmd == 31001:
        return own_guild_frame(db, uid, now).payload
    if cmd == 31979:
        return request_frame(db, uid).payload
    if cmd == 33001:
        return frame(33001, social.boss_info(db, uid, now_ts=now)).payload
    if cmd == 27005:
        return frame(27005, {'room_id': social.room(db, uid)}).payload
    if cmd == 27007:
        return frame(27007, {'chat_msg_list': social.history(db, uid, 1)}).payload
    if cmd == 27023:
        return frame(27023, {'msg': [x['msg'] for x in social.history(db, uid, 3)] if social.membership(db, uid) else []}).payload
    raise ValueError('Unknown social snapshot')
