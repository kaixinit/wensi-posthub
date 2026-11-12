from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
import tempfile
from contextlib import contextmanager, closing

from cryptography.fernet import Fernet
from filelock import FileLock


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600_000)
    return salt + ":" + hashed.hex()


def password_matches(password: str, saved: str) -> bool:
    return hmac.compare_digest(password_hash(password, saved.split(":")[0]), saved)


def initialize(directory: Path, password: str, *, must_change_password=False) -> None:
    if not 12 <= len(password) <= 256:
        raise ValueError("管理员密码需要 12–256 个字符")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "config.json"
    if path.exists():
        raise ValueError("已经初始化；修改密码请使用 reset-password")
    config = {"username": "admin", "must_change_password": must_change_password, "password_hash": password_hash(password), "encryption_key": Fernet.generate_key().decode()}
    # Exclusive creation prevents accidental replacement of the encryption key.
    with path.open("x", encoding="utf-8") as stream:
        json.dump(config, stream)
    os.chmod(path, 0o600)


SCHEMA = """
CREATE TABLE IF NOT EXISTS targets (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, config TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1, enabled INTEGER NOT NULL DEFAULT 1,
 verified INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL,
 verify_hash TEXT, verify_expires REAL, verify_attempts INTEGER NOT NULL DEFAULT 0,
 binding_hash TEXT, binding_expires REAL, deleted REAL
);
CREATE TABLE IF NOT EXISTS apps (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
 allowed TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL,
 default_profile_id TEXT, deleted REAL,
 require_ack INTEGER NOT NULL DEFAULT 0, ack_minutes INTEGER NOT NULL DEFAULT 30,
 route_error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS profiles (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, targets TEXT NOT NULL,
 require_ack INTEGER NOT NULL DEFAULT 0, ack_minutes INTEGER NOT NULL DEFAULT 30
);
CREATE TABLE IF NOT EXISTS messages (
 id TEXT PRIMARY KEY, app_id TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
 title TEXT NOT NULL, body TEXT NOT NULL, created REAL NOT NULL, scheduled REAL NOT NULL,
 expires REAL NOT NULL, require_ack INTEGER NOT NULL, ack_minutes INTEGER NOT NULL,
 purpose TEXT NOT NULL DEFAULT 'notification', UNIQUE(app_id, request_id)
);
CREATE TABLE IF NOT EXISTS deliveries (
 id TEXT PRIMARY KEY, message_id TEXT NOT NULL REFERENCES messages(id),
 target_id TEXT NOT NULL REFERENCES targets(id), revision INTEGER NOT NULL,
 state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
 next_at REAL NOT NULL, started REAL, completed REAL, accepted_at REAL, acked_at REAL,
 ack_hash TEXT, ack_secret TEXT, ack_deadline REAL, last_error TEXT NOT NULL DEFAULT '',
 provider_id TEXT NOT NULL DEFAULT '', UNIQUE(message_id, target_id)
);
CREATE TABLE IF NOT EXISTS message_routes (
 message_id TEXT PRIMARY KEY REFERENCES messages(id),
 state TEXT NOT NULL DEFAULT 'awaiting_routing',
 require_ack INTEGER, ack_minutes INTEGER
);
CREATE INDEX IF NOT EXISTS route_waiting ON message_routes(state);
CREATE INDEX IF NOT EXISTS message_app ON messages(app_id, created);
CREATE INDEX IF NOT EXISTS delivery_ready ON deliveries(state, next_at);
CREATE TABLE IF NOT EXISTS weixin_context_waits (
 delivery_id TEXT PRIMARY KEY REFERENCES deliveries(id),
 context_hash TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, delivery_id TEXT NOT NULL REFERENCES deliveries(id),
 at REAL NOT NULL, state TEXT NOT NULL, detail TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
 id TEXT PRIMARY KEY, delivery_id TEXT NOT NULL REFERENCES deliveries(id),
 reason TEXT NOT NULL, detail TEXT NOT NULL, created REAL NOT NULL,
 resolved INTEGER NOT NULL DEFAULT 0, notified INTEGER NOT NULL DEFAULT 0,
 UNIQUE(delivery_id, reason)
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires REAL NOT NULL, auth_version TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS limits (key TEXT PRIMARY KEY, count INTEGER NOT NULL, resets REAL NOT NULL);
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, action TEXT NOT NULL, subject TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS qq_conversations (
 id TEXT PRIMARY KEY, app_id TEXT NOT NULL, credential_hash TEXT NOT NULL,
 target_type TEXT NOT NULL, details TEXT NOT NULL, last_seen REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS qq_conversation_recent ON qq_conversations(app_id,last_seen);
CREATE TABLE IF NOT EXISTS network_proxies (
 id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, config TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS qq_network_routes (app_id TEXT PRIMARY KEY, config TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS network_diagnostics (id TEXT PRIMARY KEY, result TEXT NOT NULL, tested REAL NOT NULL);
"""


class Store:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory).resolve()
        self.config_path = self.directory / "config.json"
        if not self.config_path.exists():
            raise RuntimeError("请先运行 python -m posthub init")
        config = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.cipher = Fernet(config["encryption_key"].encode())
        self.path = self.directory / "posthub.sqlite3"
        with closing(sqlite3.connect(self.path, timeout=15, isolation_level=None)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > 10:
                raise RuntimeError("数据库版本高于当前程序；请使用匹配的新版程序")
            conn.executescript(SCHEMA)
            # Additive migration preserves every existing target, key and record.
            conn.execute("BEGIN IMMEDIATE")
            try:
                for table, column, kind in [("targets", "deleted", "REAL"), ("apps", "default_profile_id", "TEXT"), ("apps", "deleted", "REAL"), ("apps", "require_ack", "INTEGER NOT NULL DEFAULT 0"), ("apps", "ack_minutes", "INTEGER NOT NULL DEFAULT 30"), ("apps", "route_error", "TEXT NOT NULL DEFAULT ''")]:
                    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                    if column not in columns:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
                if version < 5:
                    # Copy each legacy route once. Never expand its recipients to
                    # the whole old authorization list or follow a profile again.
                    for app_id, allowed, profile_id in conn.execute("SELECT id,allowed,default_profile_id FROM apps WHERE default_profile_id IS NOT NULL").fetchall():
                        profile = conn.execute("SELECT targets,require_ack,ack_minutes FROM profiles WHERE id=?", (profile_id,)).fetchone()
                        error = "旧方案无法完整转换，请在下游配置中核对渠道并保存"
                        try:
                            ids = json.loads(profile[0]) if profile else None
                            valid = isinstance(ids, list) and 0 < len(ids) <= 50 and all(isinstance(i, str) for i in ids) and set(ids) <= set(json.loads(allowed))
                        except (ValueError, TypeError):
                            valid = False
                        if valid:
                            conn.execute("UPDATE apps SET allowed=?,require_ack=?,ack_minutes=?,route_error='' WHERE id=?", (json.dumps(list(dict.fromkeys(ids))), bool(profile[1]), max(1, min(10080, int(profile[2]))), app_id))
                        else:
                            conn.execute("UPDATE apps SET route_error=? WHERE id=?", (error, app_id))
                        conn.execute("UPDATE apps SET default_profile_id=NULL WHERE id=?", (app_id,))
                        conn.execute("INSERT INTO audit(at,action,subject) VALUES (?,'app.independent_downstream',?)", (time.time(), app_id))
                if version < 6:
                    self._migrate_network(conn)
                if version < 7:
                    if 'auth_version' not in {row[1] for row in conn.execute('PRAGMA table_info(sessions)')}:
                        conn.execute("ALTER TABLE sessions ADD COLUMN auth_version TEXT NOT NULL DEFAULT ''")
                    conn.execute("UPDATE sessions SET auth_version=? WHERE auth_version=''", (digest(config['password_hash']),))
                if version < 8:
                    self._resume_limited_retries(conn)
                conn.execute("PRAGMA user_version = 10")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        os.chmod(self.path, 0o600)

    def _resume_limited_retries(self, conn):
        """Resume only safe failures stopped by the old three-attempt limit."""
        now = time.time()
        rows = conn.execute("""
            SELECT d.id FROM deliveries d
            JOIN messages m ON m.id=d.message_id JOIN targets t ON t.id=d.target_id
            WHERE d.state='failed' AND d.last_error LIKE '已达到 3 次尝试上限：%'
              AND d.accepted_at IS NULL AND d.acked_at IS NULL AND m.expires>?
              AND t.deleted IS NULL AND t.enabled=1 AND t.revision=d.revision
              AND ((m.purpose!='verification' AND t.verified=1) OR
                   (m.purpose='verification' AND t.verify_hash IS NOT NULL AND t.verify_expires>?))
              AND (m.app_id='admin' OR EXISTS (SELECT 1 FROM apps a WHERE a.id=m.app_id AND a.deleted IS NULL))
              AND NOT EXISTS (SELECT 1 FROM attempts h WHERE h.delivery_id=d.id
                              AND h.state IN ('accepted','acknowledged','unknown'))
        """, (now, now)).fetchall()
        for (delivery_id,) in rows:
            detail = "已升级为自动重试：原任务因 3 次尝试上限暂停，将继续尝试至投递截止时间"
            conn.execute("UPDATE deliveries SET state='retry',next_at=?,completed=NULL,last_error=? WHERE id=?", (now, detail, delivery_id))
            conn.execute("UPDATE alerts SET resolved=1 WHERE delivery_id=? AND reason='failed'", (delivery_id,))
            conn.execute("INSERT INTO attempts(delivery_id,at,state,detail) VALUES (?,?,'retry',?)", (delivery_id, now, detail))
            conn.execute("INSERT INTO audit(at,action,subject) VALUES (?,'delivery.resume_retry_limit',?)", (now, delivery_id))

    def account(self):
        config = json.loads(self.config_path.read_text(encoding='utf-8'))
        return {'username': config.get('username', 'admin'), 'must_change_password': bool(config.get('must_change_password', False))}

    def change_password(self, new_password, *, current_password=None, require_current=True):
        if not 12 <= len(new_password) <= 256:
            raise ValueError('新密码需要 12–256 个字符')
        with FileLock(str(self.directory / 'account.lock'), timeout=5):
            config = json.loads(self.config_path.read_text(encoding='utf-8'))
            if require_current and (current_password is None or not password_matches(current_password, config['password_hash'])):
                raise ValueError('当前密码不正确')
            if password_matches(new_password, config['password_hash']):
                raise ValueError('新密码不能与当前密码相同')
            config.update(username=config.get('username', 'admin'), password_hash=password_hash(new_password), must_change_password=False)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',prefix='account-',suffix='.tmp',dir=self.directory,delete=False) as stream:
                    temporary = Path(stream.name)
                    os.chmod(temporary, 0o600)
                    json.dump(config, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.config_path)
            finally:
                if temporary and temporary.exists():
                    temporary.unlink()
            # auth_version also rejects old sessions if a crash occurs before deletion.
            with self.connect(True) as conn:
                conn.execute('DELETE FROM sessions')
                conn.execute("INSERT INTO audit(at,action,subject) VALUES (?,'account.password.change',?)", (time.time(), config['username']))
        return {'ok': True, 'relogin_required': True}

    def _migrate_network(self, conn):
        """Move legacy proxy secrets once, without touching recipient verification."""
        from uuid import uuid4
        from urllib.parse import urlsplit
        def add(name, url):
            # Older versions allowed omitted ports; make those defaults explicit.
            p = urlsplit(url)
            if p.port is None:
                url = f"{p.scheme}://{p.netloc}:{443 if p.scheme == 'https' else 1080 if p.scheme.startswith('socks') else 80}"
            proxy_id = uuid4().hex
            conn.execute("INSERT INTO network_proxies VALUES (?,?,?,?,?)", (proxy_id, name, self.encrypt({"url": url}), 1, time.time()))
            return proxy_id
        row = conn.execute("SELECT value FROM settings WHERE key='default_proxy'").fetchone()
        if row and json.loads(row[0]):
            proxy_id = add("原默认代理", self.decrypt(json.loads(row[0])))
            conn.execute("INSERT OR REPLACE INTO settings VALUES ('default_proxy_id',?)", (json.dumps(proxy_id),))
            conn.execute("UPDATE settings SET value=? WHERE key='default_proxy'", (json.dumps(""),))
        for target_id, name, encrypted in conn.execute("SELECT id,name,config FROM targets WHERE deleted IS NULL ORDER BY created,id").fetchall():
            config = self.decrypt(encrypted)
            if config.get('network_mode') == 'custom' and config.get('proxy'):
                config['proxy_id'] = add(f"原单独代理 · {name[:40]} · {target_id[:6]}", config.pop('proxy'))
                config['network_mode'] = 'profile'
                conn.execute("UPDATE targets SET config=? WHERE id=?", (self.encrypt(config), target_id))
        # Pin the previously active bot route, including across enable/disable changes.
        routes = {}
        for target_id, encrypted in conn.execute("SELECT id,config FROM targets WHERE kind='qq' AND deleted IS NULL ORDER BY enabled DESC,created,rowid").fetchall():
            config = self.decrypt(encrypted)
            app_id = config['app_id']
            route = {k: v for k, v in config.items() if k in {'network_mode', 'proxy_id', 'proxy'}}
            route.setdefault('network_mode', 'default')
            if app_id not in routes:
                routes[app_id] = route
                conn.execute("INSERT OR IGNORE INTO qq_network_routes VALUES (?,?)", (app_id, self.encrypt(route)))
            if route != routes[app_id]:
                config = {k: v for k, v in config.items() if k not in {'network_mode', 'proxy_id', 'proxy'}} | routes[app_id]
                conn.execute("UPDATE targets SET config=? WHERE id=?", (self.encrypt(config), target_id))

    @contextmanager
    def connect(self, write=False):
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        try:
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def encrypt(self, value) -> str:
        return self.cipher.encrypt(json.dumps(value, ensure_ascii=False).encode()).decode()

    def decrypt(self, value: str):
        return json.loads(self.cipher.decrypt(value.encode()))

    def setting(self, key: str, default=""):
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key, value):
        with self.connect(True) as conn:
            conn.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def limited(self, key: str, maximum: int, seconds: int, now: float) -> bool:
        with self.connect(True) as conn:
            conn.execute("DELETE FROM limits WHERE resets<?", (now,))
            conn.execute("INSERT INTO limits VALUES (?,0,?) ON CONFLICT DO NOTHING", (key, now + seconds))
            conn.execute("UPDATE limits SET count=count+1 WHERE key=?", (key,))
            return conn.execute("SELECT count FROM limits WHERE key=?", (key,)).fetchone()[0] > maximum

    def audit(self, action, subject, now):
        with self.connect(True) as conn:
            conn.execute("INSERT INTO audit(at,action,subject) VALUES (?,?,?)", (now, action, subject))
