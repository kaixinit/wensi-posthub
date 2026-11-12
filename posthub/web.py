from __future__ import annotations

from contextlib import asynccontextmanager
import html
import json
import logging
from pathlib import Path
import secrets
import threading
import time

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from filelock import FileLock

from . import adapters, __version__, PRODUCT_NAME
from .bindings import Bindings
from .qq_gateway import QQGateway, credentials as qq_credentials
from .core import Hub, HubError
from .models import AppInput, CodeInput, EnabledInput, HookMessageInput, LoginInput, MessageInput, ProfileInput, QQConversationInput, RetryInput, SettingsInput, TargetInput
from .storage import Store, digest, password_matches
from .models import MicrosoftEmailInput, EmailFlowInput, SetupTestInput
from .models import ReceivedInput, ProxyInput, NetworkRouteInput, NetworkTestInput, PasswordChangeInput

log = logging.getLogger("posthub")


def create_app(data_dir="data", *, run_worker=True, sender=None, clock=time.time):
    store = Store(data_dir)
    hub = Hub(store, sender=sender, clock=clock)
    binding = Bindings(hub)
    qq = QQGateway(hub, binding)
    stop = threading.Event()

    def worker():
        while not stop.is_set():
            try:
                store.set_setting("heartbeat", clock())
                more = hub.tick()
                hub.notify_alerts()
            except Exception:
                # Do not serialize requests/configurations to logs.
                log.error("任务处理异常；请检查数据库可写性和磁盘空间")
                more = False
            stop.wait(0.1 if more else 1)

    def poll_weixin():
        while not stop.is_set():
            try:
                with store.connect() as conn:
                    ids = [r[0] for r in conn.execute("SELECT id FROM targets WHERE kind='weixin' AND enabled=1")]
                for target_id in ids:
                    if stop.is_set():
                        break
                    try:
                        binding.wx_sync(target_id)
                    except (ValueError, adapters.DeliveryError):
                        pass
            except Exception:
                log.error("微信会话更新异常；将稍后重试")
            stop.wait(5)

    @asynccontextmanager
    async def lifespan(app):
        lock = FileLock(str(store.directory / "server.lock"), timeout=0)
        threads = []
        if run_worker:
            lock.acquire()
            hub.recover()
            for fn in (worker, poll_weixin, lambda: qq.run(stop)):
                thread = threading.Thread(target=fn, daemon=True)
                thread.start()
                threads.append(thread)
        try:
            yield
        finally:
            stop.set()
            qq.wake.set()
            for thread in threads:
                thread.join(timeout=100)
            if run_worker:
                lock.release()

    app = FastAPI(title=PRODUCT_NAME, version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.hub, app.state.bindings = hub, binding
    app.state.qq = qq

    @app.middleware("http")
    async def guard(request, call_next):
        if request.method in {"POST", "PUT", "PATCH"}:
            size, chunks = 0, []
            async for chunk in request.stream():
                size += len(chunk)
                if size > 65536:
                    return JSONResponse({"detail": "请求体超过 64 KiB"}, status_code=413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(HubError)
    async def hub_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(adapters.DeliveryError)
    async def transport_error(request, exc):
        return JSONResponse({"detail": str(exc), "state": exc.kind}, status_code=502)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic's default response includes the submitted secret-bearing body.
        issues = [{"field": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in exc.errors()]
        return JSONResponse({"detail": "输入格式不正确", "issues": issues}, status_code=422)

    def browser_write(request):
        if request.headers.get("X-PostHub-Request") != "1":
            raise HubError("请求来源验证失败", 403)

    def admin(request: Request):
        token = request.cookies.get("posthub_session", "")
        with store.connect() as conn:
            session = conn.execute("SELECT * FROM sessions WHERE token_hash=? AND expires>?", (digest(token), clock())).fetchone()
        config = json.loads(store.config_path.read_text(encoding='utf-8'))
        if not session or not secrets.compare_digest(session['auth_version'], digest(config['password_hash'])):
            raise HubError("请登录管理员账号", 401)
        if request.method not in {"GET", "HEAD"}:
            browser_write(request)
            if not secrets.compare_digest(request.headers.get("X-CSRF-Token", ""), session["csrf"]):
                raise HubError("会话验证失败，请刷新页面", 403)
        if config.get('must_change_password') and request.url.path not in {'/api/session','/api/logout','/api/admin/state','/api/admin/account','/api/admin/account/password'}:
            raise HubError('请先在账号设置中修改初始密码', 403)
        return dict(session)

    def authenticate_token(token):
        found = hub.authenticate_app(token) if token and len(token) <= 128 else None
        if not found:
            raise HubError("应用密钥无效或已停用", 401)
        if store.limited("app:" + found["id"], 120, 60, clock()):
            raise HubError("应用每分钟最多请求 120 次", 429)
        return found

    def application(request: Request):
        header = request.headers.get("Authorization", "")
        return authenticate_token(header[7:] if header.startswith("Bearer ") else "")

    def hook_application(api_key: str):
        return authenticate_token(api_key)

    @app.get("/healthz")
    def health():
        heartbeat = store.setting("heartbeat", 0)
        healthy = bool(heartbeat and clock() - heartbeat < 180) if run_worker else True
        return JSONResponse({"status": "ok" if healthy else "worker_stalled", "version": __version__}, status_code=200 if healthy else 503)

    @app.post("/api/login")
    def login(data: LoginInput, request: Request):
        browser_write(request)
        if store.limited("login:" + request.client.host, 10, 300, clock()):
            raise HubError("登录尝试过于频繁，请 5 分钟后重试", 429)
        config = json.loads(store.config_path.read_text(encoding="utf-8"))
        valid_password = password_matches(data.password, config["password_hash"])
        if not valid_password or data.username != config.get('username', 'admin'):
            raise HubError("账号或密码不正确", 401)
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with store.connect(True) as conn:
            conn.execute("DELETE FROM sessions WHERE expires<?", (clock(),))
            conn.execute("INSERT INTO sessions(token_hash,csrf,expires,auth_version) VALUES (?,?,?,?)", (digest(token), csrf, clock() + 28800, digest(config['password_hash'])))
        response = JSONResponse({"csrf": csrf, **store.account()})
        response.set_cookie("posthub_session", token, httponly=True, samesite="strict", secure=store.setting("public_base_url").startswith("https://"), max_age=28800)
        return response

    @app.get("/api/session")
    def session(who=Depends(admin)):
        return {"csrf": who["csrf"], **store.account()}

    @app.get('/api/admin/account', dependencies=[Depends(admin)])
    def account():
        return store.account()

    @app.post('/api/admin/account/password', dependencies=[Depends(admin)])
    def change_password(data: PasswordChangeInput):
        if data.new_password != data.confirm_password:
            raise HubError('两次新密码不一致')
        if store.limited('account-password-change', 6, 300, clock()):
            raise HubError('密码修改尝试过于频繁，请 5 分钟后重试', 429)
        result = store.change_password(data.new_password, current_password=data.current_password)
        response = JSONResponse(result)
        response.delete_cookie('posthub_session')
        return response

    @app.post("/api/logout")
    def logout(request: Request, who=Depends(admin)):
        with store.connect(True) as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (digest(request.cookies.get("posthub_session", "")),))
        response = JSONResponse({"ok": True})
        response.delete_cookie("posthub_session")
        return response

    @app.get("/api/admin/state", dependencies=[Depends(admin)])
    def state(offset: int = 0):
        result = hub.state(max(0, offset))
        for target in result['targets']:
            if target['kind'] == 'qq':
                target['qq_connection'] = qq.status(target['config']['app_id']) if target['config'].get('event_mode','websocket') == 'websocket' else {'state':'webhook','detail':'使用公网 HTTPS 回调接收事件'}
                target['qq_test'] = hub.qq_test_status(target['id'])
            elif target['kind'] == 'weixin':
                target['wx_test'] = hub.qq_test_status(target['id'])
        return result

    @app.post('/api/admin/qq/connect', dependencies=[Depends(admin)])
    def connect_qq(data: TargetInput, target_id: str | None = None):
        if data.kind != 'qq':
            raise HubError('此接口仅用于 QQ 机器人')
        config = dict(data.config)
        if target_id:
            old = hub.target(target_id)
            if old['kind'] != 'qq':
                raise HubError('该目标不是 QQ 渠道')
            config = old['config'] | config
        adapters.validate_config('qq', config)
        effective = hub.effective_config({'kind':'qq','config':config})
        qq_credentials(effective, need_gateway=config.get('event_mode','websocket') == 'websocket')
        result = hub.save_target(TargetInput(name=data.name,kind='qq',config=config), target_id)
        qq.wake.set()
        return result | {'credentials_valid':True}

    @app.post('/api/admin/targets/{target_id}/qq-connect', dependencies=[Depends(admin)])
    def reconnect_qq(target_id: str):
        target = hub.target(target_id)
        if target['kind'] != 'qq' or not target['enabled']:
            raise HubError('请先启用此 QQ 目标')
        config = hub.effective_config(target)
        qq_credentials(config, need_gateway=config.get('event_mode','websocket') == 'websocket')
        qq.reconnect(config['app_id'])
        return {'credentials_valid':True}

    @app.put('/api/admin/email/microsoft', dependencies=[Depends(admin)])
    def email_microsoft(data: MicrosoftEmailInput):
        return hub.email.configure_microsoft(data.client_id)

    @app.post('/api/admin/targets/{target_id}/email/connect', dependencies=[Depends(admin)])
    def email_connect(target_id: str):
        return hub.email.start(target_id)

    @app.post('/api/admin/targets/{target_id}/email/poll', dependencies=[Depends(admin)])
    def email_poll(target_id: str, data: EmailFlowInput):
        return hub.email.poll(target_id, data.flow_id)

    @app.post('/api/admin/targets/{target_id}/email/cancel', dependencies=[Depends(admin)])
    def email_cancel(target_id: str, data: EmailFlowInput):
        return hub.email.cancel(target_id, data.flow_id)

    @app.post('/api/admin/targets/{target_id}/email/disconnect', dependencies=[Depends(admin)])
    def email_disconnect(target_id: str):
        return hub.email.disconnect(target_id)

    @app.post("/api/admin/targets", dependencies=[Depends(admin)], status_code=201)
    def create_target(data: TargetInput):
        return hub.save_target(data)

    @app.put("/api/admin/targets/{target_id}", dependencies=[Depends(admin)])
    def edit_target(target_id: str, data: TargetInput):
        return hub.save_target(data, target_id)

    @app.post("/api/admin/targets/{target_id}/enabled", dependencies=[Depends(admin)])
    def target_enabled(target_id: str, data: EnabledInput):
        hub.set_enabled("targets", target_id, data.enabled)
        return {"ok": True}

    @app.get("/api/admin/targets/{target_id}/deletion", dependencies=[Depends(admin)])
    def target_deletion(target_id: str):
        return hub.deletion_info("targets", target_id)

    @app.delete("/api/admin/targets/{target_id}", dependencies=[Depends(admin)])
    def target_delete(target_id: str):
        return hub.delete_item("targets", target_id)

    @app.post("/api/admin/targets/{target_id}/verify", dependencies=[Depends(admin)], status_code=202)
    def verify_start(target_id: str):
        return hub.verify_start(target_id)

    @app.get('/api/admin/targets/{target_id}/verification-status', dependencies=[Depends(admin)])
    def verification_status(target_id: str):
        hub.target(target_id)
        return hub.qq_test_status(target_id)

    @app.post("/api/admin/targets/{target_id}/confirm", dependencies=[Depends(admin)])
    def verify_finish(target_id: str, data: CodeInput):
        return hub.verify_finish(target_id, data.code)

    @app.post("/api/admin/targets/{target_id}/qq-code", dependencies=[Depends(admin)])
    def qq_code(target_id: str):
        return binding.qq_code(target_id)

    @app.get('/api/admin/targets/{target_id}/qq-conversations', dependencies=[Depends(admin)])
    def qq_conversations(target_id: str):
        return {'conversations': binding.qq_conversations(target_id)}

    @app.post('/api/admin/targets/{target_id}/qq-recipient', dependencies=[Depends(admin)])
    def qq_recipient(target_id: str, data: QQConversationInput):
        return binding.qq_select(target_id, data.conversation_id)

    @app.post("/api/qq/{app_id}")
    async def qq_event(app_id: str, request: Request):
        if store.limited("qq:" + request.client.host, 600, 60, clock()):
            raise HubError("回调请求过于频繁", 429)
        raw = await request.body()
        payload = json.loads(raw)
        if not isinstance(payload, dict) or not isinstance(payload.get("d", {}), dict):
            raise HubError("回调格式错误")
        return binding.qq_event(app_id, payload, raw, request.headers.get("X-Signature-Ed25519", ""), request.headers.get("X-Signature-Timestamp", ""))

    @app.post("/api/admin/targets/{target_id}/weixin/qr", dependencies=[Depends(admin)])
    def qr_start(target_id: str):
        return binding.qr_start(target_id)

    @app.post("/api/admin/targets/{target_id}/weixin/poll", dependencies=[Depends(admin)])
    def qr_poll(target_id: str, data: dict):
        return binding.qr_poll(target_id, str(data.get("pin", "")))

    @app.post("/api/admin/targets/{target_id}/weixin/sync", dependencies=[Depends(admin)])
    def wx_sync(target_id: str):
        return binding.wx_sync(target_id)

    @app.post("/api/admin/profiles", dependencies=[Depends(admin)], status_code=201)
    def profile_create(data: ProfileInput):
        return hub.save_profile(data)

    @app.put("/api/admin/profiles/{profile_id}", dependencies=[Depends(admin)])
    def profile_edit(profile_id: str, data: ProfileInput):
        return hub.save_profile(data, profile_id)

    @app.post("/api/admin/apps", dependencies=[Depends(admin)], status_code=201)
    def app_create(data: AppInput):
        return hub.create_app(data)

    @app.put("/api/admin/apps/{app_id}", dependencies=[Depends(admin)])
    def app_edit(app_id: str, data: AppInput):
        return hub.update_app(app_id, data)

    @app.post('/api/admin/apps/{app_id}/setup-test',dependencies=[Depends(admin)],status_code=202)
    def setup_test(app_id: str,data: SetupTestInput):
        from .onboarding import send_test
        return send_test(hub,app_id,data)

    @app.post("/api/admin/apps/{app_id}/rotate-key", dependencies=[Depends(admin)])
    def rotate_key(app_id: str):
        return hub.rotate_app_key(app_id)

    @app.get("/api/admin/apps/{app_id}/deletion", dependencies=[Depends(admin)])
    def app_deletion(app_id: str):
        return hub.deletion_info("apps", app_id)

    @app.delete("/api/admin/apps/{app_id}", dependencies=[Depends(admin)])
    def app_delete(app_id: str):
        return hub.delete_item("apps", app_id)

    @app.post("/api/admin/apps/{app_id}/enabled", dependencies=[Depends(admin)])
    def app_enabled(app_id: str, data: EnabledInput):
        hub.set_enabled("apps", app_id, data.enabled)
        return {"ok": True}

    @app.post("/api/admin/messages", dependencies=[Depends(admin)], status_code=202)
    def admin_send(data: MessageInput):
        return hub.submit(data)

    @app.post("/api/v1/messages", status_code=202)
    def submit(data: MessageInput, who=Depends(application)):
        return hub.submit(data, app=who)

    @app.post("/api/v1/hooks/{api_key}", status_code=202)
    def hook_submit(data: HookMessageInput, who=Depends(hook_application)):
        return hub.submit(data, app=who, use_app_targets=True)

    @app.get("/api/v1/messages/{message_id}")
    def app_message(message_id: str, who=Depends(application)):
        return hub.message(message_id, who["id"])

    @app.get("/api/admin/messages/{message_id}", dependencies=[Depends(admin)])
    def get_message(message_id: str):
        return hub.message(message_id)

    @app.post("/api/admin/messages/{message_id}/cancel", dependencies=[Depends(admin)])
    def cancel(message_id: str):
        return hub.cancel(message_id)

    @app.post("/api/admin/messages/{message_id}/route", dependencies=[Depends(admin)])
    def route_message(message_id: str):
        return hub.release_message(message_id)

    @app.post("/api/admin/deliveries/{delivery_id}/retry", dependencies=[Depends(admin)])
    def retry(delivery_id: str, data: RetryInput):
        hub.retry(delivery_id, data.accept_duplicate_risk)
        return {"ok": True}

    @app.post("/api/admin/deliveries/{delivery_id}/confirm-received", dependencies=[Depends(admin)])
    def confirm_received(delivery_id: str, data: ReceivedInput):
        return hub.confirm_received(delivery_id)

    @app.post("/api/admin/alerts/{alert_id}/resolve", dependencies=[Depends(admin)])
    def resolve(alert_id: str):
        with store.connect(True) as conn:
            conn.execute("UPDATE alerts SET resolved=1 WHERE id=?", (alert_id,))
        return {"ok": True}

    @app.put("/api/admin/settings", dependencies=[Depends(admin)])
    def settings(data: SettingsInput):
        if data.public_base_url:
            adapters.http_url(data.public_base_url)
            parsed = adapters.urlsplit(data.public_base_url)
            if parsed.path not in {"", "/"} or parsed.query:
                raise HubError("平台地址仅填写域名和端口，不包含路径或参数")
        if data.alert_target_id:
            with store.connect() as conn:
                hub.check_targets(conn, [data.alert_target_id])
        if data.default_proxy is not None:
            adapters.validate_config("weixin", {"proxy": data.default_proxy})
            hub.network.legacy_default(data.default_proxy)
            qq.wake.set()
        store.set_setting("public_base_url", data.public_base_url.rstrip("/"))
        store.set_setting("alert_target_id", data.alert_target_id)
        return {"ok": True}

    @app.post("/api/admin/network/proxies", dependencies=[Depends(admin)])
    def create_proxy(data: ProxyInput):
        return hub.network.save_proxy(data)

    @app.put("/api/admin/network/proxies/{proxy_id}", dependencies=[Depends(admin)])
    def update_proxy(proxy_id: str, data: ProxyInput):
        result = hub.network.save_proxy(data, proxy_id)
        qq.wake.set()
        return result

    @app.delete("/api/admin/network/proxies/{proxy_id}", dependencies=[Depends(admin)])
    def delete_proxy(proxy_id: str):
        return hub.network.delete_proxy(proxy_id)

    @app.put("/api/admin/network/default", dependencies=[Depends(admin)])
    def default_route(data: NetworkRouteInput):
        result = hub.network.set_route(data)
        qq.wake.set()
        return result

    @app.put("/api/admin/network/targets/{target_id}", dependencies=[Depends(admin)])
    def target_route(target_id: str, data: NetworkRouteInput):
        result = hub.network.set_route(data, target_id)
        qq.wake.set()
        return result

    @app.post("/api/admin/network/test", dependencies=[Depends(admin)])
    def network_test(data: NetworkTestInput = NetworkTestInput()):
        # Admin-only, bounded, read-only network operations; never sends a message.
        if store.limited("network-diagnostics", 40, 60, clock()):
            raise HubError("检测过于频繁，请稍后重试", 429)
        return hub.network.diagnostic(data)

    @app.get("/api/admin/openapi", dependencies=[Depends(admin)])
    def api_reference():
        return app.openapi()

    def ack_page(token, confirmed=False):
        data = hub.acknowledge(token, mutate=confirmed)
        title = html.escape(data["title"])
        body = '<p class="success">已记录收件确认。</p>' if data["acknowledged"] else '<p>确认你已收到并查看这条通知后，点击下方按钮。</p><form method="post"><button type="submit">确认收到</button></form>'
        return HTMLResponse(f'<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{PRODUCT_NAME} · 收件确认</title><link rel="stylesheet" href="/style.css"><main class="ack-card"><h1>{title}</h1>{body}</main></html>')

    @app.get("/ack/{token}")
    def ack_get(token: str):
        return ack_page(token)

    @app.post("/ack/{token}")
    def ack_post(token: str):
        return ack_page(token, confirmed=True)

    app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="static")
    return app
