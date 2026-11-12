"""Explicit outbound routes. Diagnostics never send notifications or change routes."""
from __future__ import annotations

import hashlib
import json
import smtplib
import socket
import ssl
import time
from urllib.parse import quote, unquote, urlsplit
from uuid import uuid4

from . import adapters

ROUTE_FIELDS = {"network_mode", "proxy", "proxy_id"}
PROBES = {
    "qq": "https://api.bot.qq.com/", "weixin": adapters.ILINK + "/",
    "wecom": "https://qyapi.weixin.qq.com/", "feishu": "https://open.feishu.cn/",
    "dingtalk": "https://api.dingtalk.com/",
}


def validate_proxy(url):
    try:
        p = urlsplit(url)
        if (p.scheme not in {"http", "https", "socks5", "socks5h"} or not p.hostname
                or p.port is None or not 1 <= p.port <= 65535 or p.path not in {"", "/"}
                or p.query or p.fragment or any(ord(c) <= 32 or ord(c) == 127 for c in url)):
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError("代理地址需包含 HTTP(S) / SOCKS5 协议、主机和有效端口，不包含路径或参数") from None
    return url


def parts(url):
    p = urlsplit(url)
    return {"scheme": p.scheme, "host": p.hostname, "port": p.port,
            "username": unquote(p.username or ""), "has_password": bool(p.password)}


def spec(config):
    mode = config.get("network_mode", "default")
    return {"network_mode": mode, **({"proxy_id": config.get("proxy_id", "")} if mode == "profile" else {}),
            **({"proxy": config.get("proxy", "")} if mode == "custom" else {})}


def error_info(error):
    """Classify by exception types, never serialize URLs, credentials or provider text."""
    chain, item = [], error
    while item and len(chain) < 10 and item not in chain:
        chain.append(item)
        item = item.__cause__ or item.__context__
    if any(isinstance(e, ssl.SSLCertVerificationError) for e in chain):
        return "tls", "TLS 证书校验失败", "检查系统时间与服务端证书；使用代理时，也检查代理证书配置。"
    if any(isinstance(e, socket.gaierror) for e in chain):
        return "dns", "主机名解析失败", "检查目标主机名与 DNS；使用代理时，也检查代理端的域名解析。"
    if isinstance(error, adapters.httpx.ProxyError):
        return "proxy", "代理协商或认证失败", "检查协议、监听端口及用户名密码。"
    if isinstance(error, (adapters.httpx.TimeoutException, TimeoutError, socket.timeout)):
        return "timeout", "连接或响应超时", "检查网络路由、防火墙及渠道服务；使用代理时，也检查代理状态。"
    if any(isinstance(e, ConnectionRefusedError) for e in chain):
        return "refused", "连接被拒绝", "确认对应服务已启动、监听地址和端口正确。"
    return "connection", "网络连接未完成", "检查目标地址、端口、路由及防火墙；使用代理时，也核对代理配置。"


class Network:
    def __init__(self, hub):
        self.hub, self.store = hub, hub.store

    def fail(self, text, status=400):
        from .core import HubError
        raise HubError(text, status)

    @staticmethod
    def setting(conn, key, default=""):
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def put_setting(conn, key, value):
        conn.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def profile(self, conn, proxy_id):
        row = conn.execute("SELECT * FROM network_proxies WHERE id=?", (proxy_id,)).fetchone()
        if not row:
            self.fail("代理配置不存在；请重新选择连接方式", 409)
        return dict(row)

    def validate_route(self, conn, route, kind=None):
        if route.get("network_mode") == "profile":
            self.profile(conn, route.get("proxy_id", ""))
        if kind in {"email", "apprise"} and route.get("network_mode") not in {"system", "default"}:
            self.fail("邮箱与扩展渠道通过系统网络 / TUN 连接，暂不支持应用内指定代理")

    def prepare_target(self, conn, kind, config):
        config = dict(config)
        self.validate_route(conn, spec(config), kind)
        if kind == "qq" and config.get("app_id"):
            row = conn.execute("SELECT config FROM qq_network_routes WHERE app_id=?", (config["app_id"],)).fetchone()
            if row:
                # An existing bot always owns one explicit route. Change it in Network.
                config = {k: v for k, v in config.items() if k not in ROUTE_FIELDS} | self.store.decrypt(row[0])
            else:
                conn.execute("INSERT INTO qq_network_routes VALUES (?,?)", (config["app_id"], self.store.encrypt(spec(config))))
        return config

    def route(self, conn, target):
        route = spec(target["config"])
        if target["kind"] in {"email", "apprise"}:
            return {"network_mode": "system"}
        if target["kind"] == "qq":
            row = conn.execute("SELECT config FROM qq_network_routes WHERE app_id=?", (target["config"]["app_id"],)).fetchone()
            if row:
                route = self.store.decrypt(row[0])
        return route

    def resolve(self, conn, route):
        mode = route.get("network_mode", "default")
        proxy_id = route.get("proxy_id", "") if mode == "profile" else self.setting(conn, "default_proxy_id") if mode == "default" else ""
        if proxy_id:
            row = self.profile(conn, proxy_id)
            return self.store.decrypt(row["config"])["url"], row["name"], proxy_id
        if mode == "custom":
            if not route.get("proxy"):
                self.fail("指定的代理地址缺失；请重新配置连接方式", 409)
            return route["proxy"], "单独代理（旧配置）", ""
        if mode == "default":
            old = self.setting(conn, "default_proxy")
            if old:
                return self.store.decrypt(old), "默认代理（旧配置）", ""
        return "", "直接连接", ""

    def effective_config(self, target):
        with self.store.connect() as conn:
            route = self.route(conn, target)
            url, _, _ = self.resolve(conn, route)
        config = {k: v for k, v in target["config"].items() if k not in ROUTE_FIELDS}
        return config | route | ({"proxy": url} if url else {})

    def targets(self, conn):
        return [dict(r) | {"config": self.store.decrypt(r["config"])} for r in conn.execute("SELECT * FROM targets WHERE deleted IS NULL ORDER BY created,id")]

    def public_route(self, conn, target, targets):
        route = self.route(conn, target)
        url, label, proxy_id = self.resolve(conn, route)
        group = [t for t in targets if t["kind"] == "qq" and t["config"].get("app_id") == target["config"].get("app_id")] if target["kind"] == "qq" else [target]
        signature = hashlib.sha256(json.dumps([route, url], sort_keys=True).encode()).hexdigest()
        return {"mode": route["network_mode"], "proxy_id": proxy_id, "label": "系统网络" if target["kind"] in {"email", "apprise"} else label,
                "signature": signature, "shared_ids": sorted(t["id"] for t in group),
                "shared_names": [t["name"] for t in group], "system_only": target["kind"] in {"email", "apprise"}}

    def usage(self, conn, proxy_id, targets):
        uses = [t for t in targets if self.public_route(conn, t, targets)["proxy_id"] == proxy_id]
        default = self.setting(conn, "default_proxy_id") == proxy_id
        return {"target_ids": sorted(t["id"] for t in uses), "target_names": [t["name"] for t in uses], "is_default": default}

    def state(self):
        with self.store.connect() as conn:
            targets = self.targets(conn)
            proxies = []
            for row in conn.execute("SELECT * FROM network_proxies ORDER BY created,id"):
                p = parts(self.store.decrypt(row["config"])["url"])
                proxies.append({"id": row["id"], "name": row["name"], "revision": row["revision"]} | p | self.usage(conn, row["id"], targets))
            return {"proxies": proxies, "default_proxy_id": self.setting(conn, "default_proxy_id"),
                    "routes": {t["id"]: self.public_route(conn, t, targets) for t in targets},
                    "diagnostics": [json.loads(r[0]) for r in conn.execute("SELECT result FROM network_diagnostics ORDER BY tested DESC LIMIT 50")]}

    def build_url(self, data, old_url=""):
        if data.url:
            return validate_proxy(data.url)
        if not data.host or data.port is None:
            self.fail("请填写代理主机和端口，或粘贴完整代理地址")
        password = data.password
        if password is None:
            old = urlsplit(old_url)
            if data.username and data.username != unquote(old.username or "") and old.password:
                self.fail("代理用户名已变化，请填写对应密码")
            password = unquote(old.password or "") if data.username else ""
        if password and not data.username:
            self.fail("填写代理密码时需要同时填写用户名")
        if any(c in data.host for c in "/@?#") or any(c.isspace() for c in data.host):
            self.fail("代理主机只填写 IP 或域名")
        host = data.host.strip("[]")
        host = f"[{host}]" if ":" in host else host
        auth = (quote(data.username, safe="") + ":" + quote(password, safe="") + "@") if data.username else ""
        return validate_proxy(f"{data.scheme}://{auth}{host}:{data.port}")

    def save_proxy(self, data, proxy_id=None):
        with self.store.connect(True) as conn:
            old = self.profile(conn, proxy_id) if proxy_id else None
            if old:
                uses = self.usage(conn, proxy_id, self.targets(conn))
                if data.revision != old["revision"] or sorted(data.expected_targets or []) != uses["target_ids"] or data.expected_default != uses["is_default"]:
                    self.fail("代理或引用渠道已变化，请刷新后核对影响范围再保存", 409)
            url = self.build_url(data, self.store.decrypt(old["config"])["url"] if old else "")
            if conn.execute("SELECT 1 FROM network_proxies WHERE name=? AND id!=?", (data.name, proxy_id or "")).fetchone():
                self.fail("已有同名代理，请使用不同名称")
            proxy_id = proxy_id or uuid4().hex
            conn.execute("INSERT INTO network_proxies VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,config=excluded.config,revision=network_proxies.revision+1", (proxy_id, data.name, self.store.encrypt({"url": url}), 1, self.hub.clock()))
        self.store.audit("network.proxy.save", proxy_id, self.hub.clock())
        return {"id": proxy_id}

    def delete_proxy(self, proxy_id):
        with self.store.connect(True) as conn:
            self.profile(conn, proxy_id)
            uses = self.usage(conn, proxy_id, self.targets(conn))
            if uses["is_default"] or uses["target_ids"]:
                self.fail("此代理仍被默认连接方式或渠道引用，请先调整连接方式再删除", 409)
            conn.execute("DELETE FROM network_proxies WHERE id=?", (proxy_id,))
            # Remove orphan bot routes to avoid reviving a deleted reference later.
            for row in conn.execute("SELECT app_id,config FROM qq_network_routes").fetchall():
                if self.store.decrypt(row["config"]).get("proxy_id") == proxy_id:
                    conn.execute("DELETE FROM qq_network_routes WHERE app_id=?", (row["app_id"],))
        self.store.audit("network.proxy.delete", proxy_id, self.hub.clock())
        return {"ok": True}

    def legacy_default(self, url):
        with self.store.connect(True) as conn:
            if url:
                validate_proxy(url)
                proxy_id = uuid4().hex
                conn.execute("INSERT INTO network_proxies VALUES (?,?,?,?,?)", (proxy_id, "兼容接口代理 · " + proxy_id[:8], self.store.encrypt({"url": url}), 1, self.hub.clock()))
            else:
                proxy_id = ""
            self.put_setting(conn, "default_proxy_id", proxy_id)
            self.put_setting(conn, "default_proxy", "")
        self.store.audit("network.default.legacy", proxy_id or "direct", self.hub.clock())

    def set_route(self, data, target_id=None):
        route = {"network_mode": data.mode} | ({"proxy_id": data.proxy_id} if data.mode == "profile" else {})
        with self.store.connect(True) as conn:
            targets = self.targets(conn)
            self.validate_route(conn, route)
            if target_id is None:
                if data.mode not in {"profile", "system"}:
                    self.fail("默认连接方式请选择直接连接或指定代理")
                affected = sorted(t["id"] for t in targets if self.route(conn, t)["network_mode"] == "default")
                if data.previous_proxy_id != self.setting(conn, "default_proxy_id") or sorted(data.expected_targets or []) != affected:
                    self.fail("默认连接方式或关联渠道已变化，请刷新后重新确认", 409)
                self.put_setting(conn, "default_proxy_id", data.proxy_id if data.mode == "profile" else "")
                self.put_setting(conn, "default_proxy", "")
            else:
                target = next((t for t in targets if t["id"] == target_id), None)
                if not target:
                    self.fail("渠道不存在", 404)
                self.validate_route(conn, route, target["kind"])
                current = self.public_route(conn, target, targets)
                if data.signature != current["signature"] or sorted(data.expected_targets or []) != current["shared_ids"]:
                    self.fail("连接方式或关联渠道已变化，请刷新后重新确认", 409)
                if target["kind"] == "qq":
                    conn.execute("INSERT INTO qq_network_routes VALUES (?,?) ON CONFLICT(app_id) DO UPDATE SET config=excluded.config", (target["config"]["app_id"], self.store.encrypt(route)))
                for t in targets:
                    if t["id"] in current["shared_ids"]:
                        config = {k: v for k, v in t["config"].items() if k not in ROUTE_FIELDS} | route
                        conn.execute("UPDATE targets SET config=? WHERE id=?", (self.store.encrypt(config), t["id"]))
        self.store.audit("network.route.save", target_id or "default", self.hub.clock())
        return {"ok": True}

    def diagnostic(self, data):
        started = time.monotonic()
        target = None
        with self.store.connect() as conn:
            if data.scope == "target":
                target = self.hub.target(data.id, conn)
                route = self.route(conn, target)
                url, label, _ = self.resolve(conn, route)
            elif data.scope == "proxy":
                row = self.profile(conn, data.id)
                url, label = self.store.decrypt(row["config"])["url"], row["name"]
            elif data.scope == "preview":
                if data.proxy is None:
                    self.fail("请填写待测试的代理配置")
                old_url = self.store.decrypt(self.profile(conn, data.id)["config"])["url"] if data.id else ""
                url, label = self.build_url(data.proxy, old_url), "未保存的代理"
            else:
                url, label, _ = self.resolve(conn, {"network_mode": "default"})
        kind = target["kind"] if target else data.endpoint
        if kind in {"email", "apprise"}:
            label = "系统网络"
        result = {"scope": data.scope, "id": data.id, "route": label, "tested_at": self.hub.clock(),
                  "endpoint": kind, "status": "reachable", "detail": "网络请求已完成", "hint": "网络可达不代表授权通过或消息已收到。", "notification_sent": False}
        try:
            if kind == "apprise":
                result.update(status="unsupported", detail="扩展渠道由 Apprise 管理", hint="请检查系统 / TUN 路由，并在渠道绑定中手动发送测试消息。")
            elif kind == "email" and target['config'].get('provider') == 'outlook':
                with adapters.client({}, timeout=8) as http:
                    response = http.head('https://graph.microsoft.com/')
                result.update(http_status=response.status_code, detail=f"微软邮件接口返回 HTTP {response.status_code}", hint="仅检测系统网络可达性，未登录、未发送邮件。授权请到邮箱渠道配置中完成。")
                if response.status_code >= 500 or response.status_code == 429:
                    result['status'] = 'warning'
            elif kind == "email":
                c = target["config"]
                cls = smtplib.SMTP_SSL if c.get("security", "starttls") == "ssl" else smtplib.SMTP
                kwargs = {"context": ssl.create_default_context()} if cls == smtplib.SMTP_SSL else {}
                with cls(c["host"], int(c.get("port", 587)), timeout=8, **kwargs) as smtp:
                    code, _ = smtp.ehlo()
                    if code != 250:
                        raise smtplib.SMTPException()
                    if c.get("security", "starttls") == "starttls":
                        smtp.starttls(context=ssl.create_default_context())
                result.update(detail="SMTP 连接与所选加密协商完成", hint="未登录邮箱，未发送邮件；授权码和实际收件请通过渠道测试验证。")
            else:
                endpoint = PROBES.get(kind)
                if kind == "webhook":
                    p = urlsplit(target["config"]["url"])
                    endpoint = f"{p.scheme}://{p.netloc}/"
                with adapters.client({"proxy": url}, timeout=8) as http:
                    # HEAD on the origin only: no webhook path, tokens, bodies or redirects.
                    with http.stream("HEAD", endpoint) as response:
                        code = response.status_code
                result["http_status"] = code
                if code == 407:
                    result.update(status="failed", detail="代理认证失败（HTTP 407）", hint="核对代理用户名、密码与认证方式。")
                elif code >= 500 or code == 429:
                    result.update(status="warning", detail=f"收到 HTTP {code} 响应，服务异常或限流", hint="网络路径已有响应，请稍后重试并检查渠道状态。")
                else:
                    result["detail"] = f"收到 HTTP {code} 响应，网络路径可达"
                    result["hint"] = "401 / 403 / 404 / 405 也仅说明收到了 HTTP 响应；本检测不验证账号或投递。"
        except (adapters.httpx.HTTPError, OSError, smtplib.SMTPException) as error:
            code, detail, hint = error_info(error)
            result.update(status="failed", error_code=code, detail=detail, hint=hint)
        result["latency_ms"] = round((time.monotonic() - started) * 1000)
        if data.scope != "preview":
            with self.store.connect(True) as conn:
                key = f"{data.scope}:{data.id}:{data.endpoint}"
                conn.execute("INSERT INTO network_diagnostics VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET result=excluded.result,tested=excluded.tested", (key, json.dumps(result), self.hub.clock()))
                conn.execute("DELETE FROM network_diagnostics WHERE id NOT IN (SELECT id FROM network_diagnostics ORDER BY tested DESC LIMIT 50)")
        return result
