"""One-time, locally displayed registration codes for new numeric accounts."""
import hashlib
import hmac
import re
import secrets
import time

from auth_service import AuthError

KEY = 'auth_local_code_registration'
CODE_DIGITS = 8
CODE_TTL = 300
RESEND_SECONDS = 60
MAX_ATTEMPTS = 5
CODE_TOAST = '49001'
REGISTERED_TOAST = '49002'


def enabled(db):
    return str(db.get_system_config(KEY, '1')).lower() in ('1', 'true', 'enabled')


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS auth_registration_codes (
        username TEXT PRIMARY KEY,peer TEXT NOT NULL,salt TEXT NOT NULL,
        code_hash TEXT NOT NULL,created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,resend_at INTEGER NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0)''')


def require_enabled(auth):
    if not enabled(auth.db):
        raise AuthError('本地验证码注册当前未开启，请使用账号密码注册', 403)


def code_hash(salt, code):
    return hashlib.sha256((salt + ':' + code).encode()).hexdigest()


def require_new_account(auth, username):
    if auth.db.query('SELECT 1 FROM auth_accounts WHERE username=? COLLATE NOCASE', (username,)):
        raise AuthError('账号已存在，请使用账号密码登录；忘记密码请联系管理员', 409)


def issue(auth, phone, peer):
    require_enabled(auth)
    username = auth.username(phone)
    now = int(time.time())
    with auth.transaction():
        if not auth.ready():
            raise AuthError('服务器正在设置管理员，请稍后注册', 503)
        require_new_account(auth, username)
        rows = auth.db.query('SELECT resend_at FROM auth_registration_codes WHERE username=?', (username,))
        if rows and rows[0]['resend_at'] > now:
            raise AuthError(f"请在 {rows[0]['resend_at'] - now} 秒后重新获取注册码", 429)
        code = str(10000000 + secrets.randbelow(90000000))
        salt = secrets.token_hex(16)
        auth.db.execute('DELETE FROM auth_registration_codes WHERE expires_at<?', (now - 3600,))
        auth.db.execute('''INSERT INTO auth_registration_codes
            (username,peer,salt,code_hash,created_at,expires_at,resend_at,attempts)
            VALUES(?,?,?,?,?,?,?,0) ON CONFLICT(username) DO UPDATE SET
            peer=excluded.peer,salt=excluded.salt,code_hash=excluded.code_hash,
            created_at=excluded.created_at,expires_at=excluded.expires_at,
            resend_at=excluded.resend_at,attempts=0''',
            (username, str(peer), salt, code_hash(salt, code), now, now + CODE_TTL, now + RESEND_SECONDS))
    return code


def register(auth, phone, code, peer, before_create=None):
    require_enabled(auth)
    username = auth.username(phone)
    now = int(time.time())
    failure = None
    account = None
    with auth.transaction():
        require_new_account(auth, username)
        rows = auth.db.query('SELECT * FROM auth_registration_codes WHERE username=?', (username,))
        if not rows or rows[0]['peer'] != str(peer):
            raise AuthError('注册码无效，请重新获取', 400)
        row = rows[0]
        if row['expires_at'] <= now:
            raise AuthError('注册码已过期，请重新获取', 400)
        if row['attempts'] >= MAX_ATTEMPTS:
            raise AuthError('注册码尝试次数过多，请重新获取', 429)
        valid_format = isinstance(code, str) and re.fullmatch(r'[0-9]{8}', code)
        if not valid_format or not hmac.compare_digest(code_hash(row['salt'], code), row['code_hash']):
            attempts = row['attempts'] + 1
            auth.db.execute('UPDATE auth_registration_codes SET attempts=? WHERE username=?', (attempts, username))
            failure = AuthError('注册码尝试次数过多，请重新获取' if attempts >= MAX_ATTEMPTS
                                else '注册码错误，请检查后重试', 429 if attempts >= MAX_ATTEMPTS else 400)
        else:
            if before_create is not None:
                before_create()
            created = auth.create(username, code)
            auth.db.execute('UPDATE auth_accounts SET password_change_required=1 WHERE uid=?', (created['uid'],))
            auth.db.execute('DELETE FROM auth_registration_codes WHERE username=?', (username,))
            account = auth.public(auth.account(created['uid']))
    # Failed attempts must commit even though the HTTP request reports an error.
    if failure is not None:
        raise failure
    return account
