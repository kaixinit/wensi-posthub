import json
import re

import pytest
from fastapi.testclient import TestClient

from posthub.models import AppInput, MessageInput
from posthub.web import create_app
from .conftest import make_target


@pytest.fixture
def web(environment):
    captured = []
    app = create_app(environment.directory, run_worker=False, sender=lambda *a: captured.append(a) or "local")
    app.state.hub.calls = captured
    with TestClient(app) as client:
        yield client, app.state.hub


def login(client):
    result = client.post('/api/login', json={"password": "test-password-12345"}, headers={"X-PostHub-Request": "1"})
    assert result.status_code == 200
    return {"X-PostHub-Request": "1", "X-CSRF-Token": result.json()["csrf"]}


def test_manual_receipt_requires_admin_confirmation_and_keeps_history(web):
    client, hub = web
    target=make_target(hub)
    message=hub.submit(MessageInput(title='核实收件',body='一次发送',target_ids=[target],request_id='receipt-check'))
    delivery=hub.message(message['id'])['deliveries'][0]['id']
    endpoint=f'/api/admin/deliveries/{delivery}/confirm-received'
    assert client.post(endpoint,json={'confirmed':True},headers={'X-PostHub-Request':'1'}).status_code==401
    headers=login(client)
    assert client.post(endpoint,json={'confirmed':False},headers=headers).status_code==422
    assert client.post(endpoint,json={},headers=headers).status_code==422
    assert client.post(endpoint,json={'confirmed':True},headers=headers).status_code==409
    from posthub.adapters import DeliveryError
    def uncertain(*args):raise DeliveryError('旧版本回执未识别','unknown')
    hub.sender=uncertain;hub.tick()
    before=hub.message(message['id'])['deliveries'][0]
    assert before['state']=='unknown'
    assert client.post(endpoint,json={'confirmed':True},headers=headers).status_code==200
    assert client.post(endpoint,json={'confirmed':True},headers=headers).status_code==200
    after=hub.message(message['id'])['deliveries'][0]
    assert after['state']=='acknowledged' and after['attempts']==before['attempts'] and not after['last_error']
    assert after['history'][:-1]==before['history'] and len(after['history'])==len(before['history'])+1
    assert all(a['resolved'] for a in hub.state()['alerts'] if a['delivery_id']==delivery)
    assert not hub.tick()
    with hub.store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM audit WHERE action='delivery.receipt_confirmed' AND subject=?",(delivery,)).fetchone()[0]==1


def test_auth_and_csrf(web):
    client, _ = web
    assert client.get('/api/admin/state').status_code == 401
    assert client.post('/api/login', json={"password": "test-password-12345"}).status_code == 403
    headers = login(client)
    assert client.get('/api/admin/state').status_code == 200
    assert client.post('/api/admin/targets', json={"name": "x", "kind": "weixin"}).status_code == 403
    assert client.post('/api/logout', headers=headers).status_code == 200
    assert client.get('/api/admin/state').status_code == 401


@pytest.mark.parametrize('code',['12345','1234567','12345678','12345x','１２３４５６','١٢٣٤٥٦',123456])
def test_receipt_api_rejects_anything_except_six_ascii_digits(web,code):
    client,hub=web;headers=login(client)
    target=client.post('/api/admin/targets',json={'name':'验证格式','kind':'webhook','config':{'url':'https://example.test'}},headers=headers).json()['id']
    client.post(f'/api/admin/targets/{target}/verify',headers=headers)
    response=client.post(f'/api/admin/targets/{target}/confirm',json={'code':code},headers=headers)
    assert response.status_code==422
    assert not hub.target(target)['verified']


def test_login_limit_and_validation_does_not_echo_password(web):
    client, _ = web
    headers = {"X-PostHub-Request": "1"}
    response = client.post('/api/login', json={"password": "top-secret", "extra": "top-secret"}, headers=headers)
    assert response.status_code == 422 and 'top-secret' not in response.text
    for _ in range(10):
        assert client.post('/api/login', json={"password": "wrong"}, headers=headers).status_code == 401
    assert client.post('/api/login', json={"password": "wrong"}, headers=headers).status_code == 429


def test_full_api_flow_and_tenant_isolation(web):
    client, hub = web
    headers = login(client)
    target = client.post('/api/admin/targets', json={"name": "QQ 邮箱", "kind": "webhook", "config": {"url": "https://example.test"}}, headers=headers).json()["id"]
    result = client.post(f'/api/admin/targets/{target}/verify', headers=headers)
    assert result.status_code == 202
    hub.tick()
    code = re.search(r'验证码：(\d{6})', hub.calls[-1][2]['body'])[1]
    assert client.post(f'/api/admin/targets/{target}/confirm', json={"code": code}, headers=headers).json()["verified"]
    app = client.post('/api/admin/apps', json={"name": "monitor", "target_ids": [target]}, headers=headers).json()
    bearer = {"Authorization": "Bearer " + app["api_key"]}
    data = {"title": "hello", "body": "test", "target_ids": [target], "request_id": "one"}
    result = client.post('/api/v1/messages', json=data, headers=bearer)
    assert result.status_code == 202
    mid = result.json()["id"]
    assert client.post('/api/v1/messages', json=data, headers=bearer).json()["deduplicated"]
    assert client.post('/api/v1/messages', json=data | {"body": "different"}, headers=bearer).status_code == 409
    hub.tick()
    result = client.get('/api/v1/messages/' + mid, headers=bearer).json()
    assert result["deliveries"][0]["state"] == "accepted"
    second = hub.create_app(AppInput(name="other", target_ids=[target]))
    assert client.get('/api/v1/messages/' + mid, headers={"Authorization": "Bearer " + second["api_key"]}).status_code == 404
    client.post('/api/admin/apps/'+app["id"]+'/enabled', json={"enabled": False}, headers=headers)
    assert client.post('/api/v1/messages', json=data, headers=bearer).status_code == 401


def test_ack_get_has_no_side_effect_and_html_is_escaped(web):
    client, hub = web
    target = make_target(hub)
    hub.store.set_setting("public_base_url", "http://testserver")
    result = hub.submit(MessageInput(title="<script>alert(1)</script>", body="message", request_id="ack", target_ids=[target], require_ack=True))
    hub.tick()
    url = hub.calls[-1][2]["ack_url"]
    response = client.get(url)
    assert '&lt;script&gt;' in response.text and '<script>' not in response.text
    assert hub.message(result['id'])['deliveries'][0]['state'] == 'accepted'
    assert client.post(url).status_code == 200
    assert hub.message(result['id'])['deliveries'][0]['state'] == 'acknowledged'


def test_request_limits_timezones_and_settings_secret(web):
    client, hub = web
    headers = login(client)
    assert client.post('/api/admin/targets', content=b'x'*65537, headers=headers).status_code == 413
    response = client.post('/api/admin/messages', json={"title":"x", "body":"x", "request_id":"x", "scheduled_at":"2026-12-01T10:00:00"}, headers=headers)
    assert response.status_code == 422
    secret_proxy = "http://user:privatepass@127.0.0.1:7890"
    assert client.put('/api/admin/settings', json={"default_proxy": secret_proxy}, headers=headers).status_code == 200
    assert 'privatepass' not in client.get('/api/admin/state').text
    assert 'privatepass' not in str(hub.store.setting('default_proxy'))
    assert client.get('/').status_code == 200
    assert "frame-ancestors 'none'" in client.get('/').headers['content-security-policy']


def test_qq_unsigned_callbacks_rejected(web):
    client, hub = web
    headers = login(client)
    target = client.post('/api/admin/targets', json={"name":"QQ", "kind":"qq", "config":{"app_id":"123456", "app_secret":"test-secret", "target_type":"user"}}, headers=headers).json()['id']
    assert client.post('/api/admin/targets/'+target+'/qq-code', headers=headers).status_code == 410
    payload = {'op':0,'t':'C2C_MESSAGE_CREATE','d':{'content':'文思量化通知','author':{'user_openid':'user_test'}}}
    assert client.post('/api/qq/123456', json=payload).status_code == 401
    assert not hub.target(target)['config'].get('address')
    from posthub.bindings import qq_key
    stamp = str(int(hub.clock()))
    raw = json.dumps(payload).encode()
    sig = qq_key('test-secret').sign(stamp.encode()+raw).hex()
    assert client.post('/api/qq/123456', content=raw, headers={'X-Signature-Ed25519':sig,'X-Signature-Timestamp':stamp}).status_code == 200
    assert not hub.target(target)['config'].get('address')
    conversations = client.get('/api/admin/targets/'+target+'/qq-conversations').json()['conversations']
    assert len(conversations) == 1
    body = {'conversation_id':conversations[0]['id']}
    assert client.post('/api/admin/targets/'+target+'/qq-recipient', json=body).status_code == 403
    assert client.post('/api/admin/targets/'+target+'/qq-recipient', json=body, headers=headers).status_code == 200
    assert hub.target(target)['config']['address'] == 'user_test'
    assert not hub.target(target)['verified']
