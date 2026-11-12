from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from uuid import uuid4

from . import adapters, PRODUCT_NAME
from .models import MessageInput
from .storage import Store, digest
from .network import Network, ROUTE_FIELDS
from .email_channel import EmailService, normalize as normalize_email, signature as email_signature


def uid():
    return uuid4().hex


class HubError(ValueError):
    def __init__(self, detail, status=400):
        super().__init__(detail)
        self.status = status


class Hub:
    def __init__(self, store: Store, sender=None, clock=time.time):
        self.store, self.clock = store, clock
        self.network = Network(self)
        self.email = EmailService(self)
        self.sender = sender or self._send

    def _send(self, kind, config, message, delivery_id):
        return self.email.send(config, message, delivery_id) if kind == "email" else adapters.send(kind, config, message, delivery_id)

    def target(self, target_id, conn=None):
        if conn is None:
            with self.store.connect() as db:
                return self.target(target_id, db)
        row = conn.execute("SELECT * FROM targets WHERE id=? AND deleted IS NULL", (target_id,)).fetchone()
        if not row:
            raise HubError("收件目标不存在", 404)
        result = dict(row)
        result["config"] = self.store.decrypt(result["config"])
        return result

    def public_target(self, row):
        config = self.store.decrypt(row["config"])
        return {k: row[k] for k in ("id", "name", "kind", "revision", "enabled", "verified", "created")} | {
            "config": {k: v for k, v in config.items() if k in adapters.PUBLIC_FIELDS},
            "configured_secrets": [k for k, v in config.items() if k not in adapters.PUBLIC_FIELDS and v and not k.startswith("qr")],
            "has_proxy": bool(config.get("proxy")),
            "email_authorized": bool(config.get("oauth_cache")) and not config.get("oauth_error", False),
            "bound": bool(config.get("address")), "has_session": bool(config.get("context_token")),
        }

    def save_target(self, data, target_id=None):
        with self.store.connect(True) as conn:
            if target_id:
                old = self.target(target_id, conn)
                if old["kind"] != data.kind:
                    raise HubError("已建目标不能更改渠道类型，请新建目标")
                config = self.network.prepare_target(conn, data.kind, normalize_email(data.config, old["config"]) if data.kind == "email" else {**old["config"], **data.config})
                external = {k: v for k, v in config.items() if k in adapters.FIELDS[data.kind] | ROUTE_FIELDS}
                adapters.validate_config(data.kind, external)
                if data.kind == "qq":
                    self._check_qq_credentials(conn, config, target_id)
                    if config.get('app_id') != old['config'].get('app_id') or config.get('target_type','user') != old['config'].get('target_type','user'):
                        config.pop('address',None)
                changed = {k:v for k,v in config.items() if k not in ROUTE_FIELDS} != {k:v for k,v in old["config"].items() if k not in ROUTE_FIELDS}
                if data.kind == "email":
                    changed = email_signature(config) != email_signature(old["config"])
                conn.execute("UPDATE targets SET name=?,config=?,revision=revision+?,verified=CASE WHEN ? THEN 0 ELSE verified END,verify_hash=CASE WHEN ? THEN NULL ELSE verify_hash END,binding_hash=CASE WHEN ? THEN NULL ELSE binding_hash END WHERE id=?", (data.name, self.store.encrypt(config), int(changed), changed, changed, changed, target_id))
            else:
                config = self.network.prepare_target(conn, data.kind, normalize_email(data.config) if data.kind == "email" else data.config)
                adapters.validate_config(data.kind, config)
                if data.kind == "qq":
                    self._check_qq_credentials(conn, config)
                target_id = uid()
                conn.execute("INSERT INTO targets(id,name,kind,config,created) VALUES (?,?,?,?,?)", (target_id, data.name, data.kind, self.store.encrypt(config), self.clock()))
        self.store.audit("target.save", target_id, self.clock())
        return {"id": target_id}

    def _check_qq_credentials(self, conn, config, exclude=""):
        for row in conn.execute("SELECT config FROM targets WHERE kind='qq' AND enabled=1 AND deleted IS NULL AND id!=?", (exclude,)):
            existing = self.store.decrypt(row[0])
            if existing.get("app_id") == config.get("app_id") and existing.get("app_secret") != config.get("app_secret"):
                raise HubError("同一 QQ AppID 的密钥必须一致；轮换时先停用其他同 AppID 目标，再逐个更新")

    def set_enabled(self, table, item_id, enabled):
        if table not in {"targets", "apps"}:
            raise HubError("不支持的对象")
        with self.store.connect(True) as conn:
            if table == "targets" and enabled:
                target = self.target(item_id, conn)
                if target["kind"] == "qq":
                    self._check_qq_credentials(conn, target["config"], item_id)
            if not conn.execute(f"UPDATE {table} SET enabled=? WHERE id=? AND deleted IS NULL", (enabled, item_id)).rowcount:
                raise HubError("对象不存在", 404)
        self.store.audit(table + ".enabled", item_id, self.clock())

    def check_targets(self, conn, ids, allow_unverified=False):
        if not ids or len(ids) > 50:
            raise HubError("请选择 1–50 个收件目标")
        targets = [self.target(i, conn) for i in dict.fromkeys(ids)]
        if any(not t["enabled"] or (not allow_unverified and not t["verified"]) for t in targets):
            raise HubError("所选目标包含未验证或已停用的目标")
        return targets

    def save_profile(self, data, profile_id=None):
        with self.store.connect(True) as conn:
            self.check_targets(conn, data.target_ids)
            profile_id = profile_id or uid()
            conn.execute("INSERT INTO profiles VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,targets=excluded.targets,require_ack=excluded.require_ack,ack_minutes=excluded.ack_minutes", (profile_id, data.name, json.dumps(list(dict.fromkeys(data.target_ids))), data.require_ack, data.ack_minutes))
        return {"id": profile_id}

    def create_app(self, data):
        token, app_id = "ph_" + secrets.token_urlsafe(32), uid()
        with self.store.connect(True) as conn:
            if data.target_ids:
                self.check_targets(conn, data.target_ids)
            self._check_default_profile(data.default_profile_id)
            conn.execute("INSERT INTO apps(id,name,token_hash,allowed,created,require_ack,ack_minutes) VALUES (?,?,?,?,?,?,?)", (app_id, data.name, digest(token), json.dumps(list(dict.fromkeys(data.target_ids))), self.clock(), data.require_ack, data.ack_minutes))
        self.store.audit("app.create", app_id, self.clock())
        return {"id": app_id, "api_key": token}

    def _check_default_profile(self, profile_id):
        if profile_id:
            raise HubError("上游不再支持共享分发方案，请在下游配置中直接选择渠道", 400)

    def rotate_app_key(self, app_id):
        token = "ph_" + secrets.token_urlsafe(32)
        with self.store.connect(True) as conn:
            if not conn.execute("UPDATE apps SET token_hash=? WHERE id=? AND deleted IS NULL", (digest(token), app_id)).rowcount:
                raise HubError("应用不存在", 404)
            conn.execute("INSERT INTO audit(at,action,subject) VALUES (?,'app.rotate_key',?)", (self.clock(), app_id))
        return {"id": app_id, "api_key": token}

    def update_app(self, app_id, data):
        with self.store.connect(True) as conn:
            current = conn.execute("SELECT * FROM apps WHERE id=? AND deleted IS NULL", (app_id,)).fetchone()
            if not current:
                raise HubError("应用不存在", 404)
            if data.target_ids:
                self.check_targets(conn, data.target_ids)
            self._check_default_profile(data.default_profile_id)
            ack = data.require_ack if 'require_ack' in data.model_fields_set else current['require_ack']
            minutes = data.ack_minutes if 'ack_minutes' in data.model_fields_set else current['ack_minutes']
            conn.execute("UPDATE apps SET name=?,allowed=?,default_profile_id=NULL,require_ack=?,ack_minutes=?,route_error='' WHERE id=?", (data.name, json.dumps(list(dict.fromkeys(data.target_ids))), ack, minutes, app_id))
            conn.execute("INSERT INTO audit(at,action,subject) VALUES (?,'app.update',?)", (self.clock(), app_id))
        return {"id": app_id}

    def deletion_info(self, table, item_id, conn=None):
        if table not in {"targets", "apps"}:
            raise HubError("不支持的对象")
        if conn is None:
            with self.store.connect() as db:
                return self.deletion_info(table, item_id, db)
        row = conn.execute(f"SELECT * FROM {table} WHERE id=? AND deleted IS NULL", (item_id,)).fetchone()
        if not row:
            raise HubError("对象不存在", 404)
        condition = "d.target_id=?" if table == "targets" else "m.app_id=?"
        counts = {r[0]: r[1] for r in conn.execute(f"SELECT d.state,count(*) FROM deliveries d JOIN messages m ON m.id=d.message_id WHERE {condition} GROUP BY d.state", (item_id,))}
        info = {"id": item_id, "name": row["name"], "enabled": bool(row["enabled"]), "pending": counts.get("queued", 0) + counts.get("retry", 0), "sending": counts.get("sending", 0), "history": sum(counts.values())}
        info["pending"] += conn.execute(f"SELECT count(*) FROM weixin_context_waits w JOIN deliveries d ON d.id=w.delivery_id JOIN messages m ON m.id=d.message_id WHERE {condition} AND d.state='blocked'", (item_id,)).fetchone()[0]
        info["awaiting_messages"] = conn.execute("SELECT count(*) FROM message_routes r JOIN messages m ON m.id=r.message_id WHERE m.app_id=? AND r.state='awaiting_routing'", (item_id,)).fetchone()[0] if table == "apps" else 0
        if table == "targets":
            info["verified"] = bool(row["verified"])
            info["profiles"] = [r["name"] for r in conn.execute("SELECT name,targets FROM profiles") if item_id in json.loads(r["targets"])]
            info["apps"] = [r["name"] for r in conn.execute("SELECT name,allowed FROM apps WHERE deleted IS NULL") if item_id in json.loads(r["allowed"])]
            setting = conn.execute("SELECT value FROM settings WHERE key='alert_target_id'").fetchone()
            info["alert_target"] = bool(setting and json.loads(setting[0]) == item_id)
        return info

    def delete_item(self, table, item_id):
        # Retain an inert identity for historic joins, remove the operational
        # credentials and revoke access in the same transaction as cancellation.
        now = self.clock()
        with self.store.connect(True) as conn:
            info = self.deletion_info(table, item_id, conn)
            if info["enabled"] and not (table == "targets" and not info["verified"]):
                raise HubError("请先停用，再删除", 409)
            if info["sending"]:
                raise HubError("仍有正在发送的任务，请等待结果后再删除", 409)
            condition = "d.target_id=?" if table == "targets" else "m.app_id=?"
            pending = conn.execute(f"SELECT d.id FROM deliveries d JOIN messages m ON m.id=d.message_id WHERE {condition} AND (d.state IN ('queued','retry') OR (d.state='blocked' AND d.id IN (SELECT delivery_id FROM weixin_context_waits)))", (item_id,)).fetchall()
            detail = "收件目标已删除，取消待发送任务" if table == "targets" else "上游应用已删除，取消待发送任务"
            for row in pending:
                conn.execute("UPDATE deliveries SET state='cancelled',completed=?,last_error=? WHERE id=?", (now, detail, row["id"]))
                conn.execute("DELETE FROM weixin_context_waits WHERE delivery_id=?", (row["id"],))
                conn.execute("UPDATE alerts SET resolved=1 WHERE delivery_id=?", (row["id"],))
                conn.execute("INSERT INTO attempts(delivery_id,at,state,detail) VALUES (?,?,'cancelled',?)", (row["id"], now, detail))
            if table == "targets":
                conn.execute("UPDATE targets SET deleted=?,enabled=0,verified=0,config=?,revision=revision+1,verify_hash=NULL,verify_expires=NULL,binding_hash=NULL,binding_expires=NULL WHERE id=?", (now, self.store.encrypt({}), item_id))
                for app in conn.execute("SELECT id,allowed FROM apps WHERE deleted IS NULL").fetchall():
                    allowed = json.loads(app["allowed"])
                    if item_id in allowed:
                        conn.execute("UPDATE apps SET allowed=? WHERE id=?", (json.dumps([i for i in allowed if i != item_id]), app["id"]))
                if info["alert_target"]:
                    conn.execute("UPDATE settings SET value='\"\"' WHERE key='alert_target_id'")
                # Profiles deliberately retain missing IDs: silently shrinking a
                # route would report success while omitting intended recipients.
            else:
                conn.execute("UPDATE apps SET deleted=?,enabled=0,token_hash=?,allowed='[]',default_profile_id=NULL WHERE id=?", (now, digest('deleted-' + uid()), item_id))
                conn.execute("UPDATE message_routes SET state='cancelled' WHERE state='awaiting_routing' AND message_id IN (SELECT id FROM messages WHERE app_id=?)", (item_id,))
            conn.execute("INSERT INTO audit(at,action,subject) VALUES (?,?,?)", (now, table + ".delete", item_id))
        return {"deleted": True, "cancelled": len(pending)} | ({"cancelled_messages": info["awaiting_messages"]} if info["awaiting_messages"] else {})

    def authenticate_app(self, token):
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM apps WHERE token_hash=? AND enabled=1 AND deleted IS NULL", (digest(token),)).fetchone()
        return dict(row) if row else None

    def submit(self, data: MessageInput, app=None, purpose="notification", use_app_targets=False):
        now = self.clock()
        app_id = app["id"] if app else "admin"
        canonical = data.model_dump(mode="json")
        canonical["target_ids"] = sorted(set(data.target_ids))
        fingerprint = digest(json.dumps(canonical, sort_keys=True, ensure_ascii=False))
        with self.store.connect(True) as conn:
            previous = conn.execute("SELECT id,fingerprint,purpose FROM messages WHERE app_id=? AND request_id=?", (app_id, data.request_id)).fetchone()
            if previous:
                if previous["fingerprint"] != fingerprint or previous["purpose"] != purpose:
                    raise HubError("同一 request_id 不能用于不同消息", 409)
                return {"id": previous["id"], "deduplicated": True, "routing_state": self.routing_state(conn, previous["id"])}
            ids, require_ack, ack_minutes = data.target_ids, data.require_ack, data.ack_minutes
            live_app = None
            if app:
                live_app = conn.execute("SELECT * FROM apps WHERE id=? AND enabled=1 AND deleted IS NULL", (app_id,)).fetchone()
                if not live_app:
                    raise HubError("应用已停用或删除", 403)
            profile_id = data.profile_id
            if live_app:
                if profile_id:
                    raise HubError("上游不再支持 profile_id，请使用本上游的下游配置或 target_ids", 400)
                if live_app["route_error"]:
                    raise HubError(live_app["route_error"], 409)
                if not ids:
                    ids = json.loads(live_app["allowed"])
                if require_ack is None:
                    require_ack = bool(live_app["require_ack"])
                if ack_minutes is None:
                    ack_minutes = live_app["ack_minutes"]
            if profile_id:
                if ids:
                    raise HubError("target_ids 与 profile_id 只能选择一种")
                profile = conn.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
                if not profile:
                    raise HubError("分发方案不存在", 404)
                ids = json.loads(profile["targets"])
                if require_ack is None:
                    require_ack = bool(profile["require_ack"])
                if ack_minutes is None:
                    ack_minutes = profile["ack_minutes"]
            ids = list(dict.fromkeys(ids))
            if app:
                if not live_app or not set(ids) <= set(json.loads(live_app["allowed"])):
                    raise HubError("应用没有向这些目标发送消息的权限", 403)
            awaiting = bool(live_app and not ids and not profile_id and not json.loads(live_app["allowed"]) and not data.target_ids and not data.profile_id)
            targets = [] if awaiting else self.check_targets(conn, ids, allow_unverified=purpose == "verification")
            if not awaiting and require_ack and not self.store.setting("public_base_url"):
                raise HubError("要求收件确认时，请先配置收件人能访问的平台地址")
            scheduled = data.scheduled_at.timestamp() if data.scheduled_at else now
            expires = data.expires_at.timestamp() if data.expires_at else max(now, scheduled) + 86400
            if scheduled > now + 366 * 86400 or expires <= max(now, scheduled):
                raise HubError("过期时间必须晚于发送时间，预约时间不能超过一年")
            message_id = uid()
            stored_body = self.store.encrypt(data.body) if purpose == "verification" else data.body
            conn.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (message_id, app_id, data.request_id, fingerprint, data.title, stored_body, now, scheduled, expires, bool(require_ack), ack_minutes or 30, purpose))
            if awaiting:
                conn.execute("INSERT INTO message_routes(message_id,require_ack,ack_minutes) VALUES (?,?,?)", (message_id, data.require_ack, data.ack_minutes))
            self.enqueue_targets(conn, message_id, targets, max(now, scheduled), require_ack)
        return {"id": message_id, "deduplicated": False, "routing_state": "awaiting_routing" if awaiting else "routed"}

    def enqueue_targets(self, conn, message_id, targets, next_at, require_ack):
        for target in targets:
            secret = secrets.token_urlsafe(32) if require_ack else ""
            conn.execute("INSERT INTO deliveries(id,message_id,target_id,revision,next_at,ack_hash,ack_secret) VALUES (?,?,?,?,?,?,?)", (uid(), message_id, target["id"], target["revision"], next_at, digest(secret) if secret else None, self.store.encrypt(secret) if secret else None))

    def routing_state(self, conn, message_id):
        row = conn.execute("SELECT r.state,m.expires FROM message_routes r JOIN messages m ON m.id=r.message_id WHERE r.message_id=?", (message_id,)).fetchone()
        if not row:
            return "routed"
        return "expired" if row["state"] == "awaiting_routing" and row["expires"] <= self.clock() else row["state"]

    def release_message(self, message_id):
        now = self.clock()
        with self.store.connect(True) as conn:
            message = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            route = conn.execute("SELECT * FROM message_routes WHERE message_id=?", (message_id,)).fetchone()
            if not message or not route:
                raise HubError("消息不属于待配置分发队列", 404)
            if route["state"] == "routed":
                return {"id": message_id, "deduplicated": True}
            if route["state"] != "awaiting_routing" or message["expires"] <= now:
                raise HubError("消息已取消或过期，不能继续分发", 409)
            app = conn.execute("SELECT * FROM apps WHERE id=? AND deleted IS NULL AND enabled=1", (message["app_id"],)).fetchone()
            if not app:
                raise HubError("上游应用已停用或删除", 409)
            allowed = json.loads(app["allowed"])
            ids, ack, minutes = allowed, route["require_ack"], route["ack_minutes"]
            if app["route_error"]:
                raise HubError(app["route_error"], 409)
            if ack is None:
                ack = app["require_ack"]
            if minutes is None:
                minutes = app["ack_minutes"]
            if not ids:
                raise HubError("请先在上游分发中配置下游渠道", 409)
            if not set(ids) <= set(allowed):
                raise HubError("分发方案包含未授权目标", 403)
            targets = self.check_targets(conn, ids)
            if ack and not self.store.setting("public_base_url"):
                raise HubError("要求收件确认时，请先配置收件人能访问的平台地址")
            conn.execute("UPDATE messages SET require_ack=?,ack_minutes=? WHERE id=?", (bool(ack), minutes or 30, message_id))
            self.enqueue_targets(conn, message_id, targets, max(now, message["scheduled"]), ack)
            conn.execute("UPDATE message_routes SET state='routed' WHERE message_id=?", (message_id,))
            conn.execute("INSERT INTO audit(at,action,subject) VALUES (?,'message.route',?)", (now, message_id))
        return {"id": message_id, "deduplicated": False}

    def verify_start(self, target_id):
        target = self.target(target_id)
        if not target["enabled"]:
            raise HubError("请先启用此目标")
        if target["kind"] == "email" and target["config"].get("provider") == "outlook" and (not target["config"].get("oauth_cache") or target["config"].get("oauth_error")):
            raise HubError("请先连接微软邮箱，再发送收件验证码")
        if self.store.limited("verify:" + target_id, 3, 600, self.clock()):
            raise HubError("10 分钟内最多发送 3 次验证消息", 429)
        with self.store.connect(True) as conn:
            if not self.target(target_id, conn)["enabled"]:
                raise HubError("请先启用此目标")
            # Active codes are distinct across targets so an accidental random collision
            # cannot confirm a different recipient.
            for _ in range(20):
                code = f"{secrets.randbelow(1_000_000):06d}"
                if not conn.execute("SELECT 1 FROM targets WHERE verify_hash=? AND verify_expires>? AND id!=?", (digest(code), self.clock(), target_id)).fetchone():
                    break
            else:
                raise HubError("验证码生成繁忙，请稍后重试")
            conn.execute("UPDATE targets SET verify_hash=?,verify_expires=?,verify_attempts=0 WHERE id=?", (digest(code), self.clock() + 600, target_id))
        label = f"{'QQ' if target['kind'] == 'qq' else '微信'} 收件目标：{target['name']}\n此消息用于测试连接并区分收件目标。\n\n" if target['kind'] in {'qq', 'weixin'} else ''
        # Some mail previews remove line breaks entirely. Keep punctuation and
        # use words for the lifetime so it cannot become part of the code.
        data = MessageInput(title=f"{PRODUCT_NAME} 收件验证", body=label + f"您的收件验证码：{code}（六位数字）。\n有效期：十分钟。\n请在{PRODUCT_NAME}填写以上六位数字完成验证。", target_ids=[target_id], request_id="verify-" + uid())
        return self.submit(data, purpose="verification")

    def qq_test_status(self, target_id):
        with self.store.connect() as conn:
            row = conn.execute("SELECT m.id,d.state,d.last_error,d.completed,m.created,(d.state='blocked' AND d.id IN (SELECT delivery_id FROM weixin_context_waits)) AS waiting_for_context,(t.verify_hash IS NOT NULL AND t.verify_expires>? AND t.verify_attempts<5) AS code_active FROM deliveries d JOIN messages m ON m.id=d.message_id JOIN targets t ON t.id=d.target_id WHERE d.target_id=? AND d.revision=t.revision AND m.purpose='verification' ORDER BY m.created DESC,m.rowid DESC LIMIT 1", (self.clock(), target_id)).fetchone()
            if not row:
                return None
            confirmed = conn.execute("SELECT 1 FROM audit a JOIN targets t ON t.id=a.subject WHERE a.action='target.verified' AND a.subject=? AND a.at>=? AND t.verified=1 AND t.verify_hash IS NULL LIMIT 1", (target_id, row["created"])).fetchone()
        return dict(row) | {"receipt_confirmed": bool(confirmed)}

    def verify_finish(self, target_id, code):
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
            raise HubError("请输入6位数字收件验证码")
        error = None
        with self.store.connect(True) as conn:
            target = self.target(target_id, conn)
            if not target["verify_hash"] or target["verify_expires"] < self.clock() or target["verify_attempts"] >= 5:
                error = "验证码已失效，请重新发送"
            else:
                conn.execute("UPDATE targets SET verify_attempts=verify_attempts+1 WHERE id=?", (target_id,))
                if not hmac.compare_digest(target["verify_hash"], digest(code)):
                    error = "验证码不正确"
                else:
                    conn.execute("UPDATE targets SET verified=1,verify_hash=NULL WHERE id=?", (target_id,))
        if error:
            raise HubError(error)
        self.store.audit("target.verified", target_id, self.clock())
        return {"verified": True}

    def message(self, message_id, app_id=None):
        with self.store.connect() as conn:
            row = conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            if not row or (app_id and row["app_id"] != app_id):
                raise HubError("消息不存在", 404)
            result = dict(row)
            result.pop("fingerprint")
            result["routing_state"] = self.routing_state(conn, message_id)
            if result["purpose"] == "verification":
                result["body"] = "验证码已发送到收件渠道；后台不展示验证码。"
            deliveries = conn.execute("SELECT d.*,t.name target_name,t.kind FROM deliveries d JOIN targets t ON t.id=d.target_id WHERE message_id=? ORDER BY d.rowid", (message_id,)).fetchall()
            result["deliveries"] = [{k: r[k] for k in r.keys() if k not in {"ack_hash", "ack_secret"}} for r in deliveries]
            for row in result["deliveries"]:
                row["waiting_for_context"] = row["state"] == "blocked" and conn.execute("SELECT 1 FROM weixin_context_waits WHERE delivery_id=?", (row["id"],)).fetchone() is not None
                row["history"] = [dict(r) for r in conn.execute("SELECT at,state,detail FROM attempts WHERE delivery_id=? ORDER BY id", (row["id"],))]
            return result

    def _alert(self, conn, delivery, reason, detail):
        purpose = conn.execute("SELECT purpose FROM messages WHERE id=?", (delivery["message_id"],)).fetchone()[0]
        conn.execute("INSERT INTO alerts(id,delivery_id,reason,detail,created,notified) VALUES (?,?,?,?,?,?) ON CONFLICT(delivery_id,reason) DO UPDATE SET detail=excluded.detail,created=excluded.created,resolved=0,notified=excluded.notified WHERE alerts.resolved=1", (uid(), delivery["id"], reason, detail, self.clock(), int(purpose == "alert")))

    def _finish(self, delivery, state, detail="", provider_id="", *, wait_context_hash=None):
        now = self.clock()
        with self.store.connect(True) as conn:
            current = conn.execute("SELECT state FROM deliveries WHERE id=?", (delivery["id"],)).fetchone()[0]
            if current in {"accepted", "acknowledged", "cancelled", "expired"}:
                return
            expires = conn.execute("SELECT expires FROM messages WHERE id=?", (delivery["message_id"],)).fetchone()[0]
            if (state == "retry" or wait_context_hash is not None) and expires <= now:
                state, detail = "expired", "消息已到投递截止时间，停止自动重试"
            conn.execute("DELETE FROM weixin_context_waits WHERE delivery_id=?", (delivery["id"],))
            waiting = state == "blocked" and wait_context_hash is not None
            if waiting:
                conn.execute("INSERT INTO weixin_context_waits VALUES (?,?,?)", (delivery["id"], wait_context_hash, now))
            # Safe connection failures remain durable until the message deadline.
            # Cap the delay, so a long outage does not create a very long wait.
            next_at = min(expires, now + (30 if delivery["attempts"] <= 1 else 60))
            conn.execute("UPDATE deliveries SET state=?,last_error=?,provider_id=?,next_at=?,completed=?,accepted_at=CASE WHEN ?='accepted' THEN ? ELSE accepted_at END,ack_deadline=CASE WHEN ?='accepted' THEN ?+(SELECT ack_minutes*60 FROM messages WHERE id=message_id) ELSE ack_deadline END WHERE id=?", (state, detail, provider_id, next_at, None if state == "retry" or waiting else now, state, now, state, now, delivery["id"]))
            conn.execute("INSERT INTO attempts(delivery_id,at,state,detail) VALUES (?,?,?,?)", (delivery["id"], now, state, detail))
            if state == "retry" and delivery["attempts"] >= 3:
                self._alert(conn, delivery, "retrying", "连续连接失败或限流，正在自动重试；恢复后自动补发，投递截止后停止")
            elif state != "retry":
                conn.execute("UPDATE alerts SET resolved=1 WHERE delivery_id=? AND reason='retrying'", (delivery["id"],))
            if state in {"failed", "unknown", "blocked", "expired"}:
                self._alert(conn, delivery, state, detail)
            if state == "accepted":
                conn.execute("UPDATE alerts SET resolved=1 WHERE delivery_id=?", (delivery["id"],))

    def _resume_weixin_context_waits(self, conn, now):
        # Runs under the same write lock as claiming a delivery. The hash is of
        # the context actually used by the rejected request, even when polling
        # obtains a new context while that request is in flight.
        rows = conn.execute("""SELECT d.*,w.context_hash,m.purpose,m.app_id,
            t.config,t.revision current_revision,t.enabled,t.verified,t.deleted,t.kind,
            t.verify_hash,t.verify_expires
            FROM weixin_context_waits w JOIN deliveries d ON d.id=w.delivery_id
            JOIN messages m ON m.id=d.message_id JOIN targets t ON t.id=d.target_id""").fetchall()
        for row in rows:
            if row["state"] != "blocked":
                conn.execute("DELETE FROM weixin_context_waits WHERE delivery_id=?", (row["id"],))
                continue
            invalid = row["deleted"] is not None or row["kind"] != "weixin" or row["revision"] != row["current_revision"]
            if row["app_id"] != "admin":
                invalid |= conn.execute("SELECT 1 FROM apps WHERE id=? AND deleted IS NULL", (row["app_id"],)).fetchone() is None
            if invalid:
                detail = "渠道配置或上游已变化，已停止等待微信会话；请重新检查后处理"
                conn.execute("DELETE FROM weixin_context_waits WHERE delivery_id=?", (row["id"],))
                conn.execute("UPDATE deliveries SET completed=?,last_error=? WHERE id=?", (now, detail, row["id"]))
                conn.execute("INSERT INTO attempts(delivery_id,at,state,detail) VALUES (?,?,'blocked',?)", (row["id"], now, detail))
                continue
            if not row["enabled"]:
                continue
            if row["purpose"] == "verification":
                if not row["verify_hash"] or (row["verify_expires"] or 0) <= now:
                    detail = "验证码已过期或已使用，停止等待微信会话"
                    conn.execute("DELETE FROM weixin_context_waits WHERE delivery_id=?", (row["id"],))
                    conn.execute("UPDATE deliveries SET state='expired',completed=?,last_error=? WHERE id=?", (now, detail, row["id"]))
                    conn.execute("UPDATE alerts SET resolved=1 WHERE delivery_id=?", (row["id"],))
                    conn.execute("INSERT INTO attempts(delivery_id,at,state,detail) VALUES (?,?,'expired',?)", (row["id"], now, detail))
                    continue
            elif not row["verified"]:
                continue
            config = self.store.decrypt(row["config"])
            context = config.get("context_token", "")
            if not context or digest(context) == row["context_hash"]:
                continue
            conn.execute("DELETE FROM weixin_context_waits WHERE delivery_id=?", (row["id"],))
            conn.execute("UPDATE deliveries SET state='queued',next_at=?,completed=NULL,last_error='' WHERE id=?", (now, row["id"]))
            conn.execute("UPDATE alerts SET resolved=1 WHERE delivery_id=?", (row["id"],))
            conn.execute("INSERT INTO attempts(delivery_id,at,state,detail) VALUES (?,?,'queued','微信新会话已更新，自动续发未过期消息')", (row["id"], now))

    def recover(self):
        # Called only after exclusive process lock acquisition. Never auto-resend
        # a send interrupted by process termination: acceptance is unknowable.
        with self.store.connect(True) as conn:
            for delivery in conn.execute("SELECT * FROM deliveries WHERE state='sending'").fetchall():
                detail = "进程在发送期间退出；请核实收件结果后再处理"
                conn.execute("UPDATE deliveries SET state='unknown',last_error=? WHERE id=?", (detail, delivery["id"]))
                conn.execute("INSERT INTO attempts(delivery_id,at,state,detail) VALUES (?,?,'unknown',?)", (delivery["id"], self.clock(), detail))
                self._alert(conn, delivery, "unknown", detail)

    def tick(self):
        now = self.clock()
        with self.store.connect(True) as conn:
            conn.execute("UPDATE message_routes SET state='expired' WHERE state='awaiting_routing' AND message_id IN (SELECT id FROM messages WHERE expires<=?)", (now,))
            expired = conn.execute("SELECT d.* FROM deliveries d JOIN messages m ON m.id=d.message_id WHERE (d.state IN ('queued','retry') OR (d.state='blocked' AND d.id IN (SELECT delivery_id FROM weixin_context_waits))) AND m.expires<=?", (now,)).fetchall()
            for item in expired:
                detail = "消息已到投递截止时间，停止待发送和自动重试"
                conn.execute("UPDATE deliveries SET state='expired',completed=?,last_error=? WHERE id=?", (now, detail, item["id"]))
                conn.execute("DELETE FROM weixin_context_waits WHERE delivery_id=?", (item["id"],))
                conn.execute("INSERT INTO attempts(delivery_id,at,state,detail) VALUES (?,?,'expired',?)", (item["id"], now, detail))
                conn.execute("UPDATE alerts SET resolved=1 WHERE delivery_id=?", (item["id"],))
                self._alert(conn, item, "expired", detail)
            self._resume_weixin_context_waits(conn, now)
            for stalled in conn.execute("SELECT * FROM deliveries WHERE state='sending' AND started<?", (now - 180,)).fetchall():
                detail = "发送状态超过 3 分钟未完成，请核实结果；未自动重发"
                conn.execute("UPDATE deliveries SET state='unknown',last_error=? WHERE id=?", (detail, stalled["id"]))
                self._alert(conn, stalled, "unknown", detail)
            for row in conn.execute("SELECT * FROM deliveries WHERE state='accepted' AND ack_hash IS NOT NULL AND ack_deadline<?", (now,)).fetchall():
                self._alert(conn, row, "ack_overdue", "渠道已接受，但收件人未在约定时间确认")
            row = conn.execute("SELECT * FROM deliveries WHERE state IN ('queued','retry') AND next_at<=? ORDER BY next_at,rowid LIMIT 1", (now,)).fetchone()
            if not row:
                return bool(expired)
            delivery = dict(row)
            conn.execute("UPDATE deliveries SET state='sending',started=?,attempts=attempts+1 WHERE id=?", (now, delivery["id"]))
            delivery["attempts"] += 1
            message = dict(conn.execute("SELECT * FROM messages WHERE id=?", (delivery["message_id"],)).fetchone())
            target = self.target(delivery["target_id"], conn)
        if message["expires"] <= now:
            self._finish(delivery, "expired", "消息已超过有效期，没有发送")
            return True
        if not target["enabled"] or target["revision"] != delivery["revision"] or (message["purpose"] != "verification" and not target["verified"]):
            self._finish(delivery, "blocked", "目标已停用、配置已变化或验证已失效；请重新检查")
            return True
        if message["purpose"] == "verification":
            message["body"] = self.store.decrypt(message["body"])
            code = re.search(r"验证码：([0-9]{6})(?![0-9])", message["body"])
            if not target["verify_hash"] or target["verify_expires"] <= now or not code or digest(code[1]) != target["verify_hash"]:
                self._finish(delivery, "expired", "验证码已过期、已使用或已被新验证码替换")
                return True
        if delivery["ack_secret"]:
            base = self.store.setting("public_base_url")
            message["ack_url"] = base.rstrip("/") + "/ack/" + self.store.decrypt(delivery["ack_secret"])
        try:
            provider_id = self.sender(target["kind"], self.effective_config(target), message, delivery["id"])
        except adapters.DeliveryError as error:
            context_hash = digest(target["config"].get("context_token", "")) if error.wait_for_context and target["kind"] == "weixin" else None
            self._finish(delivery, error.kind, str(error), wait_context_hash=context_hash)
        except HubError as error:
            self._finish(delivery, "blocked", str(error))
        except Exception:
            self._finish(delivery, "unknown", "发送过程出现未预期错误，结果待核实")
        else:
            self._finish(delivery, "accepted", provider_id=provider_id or "")
        return True

    def retry(self, delivery_id, accept_duplicate_risk=False):
        with self.store.connect(True) as conn:
            row = conn.execute("SELECT d.*,m.expires,m.purpose,m.app_id FROM deliveries d JOIN messages m ON m.id=d.message_id WHERE d.id=?", (delivery_id,)).fetchone()
            if not row:
                raise HubError("投递记录不存在", 404)
            if row["state"] not in {"failed", "blocked", "unknown"}:
                raise HubError("仅失败、受阻或结果未知的投递可以重新发送")
            if row["state"] == "unknown" and not accept_duplicate_risk:
                raise HubError("结果未知，重新发送可能重复。请先核实，再明确选择重新发送", 409)
            if conn.execute("SELECT 1 FROM weixin_context_waits WHERE delivery_id=?", (delivery_id,)).fetchone():
                raise HubError("正在等待微信新会话，请向机器人发一条消息，未过期任务将自动续发", 409)
            if row["expires"] <= self.clock():
                raise HubError("消息已过期，请创建新消息")
            if row["app_id"] != "admin":
                application = conn.execute("SELECT deleted FROM apps WHERE id=?", (row["app_id"],)).fetchone()
                if not application or application["deleted"] is not None:
                    raise HubError("原应用已删除，请重新创建消息")
            target = self.target(row["target_id"], conn)
            if not target["enabled"] or (not target["verified"] and row["purpose"] != "verification"):
                raise HubError("请先启用并验证目标")
            conn.execute("UPDATE deliveries SET state='queued',attempts=0,next_at=?,revision=?,completed=NULL,last_error='' WHERE id=?", (self.clock(), target["revision"], delivery_id))
            conn.execute("UPDATE alerts SET resolved=1 WHERE delivery_id=?", (delivery_id,))
            conn.execute("INSERT INTO attempts(delivery_id,at,state,detail) VALUES (?,?,'queued','管理员明确请求重新发送')", (delivery_id, self.clock()))
        self.store.audit("delivery.retry", delivery_id, self.clock())

    def confirm_received(self, delivery_id):
        """An administrator records explicit recipient feedback without resending."""
        now = self.clock()
        with self.store.connect(True) as conn:
            row = conn.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
            if not row:
                raise HubError("投递记录不存在", 404)
            if row["state"] == "acknowledged":
                return {"state": "acknowledged", "message_id": row["message_id"]}
            if row["state"] not in {"accepted", "unknown"}:
                raise HubError("只能核实已发送或结果待核实的投递", 409)
            conn.execute("UPDATE deliveries SET state='acknowledged',acked_at=?,last_error='' WHERE id=?", (now, delivery_id))
            conn.execute("UPDATE alerts SET resolved=1 WHERE delivery_id=?", (delivery_id,))
            conn.execute("INSERT INTO attempts(delivery_id,at,state,detail) VALUES (?,?,'acknowledged','管理员根据收件人反馈登记已收到；未重新发送。原始发送记录保留。')", (delivery_id, now))
            conn.execute("INSERT INTO audit(at,action,subject) VALUES (?,'delivery.receipt_confirmed',?)", (now, delivery_id))
        return {"state": "acknowledged", "message_id": row["message_id"]}

    def cancel(self, message_id):
        with self.store.connect(True) as conn:
            pending = conn.execute("SELECT id FROM deliveries WHERE message_id=? AND (state IN ('queued','retry') OR (state='blocked' AND id IN (SELECT delivery_id FROM weixin_context_waits)))", (message_id,)).fetchall()
            count = conn.execute("UPDATE deliveries SET state='cancelled',completed=?,last_error='' WHERE message_id=? AND (state IN ('queued','retry') OR (state='blocked' AND id IN (SELECT delivery_id FROM weixin_context_waits)))", (self.clock(), message_id)).rowcount
            for row in pending:
                conn.execute("INSERT INTO attempts(delivery_id,at,state,detail) VALUES (?,?,'cancelled','管理员取消待发送任务')", (row["id"], self.clock()))
            conn.execute("DELETE FROM weixin_context_waits WHERE delivery_id IN (SELECT id FROM deliveries WHERE message_id=? AND state='cancelled')", (message_id,))
            held = conn.execute("UPDATE message_routes SET state='cancelled' WHERE message_id=? AND state='awaiting_routing'", (message_id,)).rowcount
            conn.execute("UPDATE alerts SET resolved=1 WHERE delivery_id IN (SELECT id FROM deliveries WHERE message_id=? AND state='cancelled')", (message_id,))
        return {"cancelled": count} | ({"cancelled_messages": held} if held else {})

    def acknowledge(self, token, mutate=True):
        with self.store.connect(mutate) as conn:
            row = conn.execute("SELECT d.*,m.title FROM deliveries d JOIN messages m ON m.id=d.message_id WHERE ack_hash=?", (digest(token),)).fetchone()
            if not row or row["state"] not in {"accepted", "acknowledged", "unknown", "sending"} or (row["accepted_at"] or row["started"] or 0) < self.clock() - 30 * 86400:
                raise HubError("确认链接无效或已过期", 404)
            if mutate:
                conn.execute("UPDATE deliveries SET state='acknowledged',acked_at=COALESCE(acked_at,?) WHERE id=?", (self.clock(), row["id"]))
                conn.execute("UPDATE alerts SET resolved=1 WHERE delivery_id=?", (row["id"],))
            return {"title": row["title"], "acknowledged": mutate or row["state"] == "acknowledged"}

    def notify_alerts(self):
        target_id = self.store.setting("alert_target_id")
        if not target_id:
            return
        with self.store.connect() as conn:
            rows = conn.execute("SELECT a.*,d.target_id FROM alerts a JOIN deliveries d ON d.id=a.delivery_id WHERE a.notified=0 AND a.resolved=0 ORDER BY a.created LIMIT 10").fetchall()
        for row in rows:
            if row["target_id"] == target_id:
                continue
            try:
                self.submit(MessageInput(title=f"{PRODUCT_NAME} 投递告警", body=f"{row['detail']}\n投递编号：{row['delivery_id']}\n请进入平台的告警中心处理。", target_ids=[target_id], request_id="alert-" + row["id"]), purpose="alert")
            except HubError:
                continue
            with self.store.connect(True) as conn:
                conn.execute("UPDATE alerts SET notified=1 WHERE id=?", (row["id"],))

    def state(self, offset=0):
        with self.store.connect() as conn:
            targets = [self.public_target(r) for r in conn.execute("SELECT * FROM targets WHERE deleted IS NULL ORDER BY created")]
            apps = [{"id": r["id"], "name": r["name"], "enabled": r["enabled"], "target_ids": json.loads(r["allowed"]), "default_profile_id": None, "require_ack": bool(r["require_ack"]), "ack_minutes": r["ack_minutes"], "route_error": r["route_error"]} for r in conn.execute("SELECT * FROM apps WHERE deleted IS NULL ORDER BY created")]
            for row in apps:
                row["received_count"], row["last_received"] = conn.execute("SELECT count(*),max(created) FROM messages WHERE app_id=? AND purpose='notification'", (row["id"],)).fetchone()
                from .onboarding import progress
                row['setup'] = progress(self,conn,row)
                row["awaiting_count"] = conn.execute("SELECT count(*) FROM message_routes r JOIN messages m ON m.id=r.message_id WHERE m.app_id=? AND r.state='awaiting_routing' AND m.expires>?", (row["id"], self.clock())).fetchone()[0]
            profiles = [dict(r) for r in conn.execute("SELECT * FROM profiles ORDER BY rowid")]
            for row in profiles:
                row["target_ids"] = json.loads(row.pop("targets"))
            messages = [dict(r) for r in conn.execute("SELECT id,title,app_id,created,scheduled,purpose FROM messages ORDER BY created DESC LIMIT 50 OFFSET ?", (offset,))]
            for row in messages:
                row["states"] = {r[0]: r[1] for r in conn.execute("SELECT CASE WHEN state='blocked' AND id IN (SELECT delivery_id FROM weixin_context_waits) THEN 'waiting_context' ELSE state END display_state,count(*) FROM deliveries WHERE message_id=? GROUP BY display_state", (row["id"],))}
                row["routing_state"] = self.routing_state(conn, row["id"])
                if not row["states"] and row["routing_state"] != "routed":
                    row["states"] = {row["routing_state"]: 1}
            alerts = [dict(r) for r in conn.execute("SELECT a.*,t.name target_name,d.message_id FROM alerts a JOIN deliveries d ON d.id=a.delivery_id JOIN targets t ON t.id=d.target_id ORDER BY a.resolved,a.created DESC LIMIT 100")]
            stats = {r[0]: r[1] for r in conn.execute("SELECT CASE WHEN state='blocked' AND id IN (SELECT delivery_id FROM weixin_context_waits) THEN 'waiting_context' ELSE state END display_state,count(*) FROM deliveries GROUP BY display_state")}
            stats["awaiting_routing"] = conn.execute("SELECT count(*) FROM message_routes r JOIN messages m ON m.id=r.message_id WHERE r.state='awaiting_routing' AND m.expires>?", (self.clock(),)).fetchone()[0]
            total = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
        return {"targets": targets, "apps": apps, "profiles": profiles, "messages": messages, "message_total": total, "alerts": alerts, "stats": stats, "settings": {k: self.store.setting(k) for k in ("public_base_url", "alert_target_id")} | {"default_proxy_configured": bool(self.store.setting("default_proxy_id") or self.store.setting("default_proxy"))}, "heartbeat": self.store.setting("heartbeat", 0), "network": self.network.state(), "account": self.store.account(), "email": self.email.public_settings()}

    def effective_config(self, target):
        config = self.network.effective_config(target)
        if target["kind"] == "email" and config.get("provider") == "outlook":
            config.update(_email_target_id=target["id"], _email_revision=target["revision"])
        return config
