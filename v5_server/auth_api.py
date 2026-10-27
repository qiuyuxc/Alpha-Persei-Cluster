"""Web registration, owner setup, account administration, and SDK authentication."""
import collections
import json
import threading
import time

from flask import Blueprint, g, jsonify, request
from auth_service import AuthError, decode_sdk_password, get_service


def bearer():
    value = request.headers.get("Authorization", "")
    return value[7:] if value.startswith("Bearer ") else ""


def sdk_token(values):
    return bearer() or request.headers.get("token", "") or values.get("token", "")


def sdk_user(account, token, login_type="username"):
    return {"userId": account["uid"], "nickName": account["username"], "phone": account["username"],
            "regionNo": "1", "token": token, "valid": False, "locked": False,
            "birth": -225014400000, "gender": "male", "newMsgCount": 0,
            "totalRecharge": 0, "totalRechargeDis": "0.00", "realNameValid": True,
            "realNameValidResult": "succ", "adult": True, "realNameUpdateLimit": 0,
            "guest": False, "privList": [], "privPermList": [], "inviteLimit": False,
            "age": 25, "registerFlag": False, "birthday": "19990101",
            "securityValid": False, "loginType": login_type}


def install(app, db):
    auth = get_service(db)
    app.extensions["local_auth"] = auth
    bp = Blueprint("local_auth", __name__)
    attempts = collections.OrderedDict()
    lock = threading.Lock()

    def limit(action, count=30, seconds=300):
        key = (request.remote_addr, action)
        now = time.monotonic()
        with lock:
            recent = [t for t in attempts.get(key, []) if t > now - seconds]
            if len(recent) >= count:
                raise AuthError("操作频繁，请稍后再试", 429)
            attempts[key] = recent + [now]
            attempts.move_to_end(key)
            while len(attempts) > 4096:
                attempts.popitem(last=False)

    def values():
        if (request.content_length or 0) > 16384:
            raise AuthError("请求过大", 413)
        data = request.get_json(silent=True) if request.is_json else request.form.to_dict()
        if not isinstance(data, dict):
            raise AuthError("请求格式错误")
        return data

    def ok(data=None):
        return jsonify(code=0, msg="成功", data=data)

    @bp.get("/status")
    def status():
        ready = auth.ready()
        return ok({"ready": ready, "registration_open": ready, "game_auto_registration": ready,
                   "tutorial_mode": db.get_system_config("auth_new_tutorial_mode", "full")})

    @bp.post("/register")
    def register():
        limit("register", 5, 3600)
        data = values()
        return ok(auth.create(data.get("username"), data.get("password")))

    @bp.post("/setup")
    def setup():
        limit("setup", 10, 3600)
        data = values()
        if not data.get("setup_token"):
            raise AuthError("缺少管理员设置凭证", 403)
        return ok(auth.create(data.get("username"), data.get("password"), data["setup_token"]))

    @bp.post("/login")
    def login():
        limit("login")
        data = values()
        return ok(auth.login(data.get("username"), data.get("password")))

    @bp.get("/me")
    def me():
        return ok(auth.public(auth.verify(bearer(), {"web"})))

    @bp.post("/logout")
    def logout():
        auth.logout(bearer())
        return ok()

    @bp.post("/password")
    def password():
        limit("password", 10)
        data = values()
        account = auth.verify(bearer(), {"web"})
        auth.change_password(account["uid"], data.get("password"), data.get("old_password"))
        return ok()

    @app.get("/api/gm/accounts")
    def accounts():
        return ok(db.query("""SELECT u.uid,u.nick,u.level,a.username,a.is_admin,a.disabled,
            a.fresh_profile,a.tutorial_mode,a.created_at,a.last_login_at
            FROM users u LEFT JOIN auth_accounts a ON a.uid=u.uid ORDER BY a.is_admin DESC,u.uid"""))

    @app.post("/api/gm/accounts/password")
    def reset_password():
        data = values()
        auth.change_password(int(data.get("uid", 0)), data.get("password"), admin=True)
        return ok()

    @app.post("/api/gm/accounts/disabled")
    def disabled():
        data = values()
        if not isinstance(data.get("disabled"), bool):
            raise AuthError("disabled 必须为布尔值")
        auth.set_disabled(int(data.get("uid", 0)), data["disabled"])
        return ok()

    @app.before_request
    def protect_and_handle_sdk():
        path = request.path
        if path.startswith("/api/") and not path.startswith("/api/auth/"):
            if request.method == "OPTIONS":
                return "", 204
            if path == "/api/system/res_version" and request.method == "GET":
                return None
            account = auth.verify(bearer(), {"web"})
            if not account["is_admin"]:
                raise AuthError("需要管理员账号", 403)
            g.account = account
        # Intercept all legacy authentication routes, including their old aliases.
        if "/pass/user/" not in path and path not in ("/sdk-api/auth/user/getInfo", "/sdk-api/pass/sms/send"):
            return None
        endpoint = path.rsplit("/", 1)[-1]
        supported = {"login", "loginByToken", "getInfo"}
        if endpoint not in supported:
            raise AuthError("请使用账号密码登录，新账号首次登录自动注册；也可打开 /web/account.html 注册。短信登录未启用")
        if request.method != "POST" and endpoint != "getInfo":
            raise AuthError("请使用 POST 登录", 405)
        data = values()
        if endpoint == "getInfo":
            token = sdk_token(data)
            account = auth.verify(token, {"sdk"})
            result = sdk_user(account, token)
        elif "mix-sdk-api" in path:
            limit("mix", 120)
            try:
                info = data.get("channelLoginRespInfo", {})
                info = json.loads(info) if isinstance(info, str) else info
                info = info.get("data", info)
                account = auth.verify(info.get("token"), {"sdk"})
            except (ValueError, AttributeError, TypeError):
                raise AuthError("登录凭证无效，请重新登录", 401) from None
            token = auth.issue(account["uid"], "mix")
            result = {"id": account["uid"], "createDate": account["created_at"] * 1000,
                      "modifyDate": int(time.time() * 1000), "gameId": 3, "channelId": 9,
                      "channelUserId": str(account["uid"]), "token": token,
                      "expireDate": int(time.time() + auth.TTL["mix"]) * 1000,
                      "enableLogin": True, "enablePay": False, "enableNewRole": True,
                      "totalRecharge": 0, "registerFlag": False, "inviteLimit": False, "targetChannelId": 7}
        elif endpoint == "loginByToken":
            token = sdk_token(data)
            account = auth.verify(token, {"sdk"})
            result = sdk_user(account, token, "token")
        else:
            limit("login")
            password = decode_sdk_password(data.get("password", ""), str(data.get("encode", "")).lower() == "true")
            account = auth.login_or_register_sdk(data.get("username"), password,
                before_register=lambda: limit("register", 5, 3600))
            result = sdk_user(account, account["token"])
        return jsonify(errorCode="0", data=result)

    @app.errorhandler(AuthError)
    def auth_error(error):
        if not request.path.startswith("/api/"):
            # The mobile SDK expects application errors in an HTTP 200 envelope.
            return jsonify(errorCode=str(error.status), errorMsg=str(error), data={})
        return jsonify(code=error.status, msg=str(error), data=None), error.status

    @app.after_request
    def private_responses(response):
        if request.path.startswith("/api/") or "/pass/user/" in request.path or "/auth/user/" in request.path:
            response.headers["Cache-Control"] = "no-store"
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    app.register_blueprint(bp, url_prefix="/api/auth")
    return auth
