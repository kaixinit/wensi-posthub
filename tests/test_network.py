"""Proxy management, isolation, diagnostics and migration; no external notifications."""
import base64
import json
import socket
import ssl
from http.server import BaseHTTPRequestHandler

import httpx
import pytest
from fastapi.testclient import TestClient

from posthub import adapters
from posthub.core import Hub, HubError
from posthub.models import AppInput, MessageInput, NetworkRouteInput, NetworkTestInput, ProxyInput, TargetInput
from posthub.network import error_info, validate_proxy
from posthub.storage import Store
from posthub.web import create_app
from .conftest import make_target
from .test_api import login
from .test_real_transports import server
from .test_qq_gateway import CONFIG, target as qq_target
from posthub.qq_gateway import QQGateway
from posthub.bindings import Bindings


def proxy(hub, name='Clash', url='http://user:privatepass@127.0.0.1:7890'):
    return hub.network.save_proxy(ProxyInput(name=name,url=url))['id']


def route(hub, tid=None, mode='profile', proxy_id=''):
    n=hub.network.state()
    if tid:
        r=n['routes'][tid]
        return NetworkRouteInput(mode=mode,proxy_id=proxy_id,signature=r['signature'],expected_targets=r['shared_ids'])
    return NetworkRouteInput(mode=mode,proxy_id=proxy_id,previous_proxy_id=n['default_proxy_id'],expected_targets=[id for id,r in n['routes'].items() if r['mode']=='default'])


def test_profile_secret_preservation_and_optimistic_edit(hub,target):
    pid=proxy(hub)
    hub.network.set_route(route(hub,target,proxy_id=pid),target)
    p=hub.network.state()['proxies'][0]
    assert p['target_ids']==[target] and p['has_password']
    assert 'privatepass' not in json.dumps(hub.state())
    with hub.store.connect() as conn:
        assert 'privatepass' not in conn.execute('SELECT config FROM network_proxies').fetchone()[0]
    data=ProxyInput(name='Renamed',host='127.0.0.1',port=7890,username='user',revision=1,expected_targets=[target])
    hub.network.save_proxy(data,pid)
    assert hub.effective_config(hub.target(target))['proxy']=='http://user:privatepass@127.0.0.1:7890'
    with pytest.raises(HubError):hub.network.save_proxy(data,pid)
    data.revision=2;data.password=''
    hub.network.save_proxy(data,pid)
    assert not hub.network.state()['proxies'][0]['has_password']
    assert hub.target(target)['verified']


def test_edit_reference_race_and_default_reference_protect_delete(hub,target):
    pid=proxy(hub)
    stale=ProxyInput(name='Changed',url='http://127.0.0.1:7892',revision=1)
    hub.network.set_route(route(hub,proxy_id=pid))
    with pytest.raises(HubError):hub.network.save_proxy(stale,pid)
    with pytest.raises(HubError):hub.network.delete_proxy(pid)
    hub.network.set_route(route(hub,mode='system'))
    hub.network.set_route(route(hub,target,proxy_id=pid),target)
    hub.set_enabled('targets',target,False)
    with pytest.raises(HubError):hub.network.delete_proxy(pid)
    hub.network.set_route(route(hub,target,mode='system'),target)
    assert hub.network.delete_proxy(pid)['ok']


def test_direct_override_no_proxy_leak_and_pending_deliveries_preserved(hub,target,monkeypatch):
    pid=proxy(hub)
    other=make_target(hub,'Other')
    a=hub.create_app(AppInput(name='A',target_ids=[target]))
    b=hub.create_app(AppInput(name='B',target_ids=[other]))
    msg=hub.submit(MessageInput(title='pending',body='body',target_ids=[target],request_id='pending'))
    before=hub.target(target)
    hub.network.set_route(route(hub,proxy_id=pid))
    hub.network.set_route(route(hub,other,mode='system'),other)
    assert 'proxy' not in hub.effective_config(hub.target(other))
    assert 'privatepass' in hub.effective_config(hub.target(target))['proxy']
    assert hub.target(target)['revision']==before['revision'] and hub.target(target)['verified']
    hub.tick()
    assert hub.message(msg['id'])['deliveries'][0]['state']=='accepted'
    assert next(v for v in hub.state()['apps'] if v['id']==a['id'])['target_ids']==[target]
    assert next(v for v in hub.state()['apps'] if v['id']==b['id'])['target_ids']==[other]
    # A normal target edit restricted to network settings also preserves verification.
    hub.save_target(TargetInput(name=before['name'],kind='webhook',config={'network_mode':'system'}),target)
    assert hub.target(target)['verified'] and hub.target(target)['revision']==before['revision']


def test_qq_route_is_stable_and_shared_by_receive_and_send(hub):
    first=qq_target(hub)
    second=qq_target(hub,network_mode='system')
    pid=proxy(hub)
    stale=route(hub,first,proxy_id=pid)
    hub.network.set_route(stale,first)
    for tid in (first,second):assert hub.effective_config(hub.target(tid))['proxy'].endswith(':7890')
    with pytest.raises(HubError):hub.network.set_route(stale,first)
    hub.set_enabled('targets',first,False)
    qq=QQGateway(hub,Bindings(hub))
    assert qq.desired()[CONFIG['app_id']]['proxy']==hub.effective_config(hub.target(second))['proxy']
    third=qq_target(hub,network_mode='system')
    assert hub.effective_config(hub.target(third))['proxy'].endswith(':7890')
    assert len(hub.network.state()['routes'][second]['shared_ids'])==3


def test_migration_v5_keeps_keys_recipients_and_runs_once(hub,target):
    app=hub.create_app(AppInput(name='Existing',target_ids=[target]))
    qq1=qq_target(hub,network_mode='custom',proxy='http://127.0.0.1:7890')
    qq2=qq_target(hub)
    before=hub.target(target)
    with hub.store.connect(True) as conn:
        conn.execute('DELETE FROM qq_network_routes')
        c=hub.target(qq2,conn)['config'];c['network_mode']='system';c.pop('proxy',None)
        conn.execute('UPDATE targets SET config=? WHERE id=?',(hub.store.encrypt(c),qq2))
        conn.execute('PRAGMA user_version=5')
    hub.store.set_setting('default_proxy',hub.store.encrypt('socks5://u:privatepass@127.0.0.1:7891'))
    upgraded=Hub(Store(hub.store.directory))
    assert upgraded.authenticate_app(app['api_key'])['id']==app['id']
    assert upgraded.target(target)==before
    assert upgraded.effective_config(upgraded.target(target))['proxy'].endswith(':7891')
    # Equal timestamps must retain the old gateway's insertion-order choice.
    assert upgraded.effective_config(upgraded.target(qq1))['proxy']==upgraded.effective_config(upgraded.target(qq2))['proxy']=='http://127.0.0.1:7890'
    initial=upgraded.network.state()
    assert Hub(Store(hub.store.directory)).network.state()==initial
    assert len(initial['proxies'])==2


@pytest.mark.parametrize('url',['http://host','http://host:0','http://host:65536','http://host:bad','http://host:80/path','http://host:80?pass=secret','ftp://host:80','http://host:80#key','http://host:80\n'])
def test_invalid_proxy_rejected_without_secret_echo(url):
    with pytest.raises(ValueError) as error:validate_proxy(url)
    assert url not in str(error.value)


def test_structured_password_preserves_spaces_and_escapes(hub):
    data=ProxyInput(name=' A ',scheme='socks5h',host='::1',port=1080,username='u@',password=' a:b ')
    pid=hub.network.save_proxy(data)['id']
    with hub.store.connect() as conn:
        url=hub.store.decrypt(hub.network.profile(conn,pid)['config'])['url']
    assert url=='socks5h://u%40:%20a%3Ab%20@[::1]:1080'
    assert hub.network.state()['proxies'][0]['name']=='A'


def test_real_proxy_diagnostic_uses_head_without_webhook_secret_or_notification(hub,monkeypatch):
    seen=[]
    class Proxy(BaseHTTPRequestHandler):
        def do_HEAD(self):
            seen.append((self.command,self.path,self.headers.get('Proxy-Authorization')))
            self.send_response(404);self.end_headers()
        def do_POST(self):raise AssertionError('Diagnostics must not send notifications')
        def log_message(self,*args):pass
    with server(Proxy) as port:
        pid=proxy(hub,url=f'http://u:privatepass@127.0.0.1:{port}')
        tid=make_target(hub,config={'url':'http://example.invalid/secret-webhook?token=secret','network_mode':'profile','proxy_id':pid})
        monkeypatch.setenv('HTTP_PROXY','http://127.0.0.1:1')
        result=hub.network.diagnostic(NetworkTestInput(scope='target',id=tid))
        assert result['status']=='reachable' and result['http_status']==404
    assert seen==[('HEAD','http://example.invalid/','Basic '+base64.b64encode(b'u:privatepass').decode())]
    assert not hub.calls and result['notification_sent'] is False
    assert 'privatepass' not in json.dumps(hub.state()) and 'secret-webhook' not in json.dumps(result)


@pytest.mark.parametrize('code,status',[(407,'failed'),(429,'warning'),(502,'warning'),(200,'reachable')])
def test_diagnostic_result_classification_and_preview_does_not_save(hub,monkeypatch,code,status):
    monkeypatch.setattr(adapters,'client',lambda *a,**kw:httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(code))))
    result=hub.network.diagnostic(NetworkTestInput(scope='preview',proxy=ProxyInput(name='draft',url='http://u:privatepass@127.0.0.1:7890')))
    assert result['status']==status
    assert not hub.network.state()['proxies'] and not hub.network.state()['diagnostics']


def test_failed_proxy_never_falls_back_to_direct(hub,monkeypatch):
    seen=[]
    class Receiver(BaseHTTPRequestHandler):
        def do_HEAD(self):seen.append('received');self.send_response(200);self.end_headers()
        def log_message(self,*args):pass
    with server(Receiver) as port, socket.socket() as closed:
        closed.bind(('127.0.0.1',0));badport=closed.getsockname()[1]
        tid=make_target(hub,config={'url':f'http://127.0.0.1:{port}/','network_mode':'custom','proxy':f'http://127.0.0.1:{badport}'})
        result=hub.network.diagnostic(NetworkTestInput(scope='target',id=tid))
    assert result['status']=='failed' and not seen


def test_proxy_failure_and_unknown_receipt_do_not_retry_unsafely():
    for error,kind in [(httpx.ProxyError('privatepass'),'retry'),(httpx.ReadTimeout('privatepass'),'unknown')]:
        with httpx.Client(transport=httpx.MockTransport(lambda req: (_ for _ in ()).throw(error))) as client:
            with pytest.raises(adapters.DeliveryError) as failure:adapters.post_json(client,'https://example.test',{})
        assert failure.value.kind==kind and 'privatepass' not in str(failure.value)
    e=httpx.ConnectError('secret');e.__cause__=ssl.SSLCertVerificationError('privatepass')
    assert error_info(e)[0]=='tls'
    e.__cause__=socket.gaierror();assert error_info(e)[0]=='dns'


def test_smtp_diagnostic_does_not_send_or_login(hub):
    from aiosmtpd.controller import Controller
    received=[]
    class Inbox:
        async def handle_DATA(self, server, session, envelope):
            received.append(envelope.content)
            return '250 OK'
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    controller=Controller(Inbox(),hostname='127.0.0.1',port=port)
    controller.start()
    try:
        tid=hub.save_target(TargetInput(name='SMTP',kind='email',config={'host':'127.0.0.1','port':port,'security':'insecure','sender':'a@example.test','recipient':'b@example.test'}))['id']
        r=hub.network.diagnostic(NetworkTestInput(scope='target',id=tid))
        assert r['status']=='reachable' and not received and not hub.calls
        assert 'SMTP' in r['detail'] and r['notification_sent'] is False
        assert r['route']=='系统网络'
        assert hub.network.state()['routes'][tid]['label']=='系统网络'
    finally:controller.stop()


def test_network_endpoints_require_admin_and_csrf(environment,monkeypatch):
    app=create_app(environment.directory,run_worker=False)
    with TestClient(app) as client:
        body={'name':'Clash','url':'http://u:privatepass@127.0.0.1:7890'}
        endpoint='/api/admin/network/proxies'
        assert client.post(endpoint,json=body).status_code==401
        headers=login(client)
        assert client.post(endpoint,json=body).status_code==403
        response=client.post(endpoint,json=body,headers=headers);assert response.status_code==200
        pid=response.json()['id']
        assert 'privatepass' not in client.get('/api/admin/state').text
        assert client.put('/api/admin/network/default',json={'mode':'profile','proxy_id':pid,'expected_targets':[]},headers=headers).status_code==200
        assert client.delete(endpoint+'/'+pid,headers=headers).status_code==409
        result=client.post(endpoint,json=body|{'url':'http://privatepass@host:bad'},headers=headers)
        assert result.status_code==400 and 'privatepass' not in result.text
        app.state.hub.calls=[]
        monkeypatch.setattr(adapters,'client',lambda *a,**kw:httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(403))))
        result=client.post('/api/admin/network/test',json={'scope':'default'},headers=headers)
        assert result.status_code==200 and result.json()['notification_sent'] is False
