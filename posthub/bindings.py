"""Official QQ webhook binding and self-owned Weixin iLink QR authorization."""
import base64
import hmac
import io
import json
import re
import secrets
import threading
from collections import defaultdict

import httpx
import qrcode
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .adapters import ILINK, DeliveryError, check_code, client, ilink_base, post_json, wx_body, wx_headers
from .core import HubError
from .storage import digest


def qq_key(secret):
    if not secret:
        raise HubError("QQ 凭据未配置")
    seed = secret.encode()
    return Ed25519PrivateKey.from_private_bytes((seed * (32 // len(seed) + 1))[:32])


class Bindings:
    def __init__(self, hub):
        self.hub, self.store = hub, hub.store
        self._sync_locks = defaultdict(threading.Lock)

    def qq_code(self, target_id):
        raise HubError("QQ 已改为选择最近会话并测试发送，请刷新页面；不再使用绑定码", 410)

    def qq_event(self, app_id, payload, raw, signature, timestamp):
        with self.store.connect() as conn:
            targets = [self.hub.target(r[0], conn) for r in conn.execute("SELECT id FROM targets WHERE kind='qq' AND enabled=1 AND deleted IS NULL")]
        targets = [t for t in targets if t["config"].get("app_id") == app_id]
        if not targets:
            raise HubError("机器人不存在", 404)
        key = qq_key(targets[0]["config"]["app_secret"])
        data = payload.get("d", {})
        if payload.get("op") == 13:
            plain, stamp = str(data.get("plain_token", "")), str(data.get("event_ts", ""))
            if not plain or len(plain) > 512 or not stamp.isdigit():
                raise HubError("回调验证格式错误")
            return {"plain_token": plain, "signature": key.sign((stamp + plain).encode()).hex()}
        try:
            if abs(self.hub.clock() - int(timestamp)) > 300:
                raise ValueError()
            key.public_key().verify(bytes.fromhex(signature), timestamp.encode() + raw)
        except Exception:
            raise HubError("回调签名无效或已过期", 401) from None
        return self.qq_dispatch(app_id, payload, targets[0]["config"]["app_secret"])

    def qq_dispatch(self, app_id, payload, authenticated_secret):
        """Observe authenticated conversations. Incoming messages never change recipients."""
        data = payload.get("d", {})
        if not isinstance(data, dict):
            return {"ok": True}
        event = payload.get("t")
        if event not in {"GROUP_AT_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"}:
            return {"ok": True}
        content = data.get("content", "")
        if not isinstance(content, str):
            return {"ok": True}
        group = event == "GROUP_AT_MESSAGE_CREATE"
        author = data.get("author") or {}
        address = data.get("group_openid") if group else author.get("user_openid") if isinstance(author, dict) else None
        if not isinstance(address, str) or not re.fullmatch(r"[a-zA-Z0-9_+=.-]{1,256}", address):
            raise HubError("平台目标标识无效")
        with self.store.connect(True) as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM targets WHERE kind='qq' AND enabled=1 AND deleted IS NULL")]
            authorized = False
            for target_id in ids:
                fresh = self.hub.target(target_id, conn)
                if fresh["config"].get("app_id") == app_id and hmac.compare_digest(fresh["config"].get("app_secret", ""), authenticated_secret):
                    authorized = True
                    break
            if not authorized:
                return {"ok": True}
            credential_hash = digest(authenticated_secret)
            target_type = "group" if group else "user"
            conversation_id = digest(json.dumps([app_id, credential_hash, target_type, address]))
            # No full chat history or tokens are retained. Short excerpts help identify the intended conversation.
            details = {"address": address, "preview": re.sub(r"<@!?[^>]+>", "", content).strip()[:80]}
            now = self.hub.clock()
            conn.execute("DELETE FROM qq_conversations WHERE last_seen<?", (now - 600,))
            conn.execute("INSERT INTO qq_conversations VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET details=excluded.details,last_seen=excluded.last_seen", (conversation_id, app_id, credential_hash, target_type, self.store.encrypt(details), now))
            conn.execute("DELETE FROM qq_conversations WHERE app_id=? AND id NOT IN (SELECT id FROM qq_conversations WHERE app_id=? ORDER BY last_seen DESC LIMIT 50)", (app_id, app_id))
        return {"ok": True}

    def qq_conversations(self, target_id):
        with self.store.connect(True) as conn:
            target = self.hub.target(target_id, conn)
            if target["kind"] != "qq":
                raise HubError("该目标不是 QQ 渠道")
            config = target["config"]
            conn.execute("DELETE FROM qq_conversations WHERE last_seen<?", (self.hub.clock() - 600,))
            rows = conn.execute("SELECT * FROM qq_conversations WHERE app_id=? AND credential_hash=? AND target_type=? ORDER BY last_seen DESC LIMIT 20", (config["app_id"], digest(config["app_secret"]), config.get("target_type", "user"))).fetchall()
            result = []
            for row in rows:
                details = self.store.decrypt(row["details"])
                result.append({"id": row["id"], "label": ("群聊" if row["target_type"] == "group" else "个人会话") + " · " + details["address"][-8:], "preview": details["preview"], "last_seen": row["last_seen"], "current": details["address"] == config.get("address")})
            return result

    def qq_select(self, target_id, conversation_id):
        """Only an explicit admin selection may change the destination."""
        with self.store.connect(True) as conn:
            target = self.hub.target(target_id, conn)
            if target["kind"] != "qq" or not target["enabled"]:
                raise HubError("请先启用此 QQ 目标")
            row = conn.execute("SELECT * FROM qq_conversations WHERE id=? AND last_seen>?", (conversation_id, self.hub.clock() - 600)).fetchone()
            config = target["config"]
            if not row or row["app_id"] != config["app_id"] or row["credential_hash"] != digest(config["app_secret"]) or row["target_type"] != config.get("target_type", "user"):
                raise HubError("此会话不可用或已过期，请在目标 QQ 会话中重新发送一条消息", 409)
            address = self.store.decrypt(row["details"])["address"]
            changed = address != config.get("address")
            config = config | {"address": address}
            conn.execute("UPDATE targets SET config=?,revision=revision+?,verified=CASE WHEN ? THEN 0 ELSE verified END,binding_hash=NULL,verify_hash=CASE WHEN ? THEN NULL ELSE verify_hash END WHERE id=?", (self.store.encrypt(config), int(changed), changed, changed, target_id))
            conn.execute("INSERT INTO audit(at,action,subject) VALUES (?,'qq.recipient_selected',?)", (self.hub.clock(), target_id))
        return {"id": target_id, "bound": True}

    def _wx(self, target_id):
        target = self.hub.target(target_id)
        if target["kind"] != "weixin" or not target["enabled"]:
            raise HubError("请先启用个人微信目标")
        return target

    def _save(self, target, changes, *, reauthorize=False, expected_qr=None):
        with self.store.connect(True) as conn:
            fresh = self.hub.target(target["id"], conn)
            if not fresh["enabled"] or fresh["revision"] != target["revision"] or (expected_qr is not None and fresh["config"].get("qr_code") != expected_qr):
                raise HubError("目标配置已变化，请刷新重试", 409)
            config = fresh["config"] | changes
            conn.execute("UPDATE targets SET config=?,revision=revision+?,verified=CASE WHEN ? THEN 0 ELSE verified END,verify_hash=CASE WHEN ? THEN NULL ELSE verify_hash END WHERE id=?", (self.store.encrypt(config), int(reauthorize), reauthorize, reauthorize, target["id"]))

    def qr_start(self, target_id):
        target = self._wx(target_id)
        with client(self.hub.effective_config(target)) as http:
            result = post_json(http, ILINK + "/ilink/bot/get_bot_qrcode?bot_type=3", {"local_token_list": []}, wx_headers(), pre_send=True)
        if not result.get("qrcode") or not result.get("qrcode_img_content"):
            raise HubError("微信未返回二维码，请稍后重试")
        self._save(target, {"qr_code": result["qrcode"], "qr_base": ILINK, "qr_expires": self.hub.clock() + 480})
        image = qrcode.make(result["qrcode_img_content"])
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return {"image": "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode(), "status": "wait"}

    def qr_poll(self, target_id, pin=""):
        target = self._wx(target_id)
        config = target["config"]
        if not config.get("qr_code") or config.get("qr_expires", 0) < self.hub.clock():
            raise HubError("二维码会话已过期，请刷新")
        params = {"qrcode": config["qr_code"]}
        if pin:
            if not re.fullmatch(r"\d{4,12}", pin):
                raise HubError("请填写手机上显示的数字验证码")
            params["verify_code"] = pin
        try:
            with client(self.hub.effective_config(target), timeout=36) as http:
                response = http.get(ilink_base(config.get("qr_base", ILINK)) + "/ilink/bot/get_qrcode_status", params=params, headers=wx_headers())
                response.raise_for_status()
                result = response.json()
        except httpx.TimeoutException:
            return {"status": "wait"}
        except (httpx.HTTPError, ValueError):
            raise HubError("二维码状态查询失败，请检查代理后重试", 502) from None
        status = result.get("status", "wait")
        if status == "scaned_but_redirect" and result.get("redirect_host"):
            self._save(target, {"qr_base": ilink_base("https://" + result["redirect_host"])}, expected_qr=config["qr_code"])
        if status == "confirmed":
            if not result.get("bot_token") or not result.get("ilink_user_id"):
                raise HubError("微信授权响应缺少凭据")
            self._save(target, {"bot_token": result["bot_token"], "bot_id": result.get("ilink_bot_id", ""), "base_url": ilink_base(result.get("baseurl", ILINK)), "address": result["ilink_user_id"], "context_token": "", "cursor": "", "qr_code": ""}, reauthorize=True, expected_qr=config["qr_code"])
        return {"status": status}

    def wx_sync(self, target_id, timeout=36):
        lock = self._sync_locks[target_id]
        if not lock.acquire(blocking=False):
            return {"has_session": bool(self._wx(target_id)["config"].get("context_token")), "syncing": True}
        try:
            return self._wx_sync(target_id, timeout)
        finally:
            lock.release()

    def _wx_sync(self, target_id, timeout=36):
        target = self._wx(target_id)
        config = target["config"]
        if not config.get("bot_token"):
            raise HubError("请先扫码授权")
        try:
            with client(self.hub.effective_config(target), timeout) as http:
                result = post_json(http, ilink_base(config.get("base_url", ILINK)) + "/ilink/bot/getupdates", wx_body({"get_updates_buf": config.get("cursor", "")}), wx_headers(config["bot_token"]), pre_send=True)
            check_code(result)
        except DeliveryError as error:
            if error.kind == "blocked":
                with self.store.connect(True) as conn:
                    conn.execute("UPDATE targets SET verified=0 WHERE id=?", (target_id,))
            raise
        changes = {}
        if result.get("get_updates_buf"):
            changes["cursor"] = result["get_updates_buf"]
        for msg in result.get("msgs", []):
            if msg.get("from_user_id") == config.get("address") and msg.get("message_type") == 1 and msg.get("context_token"):
                changes["context_token"] = msg["context_token"]
        if changes:
            self._save(target, changes)
        return {"has_session": bool(changes.get("context_token") or config.get("context_token"))}
