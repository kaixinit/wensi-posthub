from contextlib import contextmanager
import json
import re
import smtplib
import socket
import time
from unittest.mock import Mock

import httpx
import pytest
from aiosmtpd.controller import Controller

from posthub.adapters import DeliveryError
from posthub.core import Hub
from posthub.email_channel import EmailService,normalize,provider_for,smtp_send,SCOPES
from posthub.models import MessageInput,TargetInput
from posthub.storage import Store
from posthub.core import HubError


def outlook(hub):
    return hub.save_target(TargetInput(name='微软测试',kind='email',config={'provider':'auto','sender':'person@outlook.com','recipient':'receiver@example.test'}))['id']


@pytest.mark.parametrize('number',[0,7,123456,999999])
def test_six_digit_code_survives_mail_preview_and_leading_zeros(hub,monkeypatch,number):
    tid=hub.save_target(TargetInput(name='验证码预览',kind='email',config={'host':'smtp.test','sender':'from@example.test','recipient':'to@example.test'}))['id']
    monkeypatch.setattr('posthub.core.secrets.randbelow',lambda maximum:number)
    hub.verify_start(tid);hub.tick()
    body=hub.calls[-1][2]['body']
    preview=re.sub(r'\s+','',body)
    code=f'{number:06d}'
    assert re.findall(r'[0-9]+',preview)==[code]
    assert f'验证码：{code}（六位数字）。有效期：十分钟。' in preview
    for invalid in (code+'10',code[:-1],code+'x'):
        with pytest.raises(HubError,match='6位数字'):hub.verify_finish(tid,invalid)
    assert hub.verify_finish(tid,code)['verified']


@pytest.mark.parametrize('address,provider',[('A@QQ.COM','qq'),('a@gmail.com','gmail'),('a@googlemail.com','gmail'),('a@outlook.com','outlook'),('a@hotmail.com','outlook'),('a@live.com','outlook'),('a@msn.com','outlook'),('a@qq.com.evil.test','custom'),('a@company.test','custom')])
def test_exact_sender_domain(address,provider):assert provider_for(address)==provider


def test_presets_are_validated_server_side_and_no_secret_export(hub):
    tid=hub.save_target(TargetInput(name='QQ',kind='email',config={'provider':'qq','sender':'person@qq.com','recipient':'person@gmail.com','password':'private-code','host':'bad.example.test','port':25,'security':'insecure'}))['id']
    c=hub.target(tid)['config'];assert c['host']=='smtp.qq.com' and c['port']==465 and c['security']=='ssl'
    assert c['username']=='person@qq.com' and c['recipient']=='person@gmail.com'
    assert 'private-code' not in json.dumps(hub.state())
    with pytest.raises(ValueError,match='授权码'):normalize({'provider':'gmail','sender':'a@gmail.com','recipient':'b@qq.com'})
    with pytest.raises(ValueError,match='不支持'):hub.save_target(TargetInput(name='bad',kind='email',config={'oauth_cache':'injected'}),tid)
    with pytest.raises(ValueError,match='授权码'):hub.save_target(TargetInput(name='changed',kind='email',config={'provider':'gmail','sender':'person@qq.com'}),tid)


def test_edit_preserves_legacy_verification_and_secret(hub):
    tid=hub.save_target(TargetInput(name='SMTP',kind='email',config={'host':'smtp.test','sender':'a@example.test','recipient':'b@example.test','username':'user','password':'saved-password'}))['id']
    with hub.store.connect(True) as conn:conn.execute('UPDATE targets SET verified=1 WHERE id=?',(tid,))
    before=hub.target(tid)
    hub.save_target(TargetInput(name='renamed',kind='email',config={'provider':'custom','host':'smtp.test','port':587,'security':'starttls','sender':'a@example.test','recipient':'b@example.test','username':'user'}),tid)
    after=hub.target(tid);assert after['verified']==1 and after['revision']==before['revision'] and after['config']['password']=='saved-password'
    hub.save_target(TargetInput(name='renamed',kind='email',config={'recipient':'new@example.test'}),tid)
    assert hub.target(tid)['verified']==0


def test_real_smtp_verification_and_business_delivery_with_retry(environment):
    received=[]
    class Inbox:
        async def handle_DATA(self,server,session,envelope):received.append(envelope.content);return '250 accepted'
    with socket.socket() as reserve:reserve.bind(('127.0.0.1',0));port=reserve.getsockname()[1]
    controller=Controller(Inbox(),hostname='127.0.0.1',port=port);controller.start()
    hub=Hub(environment);now=[time.time()];hub.clock=lambda:now[0]
    try:
        tid=hub.save_target(TargetInput(name='本机测试邮箱',kind='email',config={'provider':'custom','host':'127.0.0.1','port':port,'security':'insecure','sender':'sender@example.test','recipient':'recipient@example.test'}))['id']
        verification=hub.verify_start(tid);hub.tick()
        from email import policy
        from email.parser import BytesParser
        content=BytesParser(policy=policy.default).parsebytes(received[-1]).get_content()
        # QQ mail previews can concatenate lines; the lifetime must never
        # extend the six-digit code into an apparent eight-digit number.
        preview=re.sub(r'\s+','',content)
        assert re.search(r'验证码：[0-9]{6}（六位数字）',preview)
        assert not re.search(r'[0-9]{7,}',preview)
        assert '有效期：十分钟' in preview
        code=re.search(r'验证码：(\d{6})',content)[1];hub.verify_finish(tid,code)
        controller.stop()
        payload=MessageInput(title='业务邮件',body='恢复后自动补发',target_ids=[tid],request_id='mail-recovery')
        mid=hub.submit(payload)['id'];hub.tick()
        assert hub.message(mid)['deliveries'][0]['state']=='retry'
        controller=Controller(Inbox(),hostname='127.0.0.1',port=port);controller.start()
        now[0]+=31;hub.tick();hub.tick()
        assert hub.message(mid)['deliveries'][0]['state']=='accepted' and len(received)==2
        assert hub.submit(payload)['deduplicated']
    finally:controller.stop()


@pytest.mark.parametrize('failure,expected',[(smtplib.SMTPAuthenticationError(535,b'secret'),'blocked'),(smtplib.SMTPDataError(451,b'secret'),'retry'),(smtplib.SMTPDataError(550,b'secret'),'failed'),(smtplib.SMTPServerDisconnected('secret'),'unknown')])
def test_smtp_classification_and_secret_redaction(monkeypatch,failure,expected):
    smtp=Mock();smtp.send_message.side_effect=failure
    monkeypatch.setattr('posthub.email_channel.smtplib.SMTP',lambda *a,**kw:smtp)
    with pytest.raises(DeliveryError) as error:smtp_send({'host':'test','port':25,'security':'insecure','sender':'a@test.org','recipient':'b@test.org'},{'title':'test','body':'body'},'id')
    assert error.value.kind==expected and 'secret' not in str(error.value)


def setup_oauth(hub,monkeypatch):
    class Cache:
        has_state_changed=True
        def serialize(self):return 'private-cache'
    app=Mock();app.initiate_device_flow.return_value={'user_code':'TEST-CODE','device_code':'private-device','interval':5,'expires_in':900}
    app.acquire_token_by_device_flow.return_value={'access_token':'private-access'}
    app.get_accounts.return_value=[{'home_account_id':'test-account'}]
    app.acquire_token_silent_with_error.return_value={'access_token':'private-access'}
    http=Mock();http.get.return_value=httpx.Response(200,json={'id':'id','mail':'person@outlook.com','userPrincipalName':'person@outlook.com'})
    http.post.return_value=httpx.Response(202,headers={'request-id':'provider-receipt'})
    @contextmanager
    def application(config,client_id,serialized=''):yield app,Cache(),http
    monkeypatch.setattr(hub.email,'application',application)
    hub.email.configure_microsoft('11111111-2222-3333-4444-555555555555')
    return app,http


def test_outlook_authorize_verify_send_refresh_and_disconnect(hub,monkeypatch):
    app,http=setup_oauth(hub,monkeypatch);tid=outlook(hub)
    with pytest.raises(ValueError,match='先连接'):hub.verify_start(tid)
    f=hub.email.start(tid);assert 'private-device' not in json.dumps(f)
    assert hub.email.poll(tid,f['id'])['state']=='pending' and not app.acquire_token_by_device_flow.called
    hub.clock.advance(5);assert hub.email.poll(tid,f['id'])['state']=='authorized'
    t=hub.target(tid);assert t['config']['oauth_cache']=='private-cache' and not t['verified']
    assert 'private-' not in json.dumps(hub.state())
    with hub.store.connect() as conn:assert 'private-cache' not in conn.execute('SELECT config FROM targets WHERE id=?',(tid,)).fetchone()[0]
    hub.sender=hub._send
    mid=hub.verify_start(tid)['id'];hub.tick()
    assert hub.message(mid)['deliveries'][0]['state']=='accepted'
    args=http.post.call_args;payload=args.kwargs['json'];assert args.args[0].endswith('/me/sendMail') and payload['message']['toRecipients'][0]['emailAddress']['address']=='receiver@example.test'
    code=re.search(r'验证码：(\d{6})',payload['message']['body']['content'])[1];hub.verify_finish(tid,code)
    assert hub.target(tid)['verified']==1
    hub.email.disconnect(tid);assert not hub.target(tid)['verified'] and not hub.target(tid)['config']['oauth_cache']
    with pytest.raises(ValueError,match='先连接'):hub.verify_start(tid)


@pytest.mark.parametrize('cause',['mismatch','edited','deleted','cancelled','expired'])
def test_oauth_stale_or_wrong_account_cannot_bind(hub,monkeypatch,cause):
    app,http=setup_oauth(hub,monkeypatch);tid=outlook(hub);flow=hub.email.start(tid);hub.clock.advance(6)
    if cause=='mismatch':http.get.return_value=httpx.Response(200,json={'mail':'another@outlook.com'})
    if cause=='edited':hub.save_target(TargetInput(name='edited',kind='email',config={'recipient':'new@example.test'}),tid)
    if cause=='deleted':hub.delete_item('targets',tid)
    if cause=='cancelled':hub.email.cancel(tid,flow['id'])
    if cause=='expired':hub.clock.advance(901)
    if cause=='expired':assert hub.email.poll(tid,flow['id'])['state']=='expired'
    else:
        with pytest.raises(ValueError):hub.email.poll(tid,flow['id'])
    with hub.store.connect() as conn:
        config=hub.store.decrypt(conn.execute('SELECT config FROM targets WHERE id=?',(tid,)).fetchone()[0])
    assert not config.get('oauth_cache')


def test_profile_network_failure_keeps_consumed_device_token(hub,monkeypatch):
    app,http=setup_oauth(hub,monkeypatch);tid=outlook(hub);flow=hub.email.start(tid);hub.clock.advance(6)
    http.get.side_effect=httpx.ConnectError('private-token')
    assert hub.email.poll(tid,flow['id'])['state']=='pending'
    http.get.side_effect=None;hub.clock.advance(6)
    assert hub.email.poll(tid,flow['id'])['state']=='authorized'
    assert app.acquire_token_by_device_flow.call_count==1


@pytest.mark.parametrize('error,state',[(httpx.ReadTimeout('secret'),'unknown'),(httpx.ConnectError('secret'),'retry'),(429,'retry'),(503,'unknown'),(401,'blocked')])
def test_graph_receipt_and_network_failure_classification(hub,monkeypatch,error,state):
    app,http=setup_oauth(hub,monkeypatch);tid=outlook(hub);flow=hub.email.start(tid);hub.clock.advance(6);hub.email.poll(tid,flow['id'])
    if isinstance(error,int):http.post.return_value=httpx.Response(error)
    else:http.post.side_effect=error
    with pytest.raises(DeliveryError) as failure:hub.email.send(hub.effective_config(hub.target(tid)),{'title':'t','body':'b'},'id')
    assert failure.value.kind==state and 'secret' not in str(failure.value)


def test_expired_authorization_cannot_send(hub,monkeypatch):
    app,http=setup_oauth(hub,monkeypatch);tid=outlook(hub);flow=hub.email.start(tid);hub.clock.advance(6);hub.email.poll(tid,flow['id'])
    app.acquire_token_silent_with_error.return_value={'error':'invalid_grant','error_description':'private'}
    with pytest.raises(DeliveryError) as failure:hub.email.send(hub.effective_config(hub.target(tid)),{'title':'t','body':'b'},'id')
    assert failure.value.kind=='blocked' and not http.post.called
    assert not next(t for t in hub.state()['targets'] if t['id']==tid)['email_authorized']
