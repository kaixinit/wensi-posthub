from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import re

import pytest

from posthub.adapters import DeliveryError
from posthub.core import Hub, HubError
from posthub.models import AppInput, MessageInput, ProfileInput, TargetInput
from .conftest import make_target


def msg(target, **kwargs):
    return MessageInput(**({"title": "巡检通知", "body": "服务器状态变更", "target_ids": [target], "request_id": "business-1"} | kwargs))


def iso(clock, plus):
    return datetime.fromtimestamp(clock() + plus, timezone.utc)


def delivery(hub, mid):
    return hub.message(mid)["deliveries"][0]


def test_durable_submission_and_restart(hub, target):
    result = hub.submit(msg(target))
    assert not hub.calls
    restarted = Hub(hub.store, sender=hub.sender, clock=hub.clock)
    restarted.recover()
    restarted.tick()
    assert len(hub.calls) == 1
    item = delivery(hub, result["id"])
    assert item["state"] == "accepted" and not item["acked_at"]


def test_scheduled_survives_restart(hub, target):
    result = hub.submit(msg(target, scheduled_at=iso(hub.clock, 120)))
    restarted = Hub(hub.store, sender=hub.sender, clock=hub.clock)
    restarted.tick()
    hub.clock.advance(119)
    restarted.tick()
    assert not hub.calls
    hub.clock.advance(1)
    restarted.tick()
    assert delivery(hub, result["id"])["state"] == "accepted"


def test_idempotent_and_conflict(hub, target):
    first = hub.submit(msg(target))
    again = hub.submit(msg(target))
    assert first["id"] == again["id"] and again["deduplicated"]
    with pytest.raises(HubError, match="request_id"):
        hub.submit(msg(target, body="different"))
    hub.tick()
    hub.tick()
    assert len(hub.calls) == 1


def test_duplicates_and_concurrent_claim(hub, target):
    hub.submit(msg(target, target_ids=[target, target]))
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda _: hub.tick(), range(8)))
    assert len(hub.calls) == 1


def test_app_scope_and_revocation(hub, target):
    other = make_target(hub, "other")
    a = hub.create_app(AppInput(name="app A", target_ids=[target]))
    b = hub.create_app(AppInput(name="app B", target_ids=[target]))
    app_a, app_b = hub.authenticate_app(a["api_key"]), hub.authenticate_app(b["api_key"])
    first = hub.submit(msg(target), app=app_a)
    second = hub.submit(msg(target), app=app_b)
    assert first["id"] != second["id"]
    with pytest.raises(HubError):
        hub.submit(msg(other, request_id="forbidden"), app=app_a)
    with pytest.raises(HubError):
        hub.message(first["id"], app_b["id"])
    hub.set_enabled("apps", app_a["id"], False)
    assert hub.authenticate_app(a["api_key"]) is None
    assert a["api_key"] not in str(hub.state())


def test_profile_permission_is_rechecked(hub, target):
    other = make_target(hub, "other")
    app = hub.create_app(AppInput(name="restricted", target_ids=[target]))
    profile = hub.save_profile(ProfileInput(name="route", target_ids=[other]))
    with pytest.raises(HubError):
        hub.submit(msg(target, target_ids=[], profile_id=profile["id"]), app=hub.authenticate_app(app["api_key"]))


def test_partial_retry_never_resends_success(hub, target):
    other = make_target(hub, "other", {"url": "https://example.test/other"})
    original, failed = hub.sender, [True]
    def sender(kind, config, message, did):
        if config["url"].endswith("other") and failed[0]:
            raise DeliveryError("limited", "retry")
        return original(kind, config, message, did)
    hub.sender = sender
    result = hub.submit(msg(target, target_ids=[target, other]))
    hub.tick(); hub.tick()
    assert len(hub.calls) == 1
    failed[0] = False
    hub.clock.advance(31)
    hub.tick()
    assert len(hub.calls) == 2
    assert {d["state"] for d in hub.message(result["id"])["deliveries"]} == {"accepted"}


def test_unlimited_safe_retries_and_alert(hub, target):
    attempts = []
    def fail(*args):
        attempts.append(1)
        raise DeliveryError("connect failed", "retry")
    hub.sender = fail
    result = hub.submit(msg(target))
    for _ in range(6):
        hub.tick(); hub.clock.advance(300)
    assert len(attempts) == 6
    assert delivery(hub, result["id"])["state"] == "retry"
    assert hub.state()["alerts"]


def test_unknown_needs_explicit_risk_acknowledgment(hub, target):
    def fail(*args):
        raise DeliveryError("response lost", "unknown")
    hub.sender = fail
    result = hub.submit(msg(target))
    hub.tick()
    item = delivery(hub, result["id"])
    with pytest.raises(HubError, match="可能重复"):
        hub.retry(item["id"])
    hub.clock.advance(300)
    hub.tick()
    assert delivery(hub, result["id"])["attempts"] == 1
    hub.retry(item["id"], True)
    assert delivery(hub, result["id"])["state"] == "queued"


def test_crash_recovery_does_not_auto_resend(hub, target):
    result = hub.submit(msg(target))
    with hub.store.connect(True) as db:
        db.execute("UPDATE deliveries SET state='sending' WHERE message_id=?", (result["id"],))
    hub.recover()
    hub.tick()
    assert not hub.calls
    assert delivery(hub, result["id"])["state"] == "unknown"


def test_expired_task_and_cancellation(hub, target):
    result = hub.submit(msg(target, scheduled_at=iso(hub.clock, 30), expires_at=iso(hub.clock, 60)))
    hub.clock.advance(61)
    hub.tick()
    assert not hub.calls
    assert delivery(hub, result["id"])["state"] == "expired"
    later = hub.submit(msg(target, request_id="cancel"))
    assert hub.cancel(later["id"])["cancelled"] == 1
    hub.tick()
    assert not hub.calls


def test_configuration_change_blocks_scheduled_send(hub, target):
    result = hub.submit(msg(target))
    hub.save_target(TargetInput(name="new", kind="webhook", config={"url": "https://example.test/new"}), target)
    hub.tick()
    assert delivery(hub, result["id"])["state"] == "blocked"
    assert not hub.calls


def test_confirmation_explicit_idempotent_and_overdue_alert(hub, target):
    hub.store.set_setting("public_base_url", "https://posthub.example.test")
    result = hub.submit(msg(target, require_ack=True, ack_minutes=1))
    hub.tick()
    token = hub.calls[0][2]["ack_url"].rsplit("/", 1)[1]
    hub.acknowledge(token, mutate=False)
    assert delivery(hub, result["id"])["state"] == "accepted"
    hub.clock.advance(61)
    hub.tick()
    assert any(a["reason"] == "ack_overdue" for a in hub.state()["alerts"])
    assert len(hub.calls) == 1
    hub.acknowledge(token)
    hub.acknowledge(token)
    assert delivery(hub, result["id"])["state"] == "acknowledged"
    assert all(a["resolved"] for a in hub.state()["alerts"])


def test_invalid_ack_and_confirmation_race(hub, target):
    hub.store.set_setting("public_base_url", "https://posthub.example.test")
    with pytest.raises(HubError):
        hub.acknowledge("fake")
    def immediate(kind, config, message, did):
        hub.acknowledge(message["ack_url"].rsplit("/", 1)[1])
        return "accepted"
    hub.sender = immediate
    result = hub.submit(msg(target, require_ack=True))
    hub.tick()
    assert delivery(hub, result["id"])["state"] == "acknowledged"


def test_unverified_targets_and_secret_redaction(hub):
    target = hub.save_target(TargetInput(name="sensitive", kind="webhook", config={"url": "https://example.test/secret-token", "bearer_token": "my-private-token"}))["id"]
    with pytest.raises(HubError):
        hub.submit(msg(target))
    assert "my-private-token" not in str(hub.state())
    assert b"my-private-token" not in hub.store.path.read_bytes()
    result = hub.verify_start(target)
    assert "验证码已发送" in hub.message(result["id"])["body"]
    hub.tick()
    code = re.search(r"验证码：(\d{6})", hub.calls[-1][2]["body"])[1]
    assert code not in hub.message(result["id"])["body"]
    hub.verify_finish(target, code)
    with pytest.raises(HubError):
        hub.verify_finish(target, code)


def test_verification_expiry_and_attempt_cap(hub):
    target = hub.save_target(TargetInput(name="pending", kind="webhook", config={"url": "https://example.test"}))["id"]
    hub.verify_start(target); hub.tick()
    code = re.search(r"验证码：(\d{6})", hub.calls[-1][2]["body"])[1]
    incorrect = f"{(int(code) + 1) % 1_000_000:06d}"
    for _ in range(5):
        with pytest.raises(HubError):
            hub.verify_finish(target, incorrect)
    with pytest.raises(HubError, match="失效"):
        hub.verify_finish(target, code)


def test_default_proxy_override_and_smtp_system_route(hub, target):
    hub.store.set_setting("default_proxy", hub.store.encrypt("socks5://127.0.0.1:7891"))
    assert hub.effective_config(hub.target(target))["proxy"].startswith("socks5:")
    hub.save_target(TargetInput(name="system", kind="webhook", config={"network_mode": "system"}), target)
    assert not hub.effective_config(hub.target(target)).get("proxy")
    assert "proxy" not in hub.effective_config({"kind": "email", "config": {}})


def test_alert_route_does_not_recurse(hub, target):
    alerts = make_target(hub, "告警渠道")
    hub.store.set_setting("alert_target_id", alerts)
    def fail(*args):
        raise DeliveryError("denied")
    hub.sender = fail
    hub.submit(msg(target)); hub.tick(); hub.notify_alerts(); hub.tick(); hub.notify_alerts()
    with hub.store.connect() as db:
        assert db.execute("SELECT count(*) FROM messages WHERE purpose='alert'").fetchone()[0] == 1


def test_switch_from_custom_proxy_to_default_does_not_reuse_old_proxy(hub, target):
    hub.store.set_setting("default_proxy", hub.store.encrypt("http://127.0.0.1:7890"))
    hub.save_target(TargetInput(name="custom",kind="webhook",config={"network_mode":"custom","proxy":"socks5://127.0.0.1:7891"}),target)
    assert hub.effective_config(hub.target(target))["proxy"]=="socks5://127.0.0.1:7891"
    hub.save_target(TargetInput(name="default",kind="webhook",config={"network_mode":"default"}),target)
    assert hub.effective_config(hub.target(target))["proxy"]=="http://127.0.0.1:7890"
    hub.store.set_setting("default_proxy", "")
    assert "proxy" not in hub.effective_config(hub.target(target))


def test_stalled_send_becomes_unknown_without_resending(hub, target):
    result=hub.submit(msg(target))
    with hub.store.connect(True) as conn:
        conn.execute("UPDATE deliveries SET state='sending',started=? WHERE message_id=?", (hub.clock()-181,result['id']))
    hub.tick()
    assert delivery(hub,result['id'])['state']=='unknown'
    assert not hub.calls


def test_qq_shared_credentials_cannot_silently_diverge(hub):
    first=hub.save_target(TargetInput(name='QQ1',kind='qq',config={'app_id':'123','app_secret':'old'}))['id']
    second=hub.save_target(TargetInput(name='QQ2',kind='qq',config={'app_id':'123','app_secret':'old'}))['id']
    with pytest.raises(HubError):
        hub.save_target(TargetInput(name='QQ1',kind='qq',config={'app_secret':'new'}),first)
    hub.set_enabled('targets',second,False)
    hub.save_target(TargetInput(name='QQ1',kind='qq',config={'app_secret':'new'}),first)
    with pytest.raises(HubError):hub.set_enabled('targets',second,True)
    hub.save_target(TargetInput(name='QQ2',kind='qq',config={'app_secret':'new'}),second)
    hub.set_enabled('targets',second,True)
