"""Web registration, owner setup, account administration, and SDK authentication."""
import collections
import json
import threading
import time

from flask import Blueprint, g, jsonify, request
from auth_service import AuthError, decode_sdk_password, get_service
import local_registration


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
    local_registration.initialize(db)
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

    def registration_operation(operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except AuthError:
            raise
        except Exception:
            app.logger.exception('Local registration operation failed')
            raise AuthError('验证码注册服务异常，请稍后再试', 500) from None

    @bp.get("/status")
    def status():
        ready = auth.ready()
        return ok({"ready": ready, "registration_open": ready, "game_auto_registration": ready,
                   "local_code_registration": ready and local_registration.enabled(db),
                   "registration_code_digits": local_registration.CODE_DIGITS,
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
        from server_net import game_presence
        presence = game_presence()
        accounts = db.query("""SELECT u.uid,u.nick,u.level,a.username,a.is_admin,a.disabled,
            a.fresh_profile,a.tutorial_mode,a.created_at,a.last_login_at
            FROM users u LEFT JOIN auth_accounts a ON a.uid=u.uid ORDER BY a.is_admin DESC,u.uid""")
        for account in accounts:
            account.update(presence.get(int(account['uid']), {'online': False,
                'connection_count': 0, 'ready': False, 'connected_at': 0, 'last_seen_at': 0}))
        return ok(accounts)

    def managed_uid(data):
        from game_cycles import integer
        try:
            return integer(data.get('uid'), '玩家 UID', 1, 9223372036854775807)
        except ValueError as error:
            raise AuthError(str(error)) from error

    @app.post('/api/gm/accounts/kick')
    def kick():
        from server_net import kick_game_connections
        uid = managed_uid(values())
        revoked = auth.revoke_game_sessions(uid)
        return ok({**kick_game_connections(uid), 'revoked_tokens': revoked})

    @app.post("/api/gm/accounts/password")
    def reset_password():
        data = values()
        uid = managed_uid(data)
        auth.change_password(uid, data.get("password"), admin=True)
        from server_net import kick_game_connections
        return ok(kick_game_connections(uid))

    @app.post("/api/gm/accounts/disabled")
    def disabled():
        data = values()
        if not isinstance(data.get("disabled"), bool):
            raise AuthError("disabled 必须为布尔值")
        uid = managed_uid(data)
        auth.set_disabled(uid, data["disabled"])
        from server_net import kick_game_connections
        return ok(kick_game_connections(uid) if data['disabled'] else {'clients': 0})

    @app.before_request
    def protect_and_handle_sdk():
        path = request.path
        if path.startswith("/api/") and not path.startswith("/api/auth/"):
            if request.method == "OPTIONS":
                return "", 204
            if path == "/api/system/res_version" and request.method == "GET":
                return None
            account = auth.verify(bearer(), {"web"})
            if account.get('password_change_required'):
                raise AuthError('请先在子 GM 设置新密码', 403)
            if not account["is_admin"] and not path.startswith('/api/sub-gm/'):
                raise AuthError("需要管理员账号", 403)
            g.account = account
        # Intercept all legacy authentication routes, including their old aliases.
        sms_send = path.endswith('/pass/sms/send')
        if "/pass/user/" not in path and path != "/sdk-api/auth/user/getInfo" and not sms_send:
            return None
        if sms_send:
            if request.method not in ('GET', 'POST'):
                raise AuthError('请使用 GET 或 POST 获取注册码', 405)
            data = request.args.to_dict() if request.method == 'GET' else values()
            purpose = data.get('operType', 'login')
            if purpose not in ('login', 'regist') or 'mix-sdk-api' in path:
                raise AuthError('本地验证码仅用于新账号注册；已有账号请使用密码登录', 403)
            limit('registration_code', 10, 300)
            code = registration_operation(local_registration.issue, auth, data.get('phone'), request.remote_addr)
            instruction = '填写后点登录即可注册。' if purpose == 'login' else '请切换「验证码登录」填写。'
            return jsonify(errorCode=local_registration.CODE_TOAST,
                           errorMsg=f'注册码：{code}（5分钟有效）。{instruction}',
                           data={'localRegistration': True, 'expiresIn': local_registration.CODE_TTL})
        endpoint = path.rsplit("/", 1)[-1]
        supported = {"login", "loginByToken", "getInfo", "loginBySms", "registerBySms"}
        if endpoint not in supported:
            raise AuthError("请使用账号密码登录，新账号首次登录自动注册；也可打开 /web/account.html 注册。短信登录未启用")
        if request.method != "POST" and endpoint != "getInfo":
            raise AuthError("请使用 POST 登录", 405)
        data = values()
        if endpoint in ('loginBySms', 'registerBySms'):
            if 'mix-sdk-api' in path:
                raise AuthError('请使用游戏内验证码入口注册新账号', 403)
            limit('registration_verify')
            account = registration_operation(local_registration.register, auth, data.get('phone'), data.get('authCode'),
                request.remote_addr, before_create=lambda: limit('register', 5, 3600))
            return jsonify(errorCode=local_registration.REGISTERED_TOAST,
                           errorMsg='注册成功！请用注册码登录子 GM 修改密码，再用新密码登录游戏。',
                           data={'localRegistration': True, 'registrationComplete': True,
                                 'username': account['username'], 'passwordChangeRequired': True,
                                 'passwordChangeUrl': '/web/sub_gm.html'})
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
        if request.path.startswith("/api/") or "/pass/user/" in request.path or "/auth/user/" in request.path or '/pass/sms/' in request.path:
            response.headers["Cache-Control"] = "no-store"
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    app.register_blueprint(bp, url_prefix="/api/auth")
    import sub_gm_api
    sub_gm_api.install(app, db, auth)
    return auth
