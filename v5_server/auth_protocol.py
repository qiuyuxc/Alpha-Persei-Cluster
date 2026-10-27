"""Authentication shared by TCP handshakes and the repeated login frames."""
import hashlib
import time

from auth_service import AuthError, get_service


def is_new_player(db, account):
    return int(bool(account["fresh_profile"] and account["tutorial_mode"] == "full"
        and not db.query("SELECT 1 FROM chapter WHERE uid=? AND clear_times>0 LIMIT 1", (account["uid"],))))


def gateway(db, data, ip, port):
    auth = get_service(db)
    account = auth.verify(data.get("token"), {"sdk", "mix"})
    role = db.get("users", account["uid"])
    return {"result": 0, "server_id": int(role.get("server_id") or 2),
            "ip": ip, "port": port, "user_id": account["uid"],
            "gstoken": auth.issue(account["uid"], "game"), "timestamp": int(time.time()),
            "is_new_player": is_new_player(db, account)}


def game(db, data):
    try:
        uid = int(data.get("user_id", 0))
    except (ValueError, TypeError):
        raise AuthError("无效的角色", 401) from None
    account = get_service(db).verify(data.get("gstoken"), {"game"}, uid=uid)
    role = db.get("users", uid)
    acknowledgement = {"result": 0, "register_timestamp": int(role.get("register_ts") or account["created_at"]),
                       "timestamp": int(time.time()), "verify_timestamp": 316800,
                       "uid_sign": hashlib.sha256(data["gstoken"].encode()).hexdigest()[:32]}
    return account, acknowledgement
