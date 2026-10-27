"""Transactional GM asset/hero operations and truthful online-sync results."""
import json
import time

import generator
import hero_codec
from codec import encode, decode
from middleware import HandlerCtx, DownFrame
from inventory_service import InventoryService, ItemType, _get_equip_map
from enchant_slots_util import default_slot_list


def context(db):
    return HandlerCtx(db=db, generator=generator, codec_encode=encode, codec_decode=decode)


def sync(uid, frames):
    if not frames:
        return {"clients": 0, "requires_relogin": False}
    try:
        from server_net import push_gm_frames
        clients = push_gm_frames(uid, frames)
        return {"clients": clients, "requires_relogin": clients == 0}
    except Exception:
        # The transaction has committed. Never retry a grant after a push fails.
        return {"clients": 0, "requires_relogin": True}


def asset_frames(ctx, summary=None, equip_list=None, weapon_list=None):
    summary = summary or {}
    touched = set(ctx.touched_items)
    for key in ("currency", "material"):
        touched.update(summary.get(key, {}))
    touched.update(10000 + int(hid) for hid in summary.get("hero_piece", {}))
    touched.update(summary.get("skins", []))
    touched.update(summary.get("scenes", []))
    touched.update(item["target_id"] for item in summary.get("overflow_converted", []))
    equips = list(equip_list or [])
    for item in summary.get("equips", []):
        equips.append({"equip_id": item["id"], "prefab_id": item["prefab_id"], "num": 1,
                       "enchant_slot_list": item["enchant_slot_list"]})
    weapons = list(weapon_list or [])
    for item in summary.get("servants", []):
        weapons.append({"uid": item["id"], "item": {"id": item["prefab_id"], "num": 1}})
    frames = []
    if touched or equips or weapons:
        payload = generator.gen_payload(17023, uid=ctx.uid, db=ctx.db, touched_items=touched,
                                        equip_list=equips, weapon_list=weapons, strict=True)
        if not payload:
            raise ValueError("无法生成资产同步数据，操作已回滚")
        frames.append(DownFrame(17023, payload))
    for hid in summary.get("hero_unlocked", []):
        frame = hero_codec.build_hero_14007_frame(ctx.db, ctx.uid, hid)
        if not frame:
            raise ValueError("无法生成角色同步数据，操作已回滚")
        frames.append(frame)
    frames.extend(ctx.pop_pending_frames())
    return frames


def require_item(db, item_id, count):
    if item_id == 30054 or 1000000000 <= item_id < 2000000000:
        raise ValueError("该物品仅供图鉴展示，不能直接发放")
    if not db.query("SELECT id FROM item_catalog WHERE id=?", (item_id,)):
        raise ValueError("物品不在当前服务端物品目录中")
    kind = InventoryService.classify_item(db, item_id)
    if kind in (ItemType.EQUIP, ItemType.SERVANT, ItemType.HERO) and count > 200:
        raise ValueError("独立实例资产单次最多发放 200 个")
    if kind == ItemType.EQUIP and str(item_id) not in _get_equip_map():
        raise ValueError("未找到该刻印的套装和槽位配置")
    if kind == ItemType.HERO and not hero_codec.is_hero_payload_candidate(item_id):
        raise ValueError("当前客户端没有该角色的编码配置")
    return kind


def grant(db, uid, item_id, count):
    require_item(db, item_id, count)
    ctx = context(db)
    ctx.uid = uid
    with db.transaction():
        summary = InventoryService.grant_item(ctx, uid, item_id, count=count, source="gm")
        frames = asset_frames(ctx, summary)
        _, balance = InventoryService.get_item_balance(db, uid, item_id)
    return {"uid": uid, "item_id": item_id, "num": count, "balance": balance,
            "changes": summary, "sync": sync(uid, frames)}


def grant_suit(db, uid, suit_id, star, count=1):
    choices = {}
    for key, item in _get_equip_map().items():
        prefab = int(key)
        if int(item["suit"]) == suit_id and prefab // 100000 == star:
            choices.setdefault(int(item["pos"]), []).append(prefab)
    if set(choices) != set(range(1, 7)):
        raise ValueError("该星级套装没有完整的六槽位配置")
    prefabs = [min(choices[pos]) for pos in range(1, 7)]
    for prefab in prefabs:
        if require_item(db, prefab, count) != ItemType.EQUIP:
            raise ValueError("套装条目没有对应的刻印资产类型")
    ctx = context(db)
    ctx.uid = uid
    with db.transaction():
        summary = InventoryService.grant_items(ctx, uid, [(prefab, count) for prefab in prefabs], source="gm_suit")
        if len(summary["equips"]) != 6 * count:
            raise ValueError("套装发放不完整，操作已回滚")
        frames = asset_frames(ctx, summary)
    return {"uid": uid, "suit_id": suit_id, "star": star, "count": count,
            "generated_count": len(summary["equips"]), "instance_ids": [x["id"] for x in summary["equips"]],
            "sync": sync(uid, frames)}


def cleanup_candidates(db, uid, category, max_star=3):
    if category not in ("equip", "servant") or not 1 <= max_star <= 5:
        raise ValueError("清理仅支持闲置刻印或钥从，星级须在 1~5 之间")
    heroes = db.query("SELECT equip_slot,weapon_servant_uid FROM hero WHERE uid=?", (uid,))
    worn = set()
    if category == "equip":
        for hero in heroes:
            slots = json.loads(hero.get("equip_slot") or "{}")
            if not isinstance(slots, dict):
                raise ValueError("角色刻印槽位数据异常，暂不执行清理")
            worn.update(int(x) for x in slots.values() if x)
        rows = db.query("SELECT * FROM equip WHERE uid=? AND COALESCE(is_lock,0)=0 "
                        "AND COALESCE(hero_id,0)=0 AND COALESCE(exp,0)=0 "
                        "AND COALESCE(now_break_level,0)=0 ORDER BY id", (uid,))
    else:
        worn = {int(h["weapon_servant_uid"]) for h in heroes if h.get("weapon_servant_uid")}
        rows = db.query("SELECT * FROM servant WHERE uid=? AND COALESCE(is_locked,0)=0 "
                        "AND COALESCE(stage,1)<=1 ORDER BY id", (uid,))
    result = []
    for row in rows:
        if row["id"] in worn:
            continue
        prefab = int(row.get("prefab_id") or 0)
        star = prefab // 100000 % 10
        if not 1 <= star <= max_star:
            continue
        if category == "equip":
            if any(int(row.get(k) or 0) for k in ("race", "race_hero", "race_faction")):
                continue
            try:
                slots = json.loads(row.get("enchant_slots") or "[]")
            except (ValueError, TypeError):
                continue
            if slots and slots != default_slot_list():
                continue
        result.append({"id": row["id"], "prefab_id": prefab, "star": star})
        if len(result) >= 1000:
            break
    return result


def cleanup(db, uid, category, max_star, ids):
    if not ids or len(ids) > 1000 or len(ids) != len(set(ids)):
        raise ValueError("请先预览清理清单，单次最多清理 1000 个实例")
    requested = set(ids)
    ctx = context(db)
    ctx.uid = uid
    with db.transaction():
        eligible = cleanup_candidates(db, uid, category, max_star)
        removed = [row for row in eligible if row["id"] in requested]
        for row in removed:
            db.execute('DELETE FROM "' + category + '" WHERE uid=? AND id=?', (uid, row["id"]))
        equips = [{"equip_id": r["id"], "prefab_id": r["prefab_id"], "num": 0} for r in removed] if category == "equip" else []
        weapons = [{"uid": r["id"], "item": {"id": r["prefab_id"], "num": 0}} for r in removed] if category == "servant" else []
        frames = asset_frames(ctx, equip_list=equips, weapon_list=weapons)
    return {"uid": uid, "category": category, "deleted_count": len(removed),
            "skipped_count": len(ids) - len(removed), "sync": sync(uid, frames)}


def allowed_heroes():
    from gm_reader import GMReader
    return {hid for hid in GMReader.OFFICIAL_HERO_IDS if hero_codec.is_hero_payload_candidate(hid)}


def _backfill_portraits(db, uid, hero_ids, all_portraits=False):
    from peripheral_service import PeripheralService
    valid = PeripheralService.get_valid_decorations('portraits')
    if not valid:
        raise ValueError('缺少当前客户端头像目录，操作已回滚')
    if all_portraits:
        requested = valid
    else:
        from hero_service import _get_skin_portrait_map
        mapping = _get_skin_portrait_map()
        requested = {int(mapping.get(str(hid)) or 0) for hid in hero_ids} & valid
    owned = {int(row['item_id']) for row in db.query(
        "SELECT item_id FROM player_card WHERE uid=? AND kind='portrait' AND obtained=1", (uid,))}
    changed = sorted(requested - owned)
    for portrait in changed:
        db.execute("INSERT INTO player_card(uid,kind,item_id,obtained) VALUES(?,'portrait',?,1) "
                   "ON CONFLICT(uid,kind,item_id) DO UPDATE SET obtained=1", (uid, portrait))
    return changed


def unlock(db, uid, hero_id=None, all_heroes=False):
    allowed = allowed_heroes()
    requested = sorted(allowed) if all_heroes else [hero_id]
    if not requested or any(hid not in allowed for hid in requested):
        raise ValueError("角色不在当前可用角色目录中")
    frames, changed = [], []
    with db.transaction():
        for hid in requested:
            row = db.query("SELECT unlock FROM hero WHERE uid=? AND id=?", (uid, hid))
            if row and int(row[0].get("unlock") or 0):
                continue
            cfg = db.query("SELECT unlock_star FROM hero_cfg WHERE hero_id=?", (hid,))
            star = int(cfg[0].get("unlock_star") or 300) if cfg else 300
            skills = [[sid, 1] for sid in hero_codec._configured_hero_skills(hid)]
            db.execute("INSERT INTO hero(uid,id,level,star,exp,skill_list,unlock,update_ts) VALUES(?,?,1,?,0,?,1,?) "
                       "ON CONFLICT(uid,id) DO UPDATE SET unlock=1,update_ts=excluded.update_ts",
                       (uid, hid, star, json.dumps(skills), int(time.time())))
            frame = hero_codec.build_hero_14007_frame(db, uid, hid)
            if not frame:
                raise ValueError("无法生成角色同步数据，操作已回滚")
            frames.append(frame)
            changed.append(hid)
        # Also repair accounts that already unlocked the heroes with the old GM action.
        portraits = _backfill_portraits(db, uid, requested, all_portraits=all_heroes)
        if portraits:
            payload = generator.gen_payload(32009, uid=uid, db=db)
            if not payload:
                raise ValueError('无法生成头像同步数据，操作已回滚')
            frames.append(DownFrame(32009, payload))
    return {"uid": uid, "changed_heroes": changed, "unlocked_count": len(changed),
            "changed_portraits": portraits, "unlocked_portrait_count": len(portraits), "sync": sync(uid, frames)}


def graduate(db, uid, hero_id):
    if hero_id not in allowed_heroes():
        raise ValueError("角色不在当前可用角色目录中")
    from hero_service import HeroService, _get_weapon_module_max_levels
    with db.transaction():
        row = db.query("SELECT unlock,skill_list FROM hero WHERE uid=? AND id=?", (uid, hero_id))
        if not row or not row[0].get("unlock"):
            raise ValueError("请先解锁该角色")
        if not json.loads(row[0].get("skill_list") or "[]"):
            skills = [[sid, 1] for sid in hero_codec._configured_hero_skills(hero_id)]
            if not skills:
                raise ValueError("缺少该角色技能配置，不能执行满级操作")
            db.execute("UPDATE hero SET skill_list=? WHERE uid=? AND id=?", (json.dumps(skills), uid, hero_id))
        HeroService.get_instance(db=db).max_out_hero(uid, hero_id)
        module_level = _get_weapon_module_max_levels().get(hero_id, 0)
        db.execute("UPDATE hero SET weapon_module_level=?,module_level=? WHERE uid=? AND id=?",
                   (module_level, module_level, uid, hero_id))
        frame = hero_codec.build_hero_14007_frame(db, uid, hero_id)
        if not frame:
            raise ValueError("无法生成角色同步数据，操作已回滚")
    return {"uid": uid, "hero_id": hero_id, "level": 80, "grade": "Omega", "skills": 35,
            "weapon_level": 60, "module_level": module_level,
            "equipment_changed": False, "servant_changed": False, "sync": sync(uid, [frame])}
