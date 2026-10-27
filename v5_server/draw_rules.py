"""Validated, persistent GM draw rules. Existing rules remain the defaults."""
import copy
import json
import logging
import math
from decimal import Decimal, ROUND_CEILING

KEY = "gm_draw_rules_v1"
GROUPS = ("hero_precision_90", "hero_precision_70", "hero_standard_70", "weapon_servant_70")


def defaults(group):
    if group not in GROUPS:
        raise ValueError("未知卡池系列")
    return {
        "ssr_rate": 0.016,
        "soft_pity_start": 81 if group == "hero_precision_90" else 66,
        "hard_pity": 90 if group == "hero_precision_90" else 70,
        "sr_rate": 0.1108 if group == "weapon_servant_70" else 0.078,
        "sr_pity": 10,
        "up_rate": 0.5 if group == "hero_precision_70" else 1.0,
        "cost_multiplier": 1.0,
    }


def validate(group, values):
    result = defaults(group)
    if not isinstance(values, dict) or set(values) - set(result):
        raise ValueError("抽卡规则包含未知字段或格式错误")
    result.update(values)
    for name in ("hard_pity", "soft_pity_start", "sr_pity"):
        value = result[name]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000:
            raise ValueError(f"{name} 必须是 1~1000 的整数")
    if result["soft_pity_start"] > result["hard_pity"]:
        raise ValueError("软保底起点不能大于硬保底次数")
    for name in ("ssr_rate", "sr_rate", "up_rate", "cost_multiplier"):
        value = result[name]
        cap = 100 if name == "cost_multiplier" else 1
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= cap:
            raise ValueError(f"{name} 必须在 0~{cap} 之间")
        result[name] = float(value)
    if group != "hero_precision_70" and result["up_rate"] != 1:
        raise ValueError("仅扩充探测系列支持调整 UP 命中率")
    return result


def get_all(db=None):
    result = {group: defaults(group) for group in GROUPS}
    if db is None:
        return result
    try:
        rows = db.query("SELECT value FROM server_global_config WHERE key=?", (KEY,))
        saved = json.loads(rows[0]["value"]) if rows else {}
        if not isinstance(saved, dict):
            raise ValueError("规则存储格式错误")
        for group, values in saved.items():
            if group in result:
                result[group] = validate(group, values)
    except Exception as error:
        logging.getLogger(__name__).warning("读取自定义抽卡规则失败，使用有效默认值: %s", error)
    return result


def get_rules(db, group):
    if group not in GROUPS:
        raise ValueError("未知卡池系列")
    return get_all(db)[group]


def save(db, group, values=None, reset=False):
    defaults(group)  # Validate the key before starting a write transaction.
    if not reset and (not isinstance(values, dict) or not values):
        raise ValueError("请提供需要修改的抽卡规则")
    with db.transaction():
        current = get_all(db)
        current[group] = defaults(group) if reset else validate(group, {**current[group], **values})
        db.execute(
            "INSERT INTO server_global_config(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (KEY, json.dumps(current, ensure_ascii=False, allow_nan=False)),
        )
    return copy.deepcopy(current[group])


def ssr_probability(group, since_ssr, rules=None):
    rule = rules or defaults(group)
    base, start, hard = rule["ssr_rate"], rule["soft_pity_start"], rule["hard_pity"]
    if since_ssr >= hard:
        return 1.0
    if since_ssr < start:
        return base
    return min(1.0, base + (since_ssr - start + 1) * (1.0 - base) / (hard - start + 1))


def sr_probability(group, since_sr, rules=None):
    rule = rules or defaults(group)
    return 1.0 if since_sr >= rule["sr_pity"] else rule["sr_rate"]


def draw_cost(base_cost, rules):
    return int((Decimal(max(0, int(base_cost))) * Decimal(str(rules["cost_multiplier"]))).to_integral_value(rounding=ROUND_CEILING))
