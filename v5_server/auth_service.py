"""Local accounts and scoped, revocable sessions. No template UID fallback."""
import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager


class AuthError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def decode_sdk_password(value, encoded=False):
    """Android Functions.encodePassword: MD5[:8].upper() + UTF-8 base64."""
    if not isinstance(value, str) or len(value) > 512:
        raise AuthError("账号或密码错误", 401)
    if not encoded:
        return value
    try:
        raw = base64.b64decode(value[8:], validate=True)
        password = raw.decode("utf-8")
        if not hmac.compare_digest(hashlib.md5(raw).hexdigest()[:8].upper(), value[:8]):
            raise ValueError()
        return password
    except (ValueError, UnicodeError):
        raise AuthError("账号或密码错误", 401) from None


def password_hash(password):
    if not isinstance(password, str) or not 8 <= len(password) <= 64:
        raise AuthError("密码须为 8–64 个字符")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return "scrypt$16384$" + salt.hex() + "$" + digest.hex()


def check_password(password, stored):
    try:
        _, n, salt, expected = stored.split("$")
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=8, p=1)
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, AttributeError, TypeError):
        return False


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class AuthService:
    TTL = {"web": 43200, "sdk": 2592000, "mix": 86400, "game": 900}

    def __init__(self, db):
        self.db = db
        for sql in (
            """CREATE TABLE IF NOT EXISTS auth_accounts (
                uid INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0,
                disabled INTEGER NOT NULL DEFAULT 0, token_version INTEGER NOT NULL DEFAULT 1,
                fresh_profile INTEGER NOT NULL DEFAULT 1, tutorial_mode TEXT NOT NULL DEFAULT 'full',
                created_at INTEGER NOT NULL, last_login_at INTEGER NOT NULL DEFAULT 0)""",
            """CREATE TABLE IF NOT EXISTS auth_sessions (
                token_hash TEXT PRIMARY KEY, uid INTEGER NOT NULL, kind TEXT NOT NULL,
                expires_at INTEGER NOT NULL, token_version INTEGER NOT NULL)""",
            "CREATE INDEX IF NOT EXISTS auth_sessions_uid ON auth_sessions(uid)",
            """CREATE TABLE IF NOT EXISTS auth_bootstrap (
                id INTEGER PRIMARY KEY CHECK (id=1), token_hash TEXT NOT NULL,
                uid INTEGER NOT NULL, expires_at INTEGER NOT NULL)""",
        ):
            db.execute(sql)
        self._dummy_hash = password_hash(secrets.token_urlsafe(24))

    @contextmanager
    def transaction(self):
        # Reserve the writer before reading uniqueness/bootstrap state. DEFERRED
        # transactions can otherwise fail when simultaneous signups upgrade locks.
        if self.db._in_txn():
            yield
            return
        conn = self.db._checked_conn()
        conn.execute("BEGIN IMMEDIATE")
        self.db._local.in_txn = True
        try:
            yield
            conn.commit()
        except BaseException:
            self.db._rollback_or_discard(conn)
            raise
        finally:
            self.db._local.in_txn = False

    def ready(self):
        return bool(self.db.query("SELECT 1 FROM auth_accounts WHERE is_admin=1 AND disabled=0 LIMIT 1"))

    def bootstrap_token(self, uid):
        """Called by the owner on the server console, never by a public endpoint."""
        token = secrets.token_urlsafe(32)
        with self.transaction():
            if self.ready():
                raise AuthError("管理员已设置")
            if not self.db.query("SELECT 1 FROM users WHERE uid=?", (uid,)):
                raise AuthError("要保留的存档不存在")
            self.db.execute("INSERT OR REPLACE INTO auth_bootstrap VALUES (1,?,?,?)",
                            (token_hash(token), uid, int(time.time()) + 7 * 86400))
        return token

    @staticmethod
    def username(value):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{6,16}", value):
            raise AuthError("账号须为 6–16 位数字（无需真实手机号）")
        return value.lower()

    @staticmethod
    def public(account):
        return {k: account[k] for k in ("uid", "username", "is_admin", "disabled", "fresh_profile", "tutorial_mode", "created_at")}

    def create(self, username, password, setup_token=None):
        username = self.username(username)
        digest = password_hash(password)
        now = int(time.time())
        try:
            with self.transaction():
                if setup_token:
                    rows = self.db.query("SELECT * FROM auth_bootstrap WHERE id=1")
                    if (self.ready() or not rows or rows[0]["expires_at"] <= now
                            or not hmac.compare_digest(rows[0]["token_hash"], token_hash(str(setup_token)))):
                        raise AuthError("设置链接无效或已使用", 403)
                    uid, admin, fresh = rows[0]["uid"], 1, 0
                else:
                    if not self.ready():
                        raise AuthError("服务器正在设置管理员，请稍后注册", 503)
                    uid, admin, fresh = 0, 0, 1
                    while not uid or self.db.query("SELECT 1 FROM users WHERE uid=?", (uid,)):
                        uid = 100000000 + secrets.randbelow(1900000000)
                mode = self.db.get_system_config("auth_new_tutorial_mode", "full")
                mode = mode if mode in ("full", "skip") else "full"
                self.db.execute("INSERT INTO auth_accounts (uid,username,password_hash,is_admin,fresh_profile,tutorial_mode,created_at) VALUES (?,?,?,?,?,?,?)",
                                (uid, username, digest, admin, fresh, mode, now))
                if fresh:
                    from fresh_profile import initialize
                    initialize(self.db, uid, username, now, mode)
                else:
                    # Bind credentials only. Preserve the existing role and all its assets.
                    self.db.execute("DELETE FROM auth_bootstrap WHERE id=1")
            return self.public(self.account(uid))
        except sqlite3.IntegrityError as error:
            if "auth_accounts.username" in str(error):
                raise AuthError("账号已存在") from None
            raise

    def account(self, uid):
        rows = self.db.query("SELECT * FROM auth_accounts WHERE uid=?", (uid,))
        if not rows or rows[0]["disabled"]:
            raise AuthError("账号不可用", 401)
        return rows[0]

    def login(self, username, password, kind="web"):
        # Uniform failure and KDF cost also for unknown names.
        rows = self.db.query("SELECT * FROM auth_accounts WHERE username=? COLLATE NOCASE", (str(username)[:128],))
        valid = check_password(password, rows[0]["password_hash"] if rows else self._dummy_hash)
        if not valid or not rows or rows[0]["disabled"]:
            raise AuthError("账号或密码错误", 401)
        account = rows[0]
        self.db.execute("UPDATE auth_accounts SET last_login_at=? WHERE uid=?", (int(time.time()), account["uid"]))
        return {**self.public(account), "token": self.issue(account["uid"], kind)}

    def login_or_register_sdk(self, username, password, before_register=None):
        """Register a new name on its first password SDK login, then sign in.

        Existing accounts, including disabled ones, always use password checks.
        Reserve the writer and recheck new names so concurrent first logins
        create one profile; initialization and its SDK session commit together.
        """
        query = "SELECT uid FROM auth_accounts WHERE username=? COLLATE NOCASE"
        if self.db.query(query, (str(username)[:128],)):
            return self.login(username, password, "sdk")
        username = self.username(username)
        with self.transaction():
            if not self.db.query(query, (username,)):
                if before_register is not None:
                    before_register()
                self.create(username, password)
            return self.login(username, password, "sdk")

    def issue(self, uid, kind):
        ttl = self.TTL[kind]
        token = secrets.token_urlsafe(32) + "." + str(uid)
        with self.transaction():
            account = self.account(uid)
            self.db.execute("DELETE FROM auth_sessions WHERE expires_at<=?", (int(time.time()),))
            self.db.execute("INSERT INTO auth_sessions VALUES (?,?,?,?,?)",
                            (token_hash(token), uid, kind, int(time.time()) + ttl, account["token_version"]))
        return token

    def verify(self, token, kinds, uid=None):
        if not isinstance(token, str) or not 30 <= len(token) <= 256:
            raise AuthError("登录已失效，请重新登录", 401)
        rows = self.db.query("""SELECT a.*, s.kind, s.expires_at FROM auth_sessions s
            JOIN auth_accounts a ON a.uid=s.uid AND a.token_version=s.token_version
            WHERE s.token_hash=? AND s.expires_at>? AND a.disabled=0""", (token_hash(token), int(time.time())))
        if not rows or rows[0]["kind"] not in kinds or (uid is not None and rows[0]["uid"] != int(uid)):
            raise AuthError("登录已失效，请重新登录", 401)
        return rows[0]

    def logout(self, token):
        self.db.execute("DELETE FROM auth_sessions WHERE token_hash=?", (token_hash(token),))

    def change_password(self, uid, password, old_password=None, admin=False):
        digest = password_hash(password)
        with self.transaction():
            account = self.account(uid)
            if not admin and not check_password(old_password, account["password_hash"]):
                raise AuthError("原密码错误", 401)
            self.db.execute("UPDATE auth_accounts SET password_hash=?, token_version=token_version+1 WHERE uid=?", (digest, uid))
            self.db.execute("DELETE FROM auth_sessions WHERE uid=?", (uid,))

    def set_disabled(self, uid, disabled):
        with self.transaction():
            rows = self.db.query("SELECT * FROM auth_accounts WHERE uid=?", (uid,))
            if not rows:
                raise AuthError("账号不存在", 404)
            if rows[0]["is_admin"]:
                raise AuthError("不能停用管理员账号")
            self.db.execute("UPDATE auth_accounts SET disabled=?, token_version=token_version+1 WHERE uid=?", (int(disabled), uid))
            self.db.execute("DELETE FROM auth_sessions WHERE uid=?", (uid,))

    def connection_valid(self, uid, version):
        return bool(self.db.query("SELECT 1 FROM auth_accounts WHERE uid=? AND token_version=? AND disabled=0", (uid, version)))


_lock = threading.Lock()


def get_service(db):
    with _lock:
        if not hasattr(db, "_auth_service"):
            db._auth_service = AuthService(db)
        return db._auth_service
