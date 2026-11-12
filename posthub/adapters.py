"""Small protocol adapters; general notification transports are delegated to Apprise.

Only an explicit rejection/connection failure is retryable. A timeout after a
request may mean the receiver accepted it, so the outcome remains unknown.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import time
from urllib.parse import quote, urlencode, urlsplit

import httpx

logging.getLogger("apprise").disabled = True
ILINK = "https://ilinkai.weixin.qq.com"
QQ_API = "https://api.bot.qq.com"


class DeliveryError(Exception):
    def __init__(self, detail, kind="failed", *, wait_for_context=False):
        super().__init__(detail)
        self.kind = kind
        self.wait_for_context = wait_for_context


def http_url(value, *, https_only=False):
    parsed = urlsplit(str(value))
    if parsed.scheme not in (("https",) if https_only else ("http", "https")) or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("请输入有效的 HTTP(S) 地址，不要在地址中放用户名和密码")
    return value


def ilink_base(value):
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or not (parsed.hostname == "ilinkai.weixin.qq.com" or parsed.hostname.endswith(".ilinkai.weixin.qq.com")) or parsed.port not in (None, 443) or parsed.username or parsed.password:
        raise ValueError("微信返回了不受信任的服务地址")
    return f"https://{parsed.hostname}"


def email_url(config):
    user, password = config.get("username", ""), config.get("password", "")
    auth = f"{quote(user, safe='')}:{quote(password, safe='')}@" if user else ""
    mode = config.get("security", "starttls")
    scheme = "mailto" if mode == "insecure" else "mailtos"
    query = urlencode({"from": config["sender"], "to": config["recipient"], "smtp": config["host"], "mode": mode, "timeout": 20})
    return f"{scheme}://{auth}{config['host']}:{config.get('port', 587)}/?{query}"


FIELDS = {
    "email": {"provider", "host", "port", "username", "password", "sender", "recipient", "security"},
    "wecom": {"webhook_url"}, "feishu": {"webhook_url", "secret"},
    "dingtalk": {"app_key", "app_secret", "robot_code", "target_type", "address"},
    "qq": {"app_id", "app_secret", "target_type", "address", "event_mode"},
    "weixin": set(), "webhook": {"url", "bearer_token"}, "apprise": {"url"},
}
PUBLIC_FIELDS = {"provider", "host", "port", "username", "sender", "recipient", "security", "app_id", "app_key", "robot_code", "target_type", "address", "network_mode", "proxy_id", "event_mode"}


def validate_config(kind, config):
    if kind not in FIELDS or set(config) - FIELDS[kind] - {"proxy", "network_mode", "proxy_id"}:
        raise ValueError("不支持的渠道配置字段")
    if config.get("network_mode", "default") not in {"default", "system", "custom", "profile"}:
        raise ValueError("网络方式无效")
    if config.get("network_mode") == "custom" and not config.get("proxy"):
        raise ValueError("单独代理模式必须填写代理地址")
    if config.get("network_mode") == "profile" and (not isinstance(config.get('proxy_id'), str) or not config['proxy_id']):
        raise ValueError("请选择已保存的代理")
    if len(json.dumps(config)) > 12000:
        raise ValueError("配置过大")
    if any(isinstance(value, str) and re.search(r"[\x00-\x1f\x7f]", value) for value in config.values()):
        raise ValueError("连接配置不能包含换行或控制字符")
    if kind == "qq" and not re.fullmatch(r"[0-9]{1,32}", str(config.get("app_id", ""))):
        raise ValueError("QQ AppID 必须是数字")
    if kind == "qq" and config.get("event_mode", "websocket") not in {"websocket", "webhook"}:
        raise ValueError("QQ 连接方式无效")
    if config.get("proxy"):
        from .network import validate_proxy
        validate_proxy(config["proxy"])
        if kind in {"email", "apprise"}:
            raise ValueError("邮箱与 Apprise 扩展渠道使用系统路由；按目标代理用于 HTTP 渠道")
    required = {"email": ("host", "sender", "recipient"), "wecom": ("webhook_url",), "feishu": ("webhook_url",), "dingtalk": ("app_key", "app_secret", "robot_code", "address"), "qq": ("app_id", "app_secret"), "weixin": (), "webhook": ("url",), "apprise": ("url",)}[kind]
    if kind == "email" and config.get("provider") == "outlook":
        required = ("sender", "recipient")
    if any(not isinstance(config.get(k), str) or not config[k].strip() for k in required):
        raise ValueError("请填写全部必填连接信息")
    if kind == "email":
        for field in ("sender", "recipient"):
            if not re.fullmatch(r"[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+", config[field]):
                raise ValueError("每个目标只能填写一个有效邮箱地址")
        if config.get("provider") == "outlook":
            return
        if not re.fullmatch(r"[a-zA-Z0-9.-]+", config["host"]):
            raise ValueError("SMTP 主机格式错误")
        if not 1 <= int(config.get("port", 587)) <= 65535:
            raise ValueError("SMTP 端口错误")
        if config.get("security", "starttls") not in {"starttls", "ssl", "insecure"}:
            raise ValueError("SMTP 加密模式错误")
        if config.get("security") == "insecure" and config.get("host") not in {"127.0.0.1", "localhost"}:
            raise ValueError("明文 SMTP 仅允许本机测试中继；远端请使用 SSL / STARTTLS")
    if kind in {"wecom", "feishu"}:
        url = http_url(config["webhook_url"], https_only=True)
        expected = "qyapi.weixin.qq.com" if kind == "wecom" else "open.feishu.cn"
        if urlsplit(url).hostname != expected:
            raise ValueError("请填写对应平台的官方机器人 Webhook")
    if kind == "webhook":
        http_url(config["url"])
    if kind in {"qq", "dingtalk"}:
        if config.get("target_type", "user") not in {"user", "group"}:
            raise ValueError("目标类型必须是 user 或 group")
        if config.get("address") and not re.fullmatch(r"[a-zA-Z0-9_+=.-]{1,256}", config["address"]):
            raise ValueError("平台目标标识格式错误")
    if kind in {"apprise", "email"}:
        import apprise
        obj = apprise.Apprise()
        url = email_url(config) if kind == "email" else config["url"]
        if not obj.add(url) or len(obj) != 1:
            raise ValueError("Apprise 无法识别此连接；每个目标只允许一个连接")


def client(config, timeout=25):
    # A per-target proxy never modifies process-wide network settings.
    return httpx.Client(proxy=config.get("proxy") or None, timeout=timeout, follow_redirects=False, trust_env=False)


def post_json(http, url, payload, headers=None, *, pre_send=False):
    try:
        response = http.post(url, json=payload, headers=headers)
    except httpx.ProxyError:
        # CONNECT / SOCKS negotiation failed before a business request was sent.
        raise DeliveryError("代理连接或协商失败，等待恢复后自动重试；请核对代理协议、端口及账号密码", "retry") from None
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as error:
        from .network import error_info
        _, detail, hint = error_info(error)
        raise DeliveryError(detail + "；" + hint, "retry") from None
    except httpx.HTTPError:
        raise DeliveryError("网络请求中断，结果待核实", "retry" if pre_send else "unknown") from None
    if response.status_code == 429:
        raise DeliveryError("渠道限流，将延后重试", "retry")
    if response.status_code == 407:
        raise DeliveryError("代理认证失败（HTTP 407），请核对用户名和密码；修复后自动重试", "retry")
    if response.status_code >= 500:
        raise DeliveryError("渠道服务异常，结果待核实", "retry" if pre_send else "unknown")
    if not 200 <= response.status_code < 300:
        raise DeliveryError(f"渠道拒绝请求（HTTP {response.status_code}），请检查授权与地址", "blocked" if response.status_code in {401,403} else "failed")
    try:
        return response.json()
    except ValueError:
        raise DeliveryError("渠道返回格式异常，结果待核实", "retry" if pre_send else "unknown") from None


def check_code(data, keys=("errcode", "code", "ret")):
    if not isinstance(data, dict):
        raise DeliveryError("渠道响应格式异常", "unknown")
    for key in keys:
        if key in data and str(data[key]) not in {"0", "None", ""}:
            code = str(data[key])[:32]
            # Do not expose provider error strings: they can contain credentials.
            raise DeliveryError(f"渠道拒绝请求（{key}={code}），请检查权限与频率限制", "blocked" if code in {"-14", "40014", "42001", "11244"} else "failed")


def check_weixin_send(data):
    """A rejected preparation can resume once a different recipient context arrives.

    This is a recovery policy, not a claim that every ret=-2 is an expired
    session. Keep ambiguous responses out of automatic resend, and never log
    arbitrary provider text (it can contain tokens).
    """
    if not isinstance(data, dict):
        raise DeliveryError("微信响应格式异常，结果待核实", "unknown")
    failures = []
    for key in ("errcode", "code", "ret"):
        value = str(data.get(key, 0))
        if value in {"0", "None", ""}:
            continue
        if not re.fullmatch(r"-?\d{1,12}", value):
            raise DeliveryError("微信响应码格式异常，结果待核实", "unknown")
        failures.append((key, value))
    if not failures:
        return
    if data.get("message_id"):
        raise DeliveryError("微信同时返回消息编号与错误码，结果待核实；请核对收件情况", "unknown")
    key, code = failures[0]
    label = f"{key}={code}"
    if any(code in {"-14", "40014", "42001", "11244"} for _, code in failures):
        raise DeliveryError(f"微信授权不可用（{label}），请重新扫码授权并验证收件渠道", "blocked")
    reason = str(data.get("errmsg", "")).strip().lower()
    if failures == [("ret", "-2")] and reason == "prepare failed":
        raise DeliveryError("微信暂无法准备发送（ret=-2，prepare failed）；等待收件人在机器人会话中发一条消息，更新会话后自动续发", "blocked", wait_for_context=True)
    if reason in {"rate limited", "freq limit", "frequency limit", "too many requests"}:
        raise DeliveryError(f"微信限制发送频率（{label}），将在投递截止前自动重试", "retry")
    raise DeliveryError(f"微信未接受消息（{label}），未返回可识别的具体原因；请检查渠道连接状态")


def wx_headers(token=""):
    headers = {"AuthorizationType": "ilink_bot_token", "X-WECHAT-UIN": base64.b64encode(str(secrets.randbits(32)).encode()).decode(), "iLink-App-Id": "bot", "iLink-App-ClientVersion": str((3 << 16) | 1)}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def wx_body(payload):
    return {**payload, "base_info": {"channel_version": "3.0.1", "bot_agent": "PostHub"}}


def send(kind, config, message, delivery_id):
    title, body = message["title"], message["body"]
    text = f"{title}\n\n{body}"
    if message.get("ack_url"):
        text += "\n\n请确认收件：" + message["ack_url"]
    if kind == "email":
        from .email_channel import smtp_send
        if config.get("provider") == "outlook":
            raise DeliveryError("微软邮箱须通过已授权渠道发送", "blocked")
        return smtp_send(config, message, delivery_id)
    if kind == "apprise":
        url = email_url(config) if kind == "email" else config["url"]
        try:
            environment = {k: v for k, v in os.environ.items() if k.lower() not in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}}
            result = subprocess.run([sys.executable, "-m", "posthub.apprise_send"], input=json.dumps({"url": url, "title": title, "body": text}), encoding="utf-8", stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90, env=environment, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except subprocess.TimeoutExpired:
            raise DeliveryError("通知库发送超时，结果待核实", "unknown") from None
        if result.returncode != 0 or result.stdout.strip() != "accepted":
            raise DeliveryError("通知库未确认成功，请核实收件情况和连接配置后处理", "unknown")
        return ""
    with client(config) as http:
        if kind == "webhook":
            headers = {"Idempotency-Key": delivery_id}
            if config.get("bearer_token"):
                headers["Authorization"] = "Bearer " + config["bearer_token"]
            # Custom webhook contract: a 2xx JSON {"accepted": true} receipt.
            result = post_json(http, config["url"], {"delivery_id": delivery_id, "message_id": message["id"], "title": title, "body": body, "ack_url": message.get("ack_url", "")}, headers)
            if not isinstance(result, dict) or result.get("accepted") is not True:
                raise DeliveryError("Webhook 未返回 accepted=true，结果待核实", "unknown")
        elif kind == "wecom":
            if len(text.encode()) > 2048:
                raise DeliveryError("企业微信文本超过 2048 字节；请缩短消息")
            result = post_json(http, config["webhook_url"], {"msgtype": "text", "text": {"content": text}})
            check_code(result)
            if result.get("errcode") != 0:
                raise DeliveryError("企业微信缺少成功回执", "unknown")
        elif kind == "feishu":
            payload = {"msg_type": "text", "content": {"text": text}}
            if config.get("secret"):
                stamp = str(int(time.time()))
                payload.update(timestamp=stamp, sign=base64.b64encode(hmac.new(f"{stamp}\n{config['secret']}".encode(), b"", hashlib.sha256).digest()).decode())
            result = post_json(http, config["webhook_url"], payload)
            check_code(result)
            if result.get("code", result.get("StatusCode")) != 0:
                raise DeliveryError("飞书缺少成功回执", "unknown")
        elif kind == "dingtalk":
            auth = post_json(http, "https://api.dingtalk.com/v1.0/oauth2/accessToken", {"appKey": config["app_key"], "appSecret": config["app_secret"]}, pre_send=True)
            if not auth.get("accessToken"):
                raise DeliveryError("钉钉未签发访问令牌", "blocked")
            group = config.get("target_type") == "group"
            payload = {"robotCode": config["robot_code"], "msgKey": "sampleText", "msgParam": json.dumps({"content": text}, ensure_ascii=False)}
            payload.update({"openConversationId": config["address"]} if group else {"userIds": [config["address"]]})
            result = post_json(http, "https://api.dingtalk.com/v1.0/robot/" + ("groupMessages/send" if group else "oToMessages/batchSend"), payload, {"x-acs-dingtalk-access-token": auth["accessToken"]})
            check_code(result)
            if result.get("invalidStaffIdList") or result.get("flowControlledStaffIdList"):
                raise DeliveryError("钉钉目标无效或被限流")
            if not result.get("processQueryKey"):
                raise DeliveryError("钉钉缺少发送回执", "unknown")
        elif kind == "qq":
            if not config.get("address"):
                raise DeliveryError("QQ 目标尚未绑定", "blocked")
            auth = post_json(http, QQ_API + "/app/getAppAccessToken", {"appId": config["app_id"], "clientSecret": config["app_secret"]}, pre_send=True)
            if not auth.get("access_token"):
                raise DeliveryError("QQ 未签发访问令牌", "blocked")
            scope = "groups" if config.get("target_type") == "group" else "users"
            result = post_json(http, f"{QQ_API}/v2/{scope}/{quote(config['address'], safe='')}/messages", {"content": text, "msg_type": 0}, {"Authorization": "QQBot " + auth["access_token"], "X-Union-Appid": config["app_id"]})
            check_code(result)
            if not result.get("id"):
                raise DeliveryError("QQ 未返回消息编号", "unknown")
        elif kind == "weixin":
            if not config.get("bot_token") or not config.get("context_token") or not config.get("address"):
                raise DeliveryError("微信尚未扫码或缺少会话；请在微信中给机器人发一条消息", "blocked")
            result = post_json(http, ilink_base(config.get("base_url", ILINK)) + "/ilink/bot/sendmessage", wx_body({"msg": {"from_user_id": "", "to_user_id": config["address"], "client_id": delivery_id, "message_type": 2, "message_state": 2, "context_token": config["context_token"], "item_list": [{"type": 1, "text_item": {"text": text}}]}}), wx_headers(config["bot_token"]))
            check_weixin_send(result)
            # Tencent's sendMessage response makes ret optional; an omitted zero
            # is a valid HTTP/JSON success, not an ambiguous delivery.
            # https://github.com/Tencent/openclaw-weixin/blob/main/src/api/api.ts
            return str(result.get("message_id") or "")[:256]
        else:
            raise DeliveryError("未实现的渠道", "blocked")
    return str(result.get("id", result.get("processQueryKey", "")))[:256]
