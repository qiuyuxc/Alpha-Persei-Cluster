# -*- coding: utf-8 -*-
"""
mail_service.py — V5 服务端统一邮件与信件收藏室领域服务 (Unified Mail Service)

核心职责：
1. 统一管理收件箱（Inbox）、收藏箱（Collect Box）、信件收藏室（Special Letters）；
2. 邮件附件领取全量接入统一资产中心 InventoryService，规范入库货币、材料、刻印、钥从、换装（含重复分解）、场景、头像框并派发 Events；
3. 严格内化历史避坑规约：
   - 新邮件默认有效 30 天，尊重自定义截止时间，过期邮件不可领取；
   - 详情一律 mail_template_id=0 直出 content_list（保底非空），防止客户端 unpack(nil) 崩溃或死循环请求；
   - content_list 严格遵循 protobuf schema 规范为 repeated content_info (text, content_type=2)；
4. 根治角色生日信件红点死灰复燃 BUG：
   - 建立 信件ID <-> 修正者英雄ID 索引矩阵；
   - 阅读角色生日信件时联动将该角色所有年度信件标记已读，彻底消除角色头像与邮件主入口的常驻红点；
   - 支持全量信件一键已读与自动初始化，解决 175 封全量历史信件导致的红点泛滥；
5. 向战令、商城、活动等全服模块提供标准规范的外部发信 API (send_mail / send_system_mail)。
"""

import os
import json
import time
import logging
from inventory_service import InventoryService
from event_bus import bus, Events
from operation_notifications import checkpoint_notifications, restore_notifications
from account_db import savepoint_transaction
from game_cycles import integer

logger = logging.getLogger("mail_service")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SPECIAL_LETTERS_PATH = os.path.join(BASE_DIR, "special_letters.json")
ARCHIVE_LETTERS_PATH = os.path.join(BASE_DIR, "mail_archive", "special_letters.json")
TEMPLATES_PATH = os.path.join(BASE_DIR, "mail_archive", "mail_templates.json")

# 远未来过期时间戳常量（2033-05-18 11:33:20），防止客户端 timeout_timestamp <= server_time 触发 MAIL_EXPIRED 弹窗
DEFAULT_FAR_FUTURE_EXPIRE = 2000000000
DEFAULT_VALID_DAYS = 30


def resolve_expiry(expire_time=None, valid_days=None, now_ts=None):
    now = int(time.time()) if now_ts is None else int(now_ts)
    if expire_time is not None and valid_days is not None:
        raise ValueError('有效天数和截止时间只能填写一项')
    if expire_time is not None:
        return integer(expire_time, '邮件截止时间', now + 1, 4294967295)
    days = integer(DEFAULT_VALID_DAYS if valid_days is None else valid_days, '邮件有效天数', 1, 3650)
    return now + days * 86400


def normalize_attachments(attachments, db=None):
    if not isinstance(attachments, list) or len(attachments) > 100:
        raise ValueError('附件须为数组，最多 100 项')
    normalized = []
    for value in attachments:
        if isinstance(value, dict):
            aid = value.get('id', value.get('item_id'))
            count = value.get('number', value.get('num', value.get('count', value.get('item_num'))))
            name = str(value.get('name') or '')
        elif isinstance(value, (list, tuple)) and len(value) == 2:
            aid, count = value
            name = ''
        else:
            raise ValueError('附件须填写物品编号和数量')
        aid = integer(aid, '附件物品编号', 1)
        count = integer(count, '附件数量', 1, 999999999)
        if 200000 <= aid <= 600000 or 830000 <= aid <= 859999:
            raise ValueError(f'物品 {aid} 属于刻印或套装，禁止邮件发送')
        if aid == 30054 or 1000000000 <= aid <= 2000000000:
            raise ValueError(f'物品 {aid} 属于试衣底片，禁止邮件发送')
        if db is not None:
            from gm_actions import require_item
            require_item(db, aid, count)
            if not name:
                name = db.query('SELECT name FROM item_catalog WHERE id=?', (aid,))[0]['name']
        normalized.append({'id': aid, 'number': count, 'name': name})
    return normalized


class MailService:
    """统一邮件与信件收藏室领域服务"""

    _instance = None
    _SPECIAL_LETTERS_CACHE = None
    _LETTER_BY_ID = None
    _LETTERS_BY_HERO = None
    _TEMPLATES_CACHE = None
    _TEMPLATES_WITH_PARAM_SET = None
    _TEMPLATES_WARNING_EMITTED = False

    def __init__(self, db=None):
        self.db = db
        self._ensure_loaded_configs()

    @classmethod
    def get_instance(cls, db=None):
        """单例入口"""
        if cls._instance is None:
            cls._instance = cls(db=db)
        elif db is not None:
            cls._instance.db = db
        return cls._instance

    def _get_db(self, db=None):
        return db or self.db

    @classmethod
    def _ensure_loaded_configs(cls):
        """加载并缓存 175 封修正者信件与官方 333 套邮件模板"""
        if cls._SPECIAL_LETTERS_CACHE is None:
            path = SPECIAL_LETTERS_PATH if os.path.exists(SPECIAL_LETTERS_PATH) else ARCHIVE_LETTERS_PATH
            letters = []
            if os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        letters = json.load(f)
                except Exception as e:
                    logger.error(f"加载 special_letters.json 失败: {e}")
            cls._SPECIAL_LETTERS_CACHE = letters
            cls._LETTER_BY_ID = {int(x["id"]): x for x in letters}
            hero_map = {}
            for x in letters:
                hid = int(x.get("hero_id") or 0)
                if hid > 0:
                    hero_map.setdefault(hid, []).append(int(x["id"]))
            cls._LETTERS_BY_HERO = hero_map
            logger.info(f"MailService 载入信件收藏室: {len(letters)} 封信件, 涉及 {len(hero_map)} 位修正者")

        if cls._TEMPLATES_CACHE is None:
            templates = []
            param_set = set()
            if os.path.exists(TEMPLATES_PATH):
                try:
                    with open(TEMPLATES_PATH, "r", encoding="utf-8") as f:
                        templates = json.load(f)
                    for t in templates:
                        t_title = t.get("title") or t.get("mail_title") or ""
                        if "%s" in t_title or "%d" in t_title:
                            param_set.add(int(t["id"]))
                except Exception as e:
                    if not cls._TEMPLATES_WARNING_EMITTED:
                        logger.warning(f"加载 mail_templates.json 失败，邮件列表继续使用字面标题: {e}")
                        cls._TEMPLATES_WARNING_EMITTED = True
            elif not cls._TEMPLATES_WARNING_EMITTED:
                logger.warning("mail_templates.json 不存在，邮件列表继续使用字面标题")
                cls._TEMPLATES_WARNING_EMITTED = True
            cls._TEMPLATES_CACHE = templates
            cls._TEMPLATES_WITH_PARAM_SET = param_set

    # =========================================================================
    #  一、普通收件箱与收藏邮件业务
    # =========================================================================

    def get_login_summary(self, uid, db=None):
        """获取登录摘要 sc_30001: {unread_number, total_number}（MAIL_UNREAD 红点数据源）"""
        _db = self._get_db(db)
        if not _db:
            return {"unread_number": 0, "total_number": 0}
        rows = _db.query(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN read_flag=1 THEN 1 ELSE 0 END) AS unread "
            "FROM mail WHERE uid=? AND mail_type=1 AND (COALESCE(expire_time,0)=0 OR expire_time>?)",
            (uid, int(time.time()))
        )
        total = int(rows[0]["total"] or 0) if rows else 0
        unread = int(rows[0]["unread"] or 0) if rows else 0
        return {"unread_number": unread, "total_number": total}

    def get_inbox_list(self, uid, db=None):
        """获取有效普通收件箱邮件列表（sc_30003），保留真实截止时间。"""
        _db = self._get_db(db)
        if not _db:
            return []
        rows = _db.query(
            "SELECT * FROM mail WHERE uid=? AND mail_type=1 AND (COALESCE(expire_time,0)=0 OR expire_time>?) ORDER BY send_time DESC",
            (uid, int(time.time()))
        )
        lst = []
        for r in rows:
            attachment_list = []
            a_raw = r.get("attachment_json") or ""
            if a_raw:
                try:
                    raw_atts = json.loads(a_raw)
                    for item in raw_atts:
                        if isinstance(item, dict):
                            aid = int(item.get("id") or item.get("item_id") or 0)
                            anum = int(item.get("number") or item.get("num") or item.get("count") or item.get("item_num") or 0)
                        elif isinstance(item, (list, tuple)) and len(item) >= 2:
                            aid, anum = int(item[0]), int(item[1])
                        else:
                            continue
                        if aid > 0 and anum > 0:
                            attachment_list.append({"id": aid, "number": anum})
                except Exception:
                    pass

            exp = int(r.get("expire_time") or 0)
            if exp == 0:
                exp = DEFAULT_FAR_FUTURE_EXPIRE

            attach_flag = int(r.get("attach_flag") or 2)
            if not attachment_list:
                attach_flag = 2

            lst.append({
                "id": int(r["mail_id"]),
                "date": int(r.get("send_time") or 0),
                "title": str(r.get("title") or ""),
                "attach_flag": attach_flag,
                "read_flag": int(r.get("read_flag") or 1),
                "attachment_list": attachment_list,
                "timeout_timestamp": exp,
                "star_state": int(r.get("star_state") or 0),
                # `title` is already rendered and title_format is empty; always bypass
                # client template formatting to prevent string.format on missing args.
                "mail_template_id": 0,
                "title_format": [],
                "i18n_info": []
            })
        return lst

    def get_collect_list(self, uid, db=None):
        """获取收藏邮件列表（sc_30021）。"""
        _db = self._get_db(db)
        if not _db:
            return []
        rows = _db.query(
            "SELECT * FROM mail WHERE uid=? AND (mail_type=2 OR star_state=1) ORDER BY send_time DESC",
            (uid,)
        )
        lst = []
        for r in rows:
            lst.append({
                "id": int(r["mail_id"]),
                "collect_date": int(r.get("update_ts") or r.get("send_time") or 0),
                "title": str(r.get("title") or ""),
                "mail_template_id": 0,
                "title_format": [],
                "i18n_info": [],
                "mail_timestamp": int(r.get("send_time") or 0)
            })
        return lst

    def get_mail_detail(self, uid, mail_id, db=None):
        """查看邮件详情（sc_30009 / sc_30023）。
        核心避坑：
        1. 强制将 mail_template_id 设为 0 直出 content_list，防止客户端 unpack(sender_format) 崩溃；
        2. content_list 必须是 repeated content_info [{'text': ..., 'content_type': 2}]，保底至少 1 行；
        3. 自动将该邮件标记为已读 read_flag=2。"""
        _db = self._get_db(db)
        if not _db:
            return None
        rows = _db.query("SELECT * FROM mail WHERE uid=? AND mail_id=?", (uid, int(mail_id)))
        if not rows:
            return None
        r = rows[0]
        expired = 0 < int(r.get('expire_time') or 0) <= int(time.time())
        if expired and int(r.get('mail_type') or 1) == 1:
            return None

        # 解析正文内容（转为 content_info 结构）
        content_list = []
        c_raw = r.get("content_json") or ""
        if c_raw:
            try:
                parsed = json.loads(c_raw)
                if isinstance(parsed, dict) and "content" in parsed:
                    content_list.append({"text": str(parsed["content"]), "content_type": 2})
                elif isinstance(parsed, list):
                    for x in parsed:
                        if isinstance(x, dict) and "text" in x:
                            content_list.append(x)
                        elif isinstance(x, str) and x.strip():
                            content_list.append({"text": x.strip(), "content_type": 2})
                elif isinstance(parsed, str) and parsed.strip():
                    content_list.append({"text": parsed.strip(), "content_type": 2})
            except Exception:
                pass

        if not content_list:
            fallback_text = (r.get("title") or "亲爱的管理员，感谢您的支持与陪伴！").strip()
            content_list = [{"text": fallback_text, "content_type": 2}]
        elif len(content_list) == 1 and not (content_list[0].get("text") or "").strip():
            content_list[0]["text"] = "亲爱的管理员，感谢您的支持与陪伴！"

        # 解析附件列表
        attachments = []
        a_raw = r.get("attachment_json") or ""
        if a_raw:
            try:
                raw_atts = json.loads(a_raw)
                for item in raw_atts:
                    if isinstance(item, dict):
                        aid = int(item.get("id") or item.get("item_id") or 0)
                        num = int(item.get("number") or item.get("num") or item.get("count") or item.get("item_num") or 0)
                    elif isinstance(item, (list, tuple)) and len(item) >= 2:
                        aid, num = int(item[0]), int(item[1])
                    else:
                        continue
                    if aid > 0 and num > 0:
                        attachments.append({"id": aid, "number": num})
            except Exception:
                pass

        if expired:
            attachments = []

        # 阅读邮件自动标记已读
        now_ts = int(time.time())
        _db.execute(
            "UPDATE mail SET read_flag=2, update_ts=? WHERE uid=? AND mail_id=?",
            (now_ts, uid, int(mail_id))
        )

        return {
            "id": int(r["mail_id"]),
            "content_list": content_list,
            "attachment_list": attachments,
            "sender": str(r.get("sender") or "隐科组总务部"),
            "mail_template_id": 0,  # 强制为 0 直出正文，防 unpack(nil) 崩溃
            "sender_format": [],
            "content_format": [],
            "i18n_info": [],
            "link_param": [],
            # 兼容字段
            "title": str(r.get("title") or ""),
            "date": int(r.get("send_time") or 0),
            "attach_flag": 2 if not attachments else int(r.get("attach_flag") or 1),
            "read_flag": 2,
            "star_state": int(r.get("star_state") or 0)
        }

    def claim_mail_attachments(self, uid, mail_id=0, ctx=None, db=None):
        """领取邮件附件（sc_30005）。
        全量接入统一资产中心 InventoryService：
        - 无论是货币、材料、刻印、钥从、换装（含重复自动分解 1680 移转之辉）、场景、气泡、头像框等，全部规范持久化并派发事件；
        - 更新对应邮件 attach_flag=2, read_flag=2。
        返回附件、成功邮件ID及需要刷新的英雄/换装/场景。
        独立调用自带事务；Core内嵌套时异常必须继续传播到外层，不能吞错后提交。"""
        _db = (ctx.db if ctx and hasattr(ctx, "db") and ctx.db else None) or self._get_db(db)
        if not _db:
            raise ValueError("领取邮件附件缺少数据库")
        checkpoint = checkpoint_notifications(ctx) if ctx is not None else None
        try:
            with _db.transaction():
                return self._claim_mail_attachments(uid, mail_id, ctx, _db)
        except BaseException:
            if checkpoint is not None:
                restore_notifications(ctx, checkpoint)
            raise

    def _claim_mail_attachments(self, uid, mail_id, ctx, _db):
        """调用方持有事务：只选待领邮件，完整发奖后才确认领取事实。"""
        mid = int(mail_id or 0)
        now_ts = int(time.time())
        if mid > 0:
            rows = _db.query("SELECT * FROM mail WHERE uid=? AND mail_id=? AND attach_flag=1", (uid, mid))
            if rows and 0 < int(rows[0].get('expire_time') or 0) <= now_ts:
                raise ValueError('邮件已过期，无法领取附件')
        else:
            rows = _db.query("SELECT * FROM mail WHERE uid=? AND attach_flag=1 AND (COALESCE(expire_time,0)=0 OR expire_time>?)", (uid, now_ts))

        all_items_to_add = []
        claimed_attachments = []
        success_mail_ids = []

        for m in rows:
            m_id = int(m["mail_id"])
            attach_list = []
            attach_list = json.loads(m.get("attachment_json") or "[]")
            if not isinstance(attach_list, list):
                raise ValueError(f"邮件 {m_id} 附件必须是列表")

            for it in attach_list:
                if isinstance(it, dict):
                    iid = int(it.get("item_id") or it.get("id") or 0)
                    num = int(it.get("count") or it.get("number") or it.get("num") or it.get("item_num") or 0)
                elif isinstance(it, (list, tuple)) and len(it) >= 2:
                    iid, num = int(it[0]), int(it[1])
                else:
                    raise ValueError(f"邮件 {m_id} 含无效附件")
                if iid <= 0 or num <= 0:
                    raise ValueError(f"邮件 {m_id} 附件ID与数量必须为正数")

                all_items_to_add.append((iid, num))
                claimed_attachments.append({"id": iid, "number": num})

            success_mail_ids.append(m_id)

        # 统一转交 InventoryService 进行批量发奖与事件广播
        summary = {}
        if all_items_to_add:
            summary = InventoryService.grant_items(ctx or _db, uid, all_items_to_add, source="mail")

        for m_id in success_mail_ids:
            changed = _db.execute(
                "UPDATE mail SET attach_flag=2, read_flag=2, update_ts=? WHERE uid=? AND mail_id=? AND attach_flag=1",
                (now_ts, uid, m_id)
            )
            if changed.rowcount != 1:
                raise ValueError(f"邮件 {m_id} 领取状态发生变化")

        touched_heroes = list(set(summary.get("hero_unlocked") or []))
        touched_scene = bool(summary.get("scenes"))

        logger.info(f"uid={uid} 成功领取邮件附件: 邮件数={len(success_mail_ids)} 道具数={len(claimed_attachments)}")
        return {
            "attachment_list": claimed_attachments,
            "success_mail_ids": success_mail_ids,
            "touched_heroes": touched_heroes,
            "touched_skins": list(set(summary.get("skins") or [])),
            "touched_scene": touched_scene
        }

    def delete_mails(self, uid, mail_id=0, db=None):
        """删除邮件（sc_30007）。
        mail_id=0: 删除所有已读且无待领附件的邮件；
        mail_id>0: 删除指定邮件。"""
        _db = self._get_db(db)
        if not _db:
            return []
        mid = int(mail_id or 0)
        if mid > 0:
            rows = _db.query("SELECT mail_id FROM mail WHERE uid=? AND mail_id=?", (uid, mid))
        else:
            rows = _db.query("SELECT mail_id FROM mail WHERE uid=? AND read_flag=2 AND attach_flag!=1", (uid,))
        deleted_ids = [r["mail_id"] for r in rows]
        if deleted_ids:
            ph = ",".join(["?"] * len(deleted_ids))
            _db.execute(f"DELETE FROM mail WHERE uid=? AND mail_id IN ({ph})", [uid] + deleted_ids)
        return deleted_ids

    def toggle_collect(self, uid, mail_id, opt, db=None):
        """收藏 / 取消收藏邮件（sc_30015）。opt: 1=收藏, 2=取消收藏"""
        _db = self._get_db(db)
        if not _db:
            return {"mail_id": mail_id, "opt": opt, "collect_mail": None}
        star = 1 if int(opt) == 1 else 0
        m_type = 2 if int(opt) == 1 else 1
        now_ts = int(time.time())
        _db.execute(
            "UPDATE mail SET star_state=?, mail_type=?, update_ts=? WHERE uid=? AND mail_id=?",
            (star, m_type, now_ts, uid, int(mail_id))
        )
        d = self.get_mail_detail(uid, mail_id, db=_db)
        collect_info = None
        if d and int(opt) == 1:
            collect_info = {
                "id": int(d["id"]),
                "collect_date": now_ts,
                "title": d.get("title") or "",
                "mail_template_id": d.get("mail_template_id") or 0,
                "title_format": [],
                "i18n_info": [],
                "mail_timestamp": d.get("date") or 0
            }
        return {"mail_id": mail_id, "opt": opt, "collect_mail": collect_info}

    def send_mail(self, uid, title, content, attachments=None, sender="隐科组总务部", template_id=0, expire_time=None, db=None, now_ts=None):
        """全服通用发信接口（支持业务模块与控制台一键发信）。
        - 自动计算下一封递增 mail_id；
        - 默认有效 30 天；序号与投递在同一事务中确认。"""
        _db = self._get_db(db)
        if not _db:
            return 0
        now_ts = int(time.time()) if now_ts is None else int(now_ts)
        exp_ts = resolve_expiry(expire_time=expire_time, now_ts=now_ts)
        norm_attachments = [{'id': it['id'], 'number': it['number']}
                            for it in normalize_attachments(attachments or [])]
        with savepoint_transaction(_db, immediate=True):
            _db.execute('CREATE TABLE IF NOT EXISTS mail_sequence(uid INTEGER PRIMARY KEY,last_id INTEGER NOT NULL)')
            rows = _db.query('SELECT MAX(mail_id) AS max_id FROM mail WHERE uid=?', (uid,))
            next_id = max(1000, int(rows[0]['max_id'] or 0)) + 1
            _db.execute('INSERT INTO mail_sequence(uid,last_id) VALUES(?,?) ON CONFLICT(uid) DO UPDATE SET last_id=MAX(mail_sequence.last_id+1,excluded.last_id)', (uid, next_id))
            new_mail_id = int(_db.query('SELECT last_id FROM mail_sequence WHERE uid=?', (uid,))[0]['last_id'])
            _db.execute(
                """INSERT INTO mail (uid,mail_id,mail_type,template_id,title,sender,send_time,expire_time,
                    read_flag,attach_flag,content_json,attachment_json,star_state,update_ts)
                    VALUES(?,?,1,?,?,?,?,?,1,?,?,?,0,?)""",
                (uid, new_mail_id, int(template_id or 0), str(title), str(sender), now_ts, exp_ts,
                 1 if norm_attachments else 2,
                 json.dumps([content] if isinstance(content, str) else list(content), ensure_ascii=False),
                 json.dumps(norm_attachments, ensure_ascii=False), now_ts))
        logger.info('邮件已入库 uid=%s mail_id=%s', uid, new_mail_id)
        return new_mail_id

    # =========================================================================
    #  二、修正者专属信件与生日档案红点消解核心业务
    # =========================================================================

    def get_special_letters(self, uid, db=None):
        """获取修正者专属信件列表（sc_30017）。
        精准下发 175 封信件的真实阅读状态，若已读则 is_viewed=True，从根本上防止虚假红点。"""
        _db = self._get_db(db)
        all_letters = self._SPECIAL_LETTERS_CACHE or []
        view_map = {}
        if _db:
            rows = _db.query("SELECT letter_id, is_viewed FROM letter_special WHERE uid=?", (uid,))
            view_map = {int(r["letter_id"]): bool(r["is_viewed"]) for r in rows}
            from fresh_profile import is_fresh
            if is_fresh(_db, uid):
                return [{"id": lid, "is_viewed": viewed} for lid, viewed in view_map.items()]

        result = []
        for l in all_letters:
            lid = int(l["id"])
            is_v = view_map.get(lid, False)
            result.append({"id": lid, "is_viewed": is_v})
        return result

    def read_special_letter(self, uid, letter_id, db=None):
        """标记修正者信件为已读（cs_30018 -> sc_30019）。
        核心修复：
        1. letter_id == 0 时，一键将该玩家名下所有 175 封信件全部置为已读；
        2. letter_id > 0 时，查出该信件所属修正者 hero_id，将该角色名下的全部年度生日信件一并置为已读！
           原理：客户端 RedPointConst.LETTER_SENDER_ID..hero_id 是该角色全部年度信件红点的父节点（OR 关系），
           只要有一封历史信件未读，角色头像红点就永远消不掉。同角色联动已读能彻底终结此顽疾！"""
        _db = self._get_db(db)
        if not _db:
            return {"id": letter_id}

        lid = int(letter_id or 0)
        now_ts = int(time.time())

        if lid == 0:
            # 一键全量已读
            self.mark_all_special_letters_read(uid, db=_db)
            return {"id": 0}

        # 查出待标记为已读的信件列表（包含同英雄的所有信件）
        target_ids = {lid}
        letter_meta = self._LETTER_BY_ID.get(lid) if self._LETTER_BY_ID else None
        if letter_meta:
            hid = int(letter_meta.get("hero_id") or 0)
            if hid > 0 and self._LETTERS_BY_HERO and hid in self._LETTERS_BY_HERO:
                target_ids.update(self._LETTERS_BY_HERO[hid])

        for tid in target_ids:
            meta = self._LETTER_BY_ID.get(tid, {}) if self._LETTER_BY_ID else {}
            hid = int(meta.get("hero_id") or 0)
            _db.execute(
                "INSERT INTO letter_special (uid, letter_id, hero_id, is_viewed, update_ts) VALUES (?, ?, ?, 1, ?) "
                "ON CONFLICT(uid, letter_id) DO UPDATE SET is_viewed=1, hero_id=excluded.hero_id, update_ts=excluded.update_ts",
                (uid, tid, hid, now_ts)
            )

        logger.info(f"uid={uid} 阅读信件 id={lid}，已联动将相关信件设为已读: {target_ids}")
        return {"id": lid}

    def mark_all_special_letters_read(self, uid, db=None):
        """一键将 175 封全量官方信件同步为已读状态。
        用于解决离线服务批量注入全部历史信件后导致的 59 位角色头像红点大泛滥。"""
        _db = self._get_db(db)
        if not _db or not self._SPECIAL_LETTERS_CACHE:
            return 0
        now_ts = int(time.time())
        count = 0
        for l in self._SPECIAL_LETTERS_CACHE:
            lid = int(l["id"])
            hid = int(l.get("hero_id") or 0)
            _db.execute(
                "INSERT INTO letter_special (uid, letter_id, hero_id, is_viewed, update_ts) VALUES (?, ?, ?, 1, ?) "
                "ON CONFLICT(uid, letter_id) DO UPDATE SET is_viewed=1, hero_id=excluded.hero_id, update_ts=excluded.update_ts",
                (uid, lid, hid, now_ts)
            )
            count += 1
        logger.info(f"uid={uid} 成功将全量 {count} 封修正者生日信件同步为已读状态")
        return count
