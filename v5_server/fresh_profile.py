"""Explicit starter state, never a copy of the level-93 recording."""
import json


def is_fresh(db, uid):
    # Existing installations can use generator before the auth migration runs.
    if not db.query("SELECT 1 FROM sqlite_master WHERE name='auth_accounts'"):
        return False
    rows = db.query("SELECT fresh_profile FROM auth_accounts WHERE uid=?", (uid,))
    return bool(rows and rows[0]["fresh_profile"])


def initialize(db, uid, username, now, mode="full"):
    """Must run in the account-creation transaction. All missing progress stays empty."""
    db.execute("INSERT INTO users (uid,account,nick,level,exp,hero_num,portrait,register_ts) VALUES (?,?,?,1,0,1,1084,?)", (uid, username, username, now))
    db.execute("UPDATE users SET extra=? WHERE uid=?", (json.dumps({"total_login_days": 0}), uid))
    db.execute("INSERT INTO game_user (uid,board_hero,cur_portrait,show_hero,update_ts) VALUES (?,1084,1084,'[1084]',?)", (uid, now))
    db.executemany("INSERT INTO currency (uid,id,num) VALUES (?,?,?)",
                   [(uid, item, 100 if item == 4 else 0) for item in (1, 2, 4, 5, 12, 19, 30, 31, 32, 36, 38, 101, 53029, 53111)])
    # Explicit values matter: hero_codec treats NULL as 'inherit recorded template'.
    fields = {r["name"]: ("[]" if r["type"] == "TEXT" else 0) for r in db.query("PRAGMA table_info(hero)")}
    fields.update(uid=uid, id=1084, level=1, star=100, unlock=1, name="朝约", update_ts=now,
                  equip_slot="{}", chip_state="{}", trust_mood=1)
    db.execute("INSERT INTO hero (" + ",".join(fields) + ") VALUES (" + ",".join("?" for _ in fields) + ")", tuple(fields.values()))
    db.execute("INSERT INTO autochess_meta (uid,rank_score,energy,sunglasses) VALUES (?,0,0,0)", (uid,))
    db.execute("INSERT INTO backhome_canteen_meta (uid,canteen_id,accruing_earnings,update_ts) VALUES (?,4,0,?)", (uid, now))
    db.execute("INSERT INTO accumulate_sign (uid,version,open_sign,login_days) VALUES (?,2,1,0)", (uid,))
    db.execute("INSERT INTO mythic_final_progress (uid,now_difficulty,can_choose_list,update_ts) VALUES (?,1,'[1]',?)", (uid, now))
    db.get_or_create_newbie_activity(uid)
    db.execute("UPDATE newbie_activity SET max_phase=1 WHERE uid=?", (uid,))
    # First plot/assignment phase only. Periodic tasks are populated by the normal timer.
    from generator import _assignment_phases
    phases = _assignment_phases()
    for row in db.query("SELECT task_id,name,task_type FROM task_cfg WHERE task_type IN (3,4)"):
        phase = phases.get(int(row["task_id"]), (0, 0))[1]
        if phase == 1:
            db.execute("INSERT INTO task (uid,task_id,name,task_type) VALUES (?,?,?,?)",
                       (uid, row["task_id"], row["name"], row["task_type"]))
    if mode == "skip":
        db.executemany("INSERT INTO guide (uid,guide_id) VALUES (?,?)", [(uid, n) for n in range(501, 527)])


def task_phases(db, uid, tasks):
    from generator import _assignment_phases
    phases = _assignment_phases()
    ids = {int(t["task_id"]) for t in tasks}
    return {kind: max([v[1] for k, v in phases.items() if k in ids and v[0] == kind] or [1]) for kind in (3, 4)}


def ensure_resident_tasks(db, uid):
    """Client ChapterResidentTools requires vehicle-ball task records to exist.

    Unlike other resident activities, that branch reads GetTask(id).complete_flag
    without a nil check. Missing records abort sc_10201's login-completion event.
    These are uncompleted tasks, not rewards or unlocked main-story phases.
    """
    if not is_fresh(db, uid):
        return
    db.execute("""INSERT OR IGNORE INTO task
        (uid,task_id,name,task_type,progress,complete_flag,expired_ts,claimed_ts)
        SELECT ?,task_id,name,task_type,0,0,0,0 FROM task_cfg WHERE activity_id=4243101""", (uid,))


def advance_tasks(db, uid):
    """Unlock the next chapter/assignment phase after its summary reward is claimed."""
    if not is_fresh(db, uid):
        return
    from generator import _assignment_phases
    phases = _assignment_phases()
    rows = db.query("""SELECT c.*, COALESCE(t.claimed_ts,0) AS claimed,
        COALESCE(t.progress,0) AS progress, t.task_id AS owned
        FROM task_cfg c LEFT JOIN task t ON t.task_id=c.task_id AND t.uid=?""", (uid,))
    by_id = {r["task_id"]: r for r in rows}
    for kind in (3, 4):
        current = max([phases.get(r["task_id"], (0, 0))[1] for r in rows
                       if r["task_type"] == kind and r["owned"]] or [1])
        roots = []
        for r in rows:
            if not r["owned"] or r["condition"] != 500:
                continue
            params = json.loads(r.get("additional_parameter") or "[]")
            if params == [kind, current]:
                roots.append(r)
                children = [c for c in rows if c["task_id"] != r["task_id"]
                            and phases.get(c["task_id"]) == (kind, current)]
                progress = min(int(r["need"] or 0), sum(bool(c["claimed"]) for c in children))
                if progress > r["progress"]:
                    db.execute("UPDATE task SET progress=? WHERE uid=? AND task_id=?", (progress, uid, r["task_id"]))
        if roots and all(r["claimed"] for r in roots):
            for tid, phase in phases.items():
                row = by_id.get(tid)
                if phase == (kind, current + 1) and row and not row["owned"]:
                    db.execute("INSERT OR IGNORE INTO task (uid,task_id,name,task_type) VALUES (?,?,?,?)",
                               (uid, tid, row["name"], row["task_type"]))


def fallback_payload(db, uid, cmd):
    """Missing handlers cannot replay another player's state to fresh accounts."""
    import generator
    from schema_default import default_obj
    from codec import encode
    data = generator.gen_payload(cmd, uid=uid, db=db)
    if data is not None:
        return data
    return encode(f"sc_{cmd}", default_obj(f"sc_{cmd}", {"result": 1}))


def activity_definitions(db, stored_rows):
    """Public activity topology is required even when a new UID has no progress.

    The activity table is UID-keyed, but these fields describe schedules and
    parent/child relationships. Synthesizing only the parent with template=0
    leaves ChapterResidentTools indexing missing children at login completion.
    Copy only configuration fields from the shared protocol catalog; let any
    explicit per-account definitions override it.
    """
    rows = db.query("SELECT payload_json FROM login_push WHERE cmd=11001 LIMIT 1")
    if not rows or not rows[0]["payload_json"]:
        raise ValueError("缺少公共活动配置，无法初始化新号")
    catalog = json.loads(rows[0]["payload_json"])
    definitions = {}
    for entry in catalog.get("activity_list", []):
        activity_id = int(entry["activity_id"])
        record = {key: entry.get(key, 0) for key in
                  ("activity_id", "start_time", "stop_time", "state", "theme", "template")}
        record["sub_activity_id_list"] = json.dumps(entry.get("sub_activity_id_list", []))
        definitions[activity_id] = record
    for entry in stored_rows:
        definitions[int(entry["activity_id"])] = entry
    return list(definitions.values())


def resident_stage_payload(db, uid, data):
    """The small resident-chapter frames contain required zero-progress entries.

    They precede the large main-story frame and must retain their per-chapter
    grouping, even when their IDs also appear in the large frame. Replacing
    every group with that frame breaks the client's chapter initialization.
    """
    from stage_service import StageService
    from codec import encode
    service = StageService.get_instance(db=db)
    stages = []
    for entry in data.get("user_chapter_list", []):
        stage_id = int(entry["id"])
        progress = service.get_stage_info(uid, stage_id)
        stages.append({"id": stage_id, "clear_times": progress["clear_times"],
                       "star_list": progress["star_list"]})
    return encode("sc_24009", {"user_chapter_list": stages})


def static_payload(cmd, data):
    """Keep protocol structure/configuration identifiers, clear recorded personal state.

    These overrides are deliberately explicit. Unknown messages get schema defaults,
    never an old role's payload. Dynamic UID-aware generators run before this function.
    """
    from schema_default import default_obj
    from codec import encode
    # Public endpoint availability and resource configuration have no player progress.
    public = {12099, 12097, 28837, 68155, 40031, 40033, 40035, 48001, 11071}
    if cmd in public:
        return encode("sc_" + str(cmd), data)
    if cmd == 33001:
        # GuildAction.UpdateGuildPersonRedPoint indexes the boss configuration
        # during login even for players who have not unlocked guilds. A schema
        # default of boss_tempalte_id=0 aborts the client's login event loop.
        # Keep the public boss/affix/stage identities, never recorded progress.
        obj = default_obj("sc_33001")
        boss_id = int(data.get("info", {}).get("boss_tempalte_id") or 0)
        if boss_id <= 0:
            raise ValueError("sc_33001 缺少有效的公会首领配置")
        obj["info"].update(boss_tempalte_id=boss_id, difficulty=1)
        obj["affx_list"] = data.get("affx_list", [])
        obj["pre_info"] = [{"level_id": row["level_id"], "stage_id": row["stage_id"], "score": 0}
                           for row in data.get("pre_info", [])]
        return encode("sc_33001", obj)
    # Activity IDs identify a configuration, not ownership. Zero IDs crash some
    # client initializers. Retain just these keys, never scores/claims/unlocks.
    identifiers = {k: data[k] for k in ("activity_id", "template_id") if k in data}
    return encode("sc_" + str(cmd), default_obj("sc_" + str(cmd), identifiers))
