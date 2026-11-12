"""QQ protocol tests use isolated stores and a local WebSocket, never a live QQ account."""
from contextlib import contextmanager
import json
import re
import select
import socket
import socketserver
import struct
import threading
import time

from fastapi.testclient import TestClient
import httpx
import pytest
from websockets.sync.client import connect
from websockets.sync.server import serve

from posthub import adapters
from posthub.bindings import Bindings
from posthub.core import HubError
from posthub.models import TargetInput
from posthub.qq_gateway import QQGateway, QQSession, credentials, gateway_url
from posthub.web import create_app
from posthub.storage import Store

CONFIG={'app_id':'123456','app_secret':'test-secret-not-real','target_type':'user','event_mode':'websocket'}


def wait_for(predicate,timeout=5):
    until=time.monotonic()+timeout
    while not predicate():
        assert time.monotonic()<until,'Timed out waiting for local QQ protocol'
        time.sleep(.01)


def target(hub,**changes):
    return hub.save_target(TargetInput(name='QQ test',kind='qq',config=CONFIG|changes))['id']


def event(text='通知接收测试',group=False,address='recipient-one'):
    data={'content':text}
    data.update({'group_openid':address} if group else {'author':{'user_openid':address}})
    return {'op':0,'t':'GROUP_AT_MESSAGE_CREATE' if group else 'C2C_MESSAGE_CREATE','s':2,'d':data}


@pytest.mark.parametrize('value',['ws://api.bot.qq.com','wss://evil.test','wss://api.bot.qq.com.evil.test','wss://user@api.bot.qq.com','wss://api.bot.qq.com/#secret'])
def test_reject_untrusted_gateway(value):
    with pytest.raises(HubError):gateway_url(value)


def test_credentials_and_gateway_use_official_auth_and_redact(monkeypatch):
    requests=[]
    def handler(request):
        requests.append(request)
        return httpx.Response(200,json={'access_token':'private-token'} if request.method=='POST' else {'url':'wss://api.bot.qq.com/websocket/'})
    monkeypatch.setattr(adapters,'client',lambda *a,**k:httpx.Client(transport=httpx.MockTransport(handler)))
    assert credentials(CONFIG)==('private-token','wss://api.bot.qq.com/websocket/')
    assert str(requests[0].url)=='https://api.bot.qq.com/app/getAppAccessToken'
    assert json.loads(requests[0].content)['clientSecret']==CONFIG['app_secret']
    assert requests[1].headers['Authorization']=='QQBot private-token'
    assert credentials(CONFIG,need_gateway=False)==('private-token',None)
    monkeypatch.setattr(adapters,'client',lambda *a,**k:httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'code':'sensitive-provider-echo'}))))
    with pytest.raises(adapters.DeliveryError) as error:credentials(CONFIG)
    assert 'sensitive-provider-echo' not in str(error.value)


def test_connect_validates_before_saving_and_keeps_secret_private(environment,monkeypatch):
    app=create_app(environment.directory,run_worker=False)
    def refused(*a,**k):raise adapters.DeliveryError('QQ 凭据校验被拒绝','blocked')
    monkeypatch.setattr('posthub.web.qq_credentials',refused)
    with TestClient(app) as http:
        login=http.post('/api/login',json={'password':'test-password-12345'},headers={'X-PostHub-Request':'1'})
        data={'name':'Bot','kind':'qq','config':CONFIG}
        assert http.post('/api/admin/qq/connect',json=data).status_code==403
        http.headers.update({'X-PostHub-Request':'1','X-CSRF-Token':login.json()['csrf']})
        assert http.post('/api/admin/qq/connect',json=data).status_code==502
        assert http.get('/api/admin/state').json()['targets']==[]
        monkeypatch.setattr('posthub.web.qq_credentials',lambda *a,**k:('private-token','wss://api.bot.qq.com/websocket/'))
        result=http.post('/api/admin/qq/connect',json=data)
        assert result.status_code==200 and result.json()['credentials_valid']
        target_id=result.json()['id']
        state=http.get('/api/admin/state')
        saved=state.json()['targets'][0]
        assert not saved['verified'] and not saved['bound']
        assert saved['qq_connection']['state']=='pending'
        assert CONFIG['app_secret'] not in state.text and 'private-token' not in result.text
        # Blank secret fields are omitted by the UI; existing encrypted credentials survive edits.
        assert http.post('/api/admin/qq/connect',params={'target_id':target_id},json={'name':'Renamed','kind':'qq','config':{'event_mode':'webhook'}}).status_code==200
        monkeypatch.setattr('posthub.web.qq_credentials',refused)
        assert http.post('/api/admin/qq/connect',params={'target_id':target_id},json={'name':'Bad edit','kind':'qq','config':{'app_secret':'invalid'}}).status_code==502
        assert app.state.hub.target(target_id)['config']['app_secret']==CONFIG['app_secret']
        assert app.state.hub.target(target_id)['name']=='Renamed'


@pytest.mark.parametrize('group',[False,True])
def test_actual_socket_auth_heartbeat_binding_and_resume(hub,group):
    target_id=target(hub,target_type='group' if group else 'user')
    binding=Bindings(hub)
    frames=[];heartbeats=[];server_errors=[];connections=[];finish=threading.Event()
    def handler(ws):
        try:
            index=len(connections);connections.append(index)
            ws.send(json.dumps({'op':10,'d':{'heartbeat_interval':150}}))
            identify=json.loads(ws.recv(timeout=3));frames.append(identify)
            if index==0:
                ws.send(json.dumps({'op':0,'s':1,'t':'READY','d':{'session_id':'session-one'}}))
                ws.send(json.dumps(event('通知接收测试',group)))
                beat=json.loads(ws.recv(timeout=3));heartbeats.append(beat)
                ws.send(json.dumps({'op':11}));ws.send(json.dumps({'op':7}))
            else:
                ws.send(json.dumps({'op':0,'s':3,'t':'RESUMED','d':{}}))
                while not finish.is_set():
                    try:beat=json.loads(ws.recv(timeout=.2))
                    except TimeoutError:continue
                    if beat['op']==1:ws.send(json.dumps({'op':11}))
        except Exception as error:
            if not finish.is_set():server_errors.append(type(error).__name__)
    with serve(handler,'127.0.0.1',0) as server:
        server_thread=threading.Thread(target=server.serve_forever,daemon=True);server_thread.start()
        url=f'ws://127.0.0.1:{server.socket.getsockname()[1]}'
        session=QQSession(CONFIG,binding,authenticate=lambda c:('fixture-token',url))
        session.thread.start()
        try:
            wait_for(lambda:len(frames)>=2,timeout=7)
            wait_for(lambda:session.status()['state']=='connected')
            assert frames[0]['op']==2 and frames[0]['d']['intents']==1<<25
            assert frames[0]['d']['token']=='QQBot fixture-token'
            assert heartbeats==[{'op':1,'d':2}]
            assert frames[1]=={'op':6,'d':{'token':'QQBot fixture-token','session_id':'session-one','seq':2}}
            saved=hub.target(target_id)
            assert not saved['config'].get('address')
            candidates=binding.qq_conversations(target_id)
            assert len(candidates)==1 and candidates[0]['preview']=='通知接收测试'
            binding.qq_select(target_id,candidates[0]['id'])
            saved=hub.target(target_id)
            assert saved['config']['address']=='recipient-one' and not saved['verified']
            assert saved['binding_hash'] is None
            assert not server_errors
        finally:
            finish.set();session.stop();session.thread.join(5);server.shutdown();server_thread.join(5)
        assert not session.thread.is_alive()


def test_recent_conversations_are_scoped_and_expiring(hub):
    tid=target(hub);binding=Bindings(hub)
    for app_id,secret,payload in [('987',CONFIG['app_secret'],event()),(CONFIG['app_id'],'old-secret',event()),(CONFIG['app_id'],CONFIG['app_secret'],event(group=True))]:
        binding.qq_dispatch(app_id,payload,secret)
        assert not hub.target(tid)['config'].get('address')
        assert not binding.qq_conversations(tid)
    binding.qq_dispatch(CONFIG['app_id'],event(),CONFIG['app_secret'])
    candidate=binding.qq_conversations(tid)[0]
    hub.clock.advance(601)
    with pytest.raises(HubError,match='过期'):binding.qq_select(tid,candidate['id'])
    assert not binding.qq_conversations(tid)
    binding.qq_dispatch(CONFIG['app_id'],event(),CONFIG['app_secret'])
    binding.qq_select(tid,binding.qq_conversations(tid)[0]['id'])
    binding.qq_dispatch(CONFIG['app_id'],event(address='other-user'),CONFIG['app_secret'])
    assert hub.target(tid)['config']['address']=='recipient-one'
    hub.save_target(TargetInput(name='Different robot',kind='qq',config={'app_id':'987'}),tid)
    assert not hub.target(tid)['config'].get('address')


def test_qq_test_receipt_does_not_confirm_recipient_until_code_returned(hub):
    tid=target(hub);binding=Bindings(hub)
    binding.qq_dispatch(CONFIG['app_id'],event('文思量化通知'),CONFIG['app_secret'])
    binding.qq_select(tid,binding.qq_conversations(tid)[0]['id'])
    message=hub.verify_start(tid);hub.tick()
    status=hub.qq_test_status(tid)
    assert status['state']=='accepted' and status['code_active']
    assert not hub.target(tid)['verified']
    body=hub.calls[-1][2]['body'];assert 'QQ 收件目标：QQ test' in body
    code=re.search(r'验证码：(\d{6})',body)[1]
    assert code not in hub.message(message['id'])['body']
    hub.verify_finish(tid,code)
    assert hub.target(tid)['verified'] and not hub.qq_test_status(tid)['code_active']
    revision=hub.target(tid)['revision']
    binding.qq_dispatch(CONFIG['app_id'],event('绑定 DEADBEEF',address='another-person'),CONFIG['app_secret'])
    assert hub.target(tid)['verified'] and hub.target(tid)['revision']==revision
    other=next(c for c in binding.qq_conversations(tid) if not c['current'])
    binding.qq_select(tid,other['id'])
    assert not hub.target(tid)['verified'] and hub.qq_test_status(tid) is None


def test_wrong_bot_type_or_rotated_credentials_cannot_select_conversation(hub):
    user=target(hub);group=target(hub,target_type='group');other=target(hub,app_id='999')
    binding=Bindings(hub);binding.qq_dispatch(CONFIG['app_id'],event(),CONFIG['app_secret'])
    candidate=binding.qq_conversations(user)[0]
    for tid in (group,other):
        with pytest.raises(HubError):binding.qq_select(tid,candidate['id'])
    hub.set_enabled('targets',group,False)
    hub.save_target(TargetInput(name='new credentials',kind='qq',config={'app_secret':'rotated'}),user)
    assert not binding.qq_conversations(user)
    with pytest.raises(HubError):binding.qq_select(user,candidate['id'])


def test_conversation_previews_are_encrypted_bounded_and_not_in_binding_code(hub):
    tid=target(hub);binding=Bindings(hub)
    with pytest.raises(HubError) as error:binding.qq_code(tid)
    assert error.value.status==410
    for index in range(55):
        binding.qq_dispatch(CONFIG['app_id'],event('private-recent-conversation',address=f'user-{index}'),CONFIG['app_secret'])
        hub.clock.advance(1)
    with hub.store.connect() as conn:
        rows=conn.execute('SELECT details FROM qq_conversations').fetchall()
        assert len(rows)==50 and all('private-recent-conversation' not in row[0] for row in rows)
    assert len(binding.qq_conversations(tid))==20


def test_upgrade_preserves_existing_confirmed_qq_target(hub):
    tid=target(hub,address='existing-recipient')
    hub.verify_start(tid);hub.tick()
    code=re.search(r'验证码：(\d{6})',hub.calls[-1][2]['body'])[1]
    hub.verify_finish(tid,code)
    before=hub.target(tid)
    with hub.store.connect(True) as conn:
        conn.execute('DROP TABLE qq_conversations')
        conn.execute('PRAGMA user_version=3')
    Store(hub.store.directory)
    assert hub.target(tid)==before
    with hub.store.connect() as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0]==10
        assert conn.execute('SELECT count(*) FROM qq_conversations').fetchone()[0]==0


def test_same_bot_has_independent_receipt_codes_for_each_target(hub,monkeypatch):
    first=target(hub,address='recipient-A');second=target(hub,address='recipient-B')
    # Force a random collision: the second target must receive a fresh active code.
    codes=iter([111111,111111,222222]);monkeypatch.setattr('posthub.core.secrets.randbelow',lambda n:next(codes))
    hub.verify_start(first);hub.verify_start(second)
    while hub.tick():pass
    assert {call[1]['address'] for call in hub.calls}=={'recipient-A','recipient-B'}
    with pytest.raises(HubError,match='不正确'):hub.verify_finish(second,'111111')
    with pytest.raises(HubError,match='不正确'):hub.verify_finish(first,'222222')
    assert not hub.target(first)['verified'] and not hub.target(second)['verified']
    hub.verify_finish(first,'111111');hub.verify_finish(second,'222222')
    assert hub.target(first)['verified'] and hub.target(second)['verified']


@pytest.mark.parametrize('proxy',[None,'http://127.0.0.1:7890','socks5://127.0.0.1:7891'])
def test_proxy_is_explicit_and_never_in_status(hub,proxy):
    used=[]
    @contextmanager
    def connector(url,**kwargs):
        used.append(kwargs);session.halt.set()
        raise OSError('private proxy password and URL')
        yield
    session=QQSession(CONFIG|{'proxy':proxy},Bindings(hub),connector=connector,authenticate=lambda c:('token','wss://api.bot.qq.com/websocket/'))
    session.run()
    assert used[0]['proxy']==proxy
    assert 'private' not in str(session.status())


def test_manager_shares_bot_connection_and_reacts_to_changes(hub):
    made=[]
    class Session:
        def __init__(self,config,binding):
            self.config=config;self.halt=threading.Event();self.thread=threading.Thread(target=self.halt.wait,daemon=True);made.append(self)
        def stop(self):self.halt.set()
        def status(self):return {'state':'connected'}
    manager=QQGateway(hub,Bindings(hub),session_factory=Session)
    first=target(hub);second=target(hub)
    try:
        manager.reconcile();assert len(made)==1
        assert manager.status(CONFIG['app_id'])['state']=='connected'
        hub.set_enabled('targets',first,False);manager.reconcile();assert len(made)==1
        hub.store.set_setting('default_proxy',hub.store.encrypt('socks5://127.0.0.1:7891'))
        manager.reconcile();assert len(made)==2 and made[0].halt.is_set()
        assert made[1].config['proxy'].startswith('socks5:')
        manager.reconnect(CONFIG['app_id']);manager.reconcile();assert len(made)==3
        hub.delete_item('targets',second);manager.reconcile()
        assert not manager.sessions
    finally:
        for session in made:session.stop();session.thread.join(2)


def test_invalid_session_clears_resume_state_and_heartbeat_times_out(hub):
    class Socket:
        def __init__(self,frames):self.frames=iter(frames);self.sent=[]
        def recv(self,timeout):
            try:return json.dumps(next(self.frames))
            except StopIteration:time.sleep(timeout);raise TimeoutError()
        def send(self,data):self.sent.append(json.loads(data))
    session=QQSession(CONFIG,Bindings(hub));session.session_id='old';session.sequence=8
    socket=Socket([{'op':10,'d':{'heartbeat_interval':100}},{'op':9}])
    session.exchange(socket,'token')
    assert session.session_id is None and session.sequence is None
    socket=Socket([{'op':10,'d':{'heartbeat_interval':100}},{'op':0,'s':1,'t':'READY','d':{'session_id':'new'}}])
    with pytest.raises(TimeoutError,match='heartbeat'):session.exchange(socket,'token')
    assert socket.sent[0]['op']==2 and socket.sent[1]['op']==1


@pytest.mark.parametrize('scheme',['http','socks5'])
def test_real_websocket_through_local_proxy(scheme):
    """Exercise websockets + python-socks over actual local TCP tunnels."""
    destinations=[]
    def echo(ws):
        ws.send(ws.recv(timeout=5))
    with serve(echo,'127.0.0.1',0) as upstream:
        upstream_thread=threading.Thread(target=upstream.serve_forever,daemon=True);upstream_thread.start()
        upstream_port=upstream.socket.getsockname()[1]
        class Tunnel(socketserver.StreamRequestHandler):
            def handle(self):
                if scheme=='http':
                    method,authority,_=self.rfile.readline().decode().split()
                    assert method=='CONNECT'
                    host,port=authority.rsplit(':',1)
                    while self.rfile.readline()!=b'\r\n':pass
                    self.wfile.write(b'HTTP/1.1 200 Connection established\r\n\r\n');self.wfile.flush()
                else:
                    assert self.rfile.read(1)==b'\x05'
                    self.rfile.read(self.rfile.read(1)[0]);self.wfile.write(b'\x05\x00');self.wfile.flush()
                    version,command,_,kind=self.rfile.read(4)
                    assert version==5 and command==1
                    host=socket.inet_ntoa(self.rfile.read(4)) if kind==1 else self.rfile.read(self.rfile.read(1)[0]).decode()
                    port=struct.unpack('!H',self.rfile.read(2))[0]
                    self.wfile.write(b'\x05\x00\x00\x01\x7f\x00\x00\x01'+struct.pack('!H',upstream_port));self.wfile.flush()
                assert host=='127.0.0.1' and int(port)==upstream_port
                destinations.append((host,int(port)))
                with socket.create_connection((host,int(port)),timeout=5) as remote:
                    remote.settimeout(None)
                    while True:
                        readable,_,_=select.select([self.connection,remote],[],[],5)
                        if not readable:return
                        for source in readable:
                            data=source.recv(65536)
                            if not data:return
                            (remote if source is self.connection else self.connection).sendall(data)
        with socketserver.ThreadingTCPServer(('127.0.0.1',0),Tunnel) as proxy:
            proxy.daemon_threads=True
            proxy_thread=threading.Thread(target=proxy.serve_forever,daemon=True);proxy_thread.start()
            try:
                with connect(f'ws://127.0.0.1:{upstream_port}',proxy=f'{scheme}://127.0.0.1:{proxy.server_address[1]}',open_timeout=5,close_timeout=2) as client:
                    client.send('local QQ gateway transport test')
                    assert client.recv(timeout=5)=='local QQ gateway transport test'
                assert destinations==[('127.0.0.1',upstream_port)]
            finally:
                proxy.shutdown();proxy_thread.join(5);upstream.shutdown();upstream_thread.join(5)
