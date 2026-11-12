"""QQ outbound gateway connections; websockets owns TLS, framing and proxies."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from urllib.parse import urlsplit

import httpx
from websockets.sync.client import connect

from . import adapters
from .core import HubError


def gateway_url(value):
    url=urlsplit(str(value))
    if url.scheme!='wss' or not url.hostname or not url.hostname.endswith('.qq.com') or url.username or url.password or url.fragment:
        raise HubError('QQ 返回的连接地址无效',502)
    return str(value)


def credentials(config, *, need_gateway=True):
    """Never return tokens to the browser or include provider response text in errors."""
    with adapters.client(config,timeout=12) as http:
        result=adapters.post_json(http,adapters.QQ_API+'/app/getAppAccessToken',{'appId':config['app_id'],'clientSecret':config['app_secret']},pre_send=True)
        try:
            adapters.check_code(result)
        except adapters.DeliveryError:
            raise adapters.DeliveryError('QQ 凭据校验被拒绝，请核对 AppID、AppSecret 和机器人权限','blocked') from None
        token=result.get('access_token')
        if not isinstance(token,str) or not token:
            raise adapters.DeliveryError('QQ 未通过凭据校验，请检查 AppID 和 AppSecret','blocked')
        if not need_gateway:return token,None
        try:
            response=http.get(adapters.QQ_API+'/gateway',headers={'Authorization':'QQBot '+token})
            response.raise_for_status();gateway=response.json()
        except httpx.HTTPStatusError as error:
            raise adapters.DeliveryError(f'QQ 网关拒绝连接（HTTP {error.response.status_code}），请检查机器人权限','blocked') from None
        except (httpx.HTTPError,ValueError):
            raise adapters.DeliveryError('QQ 网关暂不可达，请检查网络或代理','retry') from None
        try:
            adapters.check_code(gateway)
        except adapters.DeliveryError:
            raise adapters.DeliveryError('QQ 未允许连接网关，请检查机器人事件权限','blocked') from None
        return token,gateway_url(gateway.get('url',''))


def fingerprint(config):
    fields={key:config.get(key) for key in ('app_id','app_secret','proxy')}
    return hashlib.sha256(json.dumps(fields,sort_keys=True).encode()).hexdigest()


class QQSession:
    def __init__(self,config,binding, *, connector=connect,authenticate=credentials):
        self.config=dict(config);self.binding=binding
        self.connector=connector;self.authenticate=authenticate
        self.halt=threading.Event();self.lock=threading.Lock();self.ws=None
        self.session_id=None;self.sequence=None
        self.snapshot={'state':'connecting','detail':'正在校验凭据并连接机器人','updated_at':time.time()}
        self.thread=threading.Thread(target=self.run,daemon=True)

    def status(self):
        with self.lock:return dict(self.snapshot)

    def set_status(self,state,detail,**extra):
        with self.lock:self.snapshot={'state':state,'detail':detail,'updated_at':time.time(),**extra}

    def stop(self):
        self.halt.set()
        with self.lock:ws=self.ws
        if ws:
            try:ws.close()
            except Exception:pass

    def exchange(self,ws,token):
        hello=json.loads(ws.recv(timeout=12))
        if not isinstance(hello,dict) or hello.get('op')!=10:raise ValueError('Expected QQ Hello')
        interval=float(hello.get('d',{}).get('heartbeat_interval',0))/1000
        if not .1<=interval<=300:raise ValueError('Invalid heartbeat interval')
        if self.session_id and self.sequence is not None:
            ws.send(json.dumps({'op':6,'d':{'token':'QQBot '+token,'session_id':self.session_id,'seq':self.sequence}}))
        else:
            ws.send(json.dumps({'op':2,'d':{'token':'QQBot '+token,'intents':1<<25,'shard':[0,1]}}))
        now=time.monotonic();next_beat=now+interval;ack_pending=False;ready=False;login_deadline=now+15
        while not self.halt.is_set():
            now=time.monotonic()
            if not ready and now>=login_deadline:raise TimeoutError('QQ login timeout')
            if now>=next_beat:
                if ack_pending:raise TimeoutError('QQ heartbeat timeout')
                ws.send(json.dumps({'op':1,'d':self.sequence}));ack_pending=True;next_beat=now+interval
            try:payload=json.loads(ws.recv(timeout=min(1,max(.01,next_beat-time.monotonic()))))
            except TimeoutError:continue
            if not isinstance(payload,dict):raise ValueError('Invalid QQ payload')
            if isinstance(payload.get('s'),int):self.sequence=payload['s']
            op=payload.get('op')
            if op==11:ack_pending=False
            elif op==1:
                ws.send(json.dumps({'op':1,'d':self.sequence}));ack_pending=True;next_beat=time.monotonic()+interval
            elif op in (7,9):
                if op==9:self.session_id=None;self.sequence=None
                return
            elif op==0:
                event=payload.get('t')
                if event in ('READY','RESUMED'):
                    data=payload.get('d') or {}
                    if event=='READY':self.session_id=data.get('session_id');ready=bool(self.session_id)
                    else:ready=True
                    if ready:self.set_status('connected','已通过 QQ 鉴权，正在接收会话事件')
                elif ready and event in ('C2C_MESSAGE_CREATE','GROUP_AT_MESSAGE_CREATE'):
                    self.binding.qq_dispatch(self.config['app_id'],payload,self.config['app_secret'])

    def run(self):
        delay=2
        while not self.halt.is_set():
            try:
                self.set_status('connecting','正在校验凭据并连接机器人')
                token,url=self.authenticate(self.config)
                if self.halt.is_set():break
                with self.connector(url,proxy=self.config.get('proxy') or None,open_timeout=12,close_timeout=2,ping_interval=None,max_size=65536) as ws:
                    with self.lock:self.ws=ws
                    if self.halt.is_set():break
                    self.exchange(ws,token)
                if self.halt.is_set():break
                self.set_status('reconnecting','连接已断开，正在自动重连')
            except (adapters.DeliveryError,HubError) as error:
                self.set_status('error',str(error))
            except Exception:
                # Socket exceptions may contain URLs or proxy credentials.
                self.set_status('reconnecting','QQ 连接中断；请检查网络、代理及机器人事件权限，将自动重连')
            finally:
                with self.lock:self.ws=None
            if self.halt.wait(delay):break
            delay=min(delay*2,30)
        self.set_status('stopped','机器人连接已停止')


class QQGateway:
    def __init__(self,hub,binding, *, session_factory=QQSession):
        self.hub=hub;self.binding=binding;self.session_factory=session_factory
        self.lock=threading.Lock();self.sessions={};self.wake=threading.Event()

    def desired(self):
        result={}
        with self.hub.store.connect() as conn:
            ids=[r[0] for r in conn.execute("SELECT id FROM targets WHERE kind='qq' AND enabled=1 AND deleted IS NULL ORDER BY created")]
            for target_id in ids:
                target=self.hub.target(target_id,conn);config=self.hub.effective_config(target)
                if config.get('event_mode','websocket')=='websocket':result.setdefault(config['app_id'],config)
        return result

    def status(self,app_id):
        with self.lock:entry=self.sessions.get(app_id)
        return entry[1].status() if entry else {'state':'pending','detail':'等待连接机器人'}

    def reconnect(self,app_id):
        with self.lock:entry=self.sessions.get(app_id)
        if entry:entry[1].stop()
        self.wake.set()

    def reconcile(self):
        desired=self.desired()
        with self.lock:entries=list(self.sessions.items())
        for app_id,(stamp,session) in entries:
            wanted=desired.get(app_id)
            if wanted is None or stamp!=fingerprint(wanted) or session.halt.is_set():
                session.stop();session.thread.join(timeout=15)
                if session.thread.is_alive():continue
                with self.lock:self.sessions.pop(app_id,None)
        for app_id,config in desired.items():
            with self.lock:
                if app_id in self.sessions:continue
                session=self.session_factory(config,self.binding)
                self.sessions[app_id]=(fingerprint(config),session)
                session.thread.start()

    def run(self,stop):
        try:
            while not stop.is_set():
                try:self.reconcile()
                except Exception:pass # A later reconciliation retries without exposing configuration.
                self.wake.wait(2);self.wake.clear()
        finally:
            with self.lock:entries=list(self.sessions.values())
            for _,session in entries:session.stop()
            for _,session in entries:session.thread.join(timeout=15)
