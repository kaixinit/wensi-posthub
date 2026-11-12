import base64
import json
import time
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

from posthub.core import Hub
from posthub.email_channel import SCOPES
from posthub.models import TargetInput
from posthub.web import create_app
from .test_api import login

CLIENT='11111111-2222-3333-4444-555555555555'
TENANT='aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'


def encoded(data):return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip('=')


def test_real_msal_device_flow_cache_reload_refresh_and_graph(environment,monkeypatch):
    """Run the real MSAL SDK against controlled HTTP responses, no external mail."""
    requests=[];state={'token_calls':0,'graph_calls':0};real_client=httpx.Client
    now=int(time.time())
    def handle(request):
        requests.append((request.method,request.url.path))
        if request.url.path.endswith('openid-configuration'):
            return httpx.Response(200,json={'issuer':f'https://login.microsoftonline.com/{TENANT}/v2.0','authorization_endpoint':f'https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize','token_endpoint':f'https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token','device_authorization_endpoint':f'https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/devicecode','jwks_uri':'https://login.microsoftonline.com/keys'})
        if request.url.path.endswith('devicecode'):
            return httpx.Response(200,json={'device_code':'secret-device-code','user_code':'DISPLAY-CODE','verification_uri':'https://microsoft.com/devicelogin','expires_in':900,'interval':5})
        if request.url.path.endswith('/token'):
            state['token_calls']+=1;data=parse_qs(request.content.decode())
            if data['grant_type']==['refresh_token']:
                assert data['refresh_token']==['secret-refresh'];return httpx.Response(200,json={'token_type':'Bearer','expires_in':3600,'access_token':'secret-access-refreshed','refresh_token':'secret-refresh','scope':' '.join(SCOPES)})
            claims={'aud':CLIENT,'iss':f'https://login.microsoftonline.com/{TENANT}/v2.0','sub':'subject','oid':'uid','tid':TENANT,'preferred_username':'person@outlook.com','iat':now,'exp':now+3600}
            token=encoded({'alg':'none'})+'.'+encoded(claims)+'.signature'
            return httpx.Response(200,json={'token_type':'Bearer','expires_in':3600,'access_token':'secret-access','refresh_token':'secret-refresh','id_token':token,'client_info':encoded({'uid':'uid','utid':TENANT}),'scope':' '.join(SCOPES)})
        if request.url.path=='/v1.0/me':return httpx.Response(200,json={'mail':'person@outlook.com','userPrincipalName':'person@outlook.com','id':'uid'})
        if request.url.path=='/v1.0/me/sendMail':
            state['graph_calls']+=1;assert request.headers['authorization']=='Bearer secret-access-refreshed'
            return httpx.Response(202,headers={'request-id':'actual-sdk-mock-receipt'})
        raise AssertionError((request.method,str(request.url)))
    monkeypatch.setattr('posthub.email_channel.httpx.Client',lambda **kw:real_client(transport=httpx.MockTransport(handle),**kw))
    hub=Hub(environment);hub.email.configure_microsoft(CLIENT)
    tid=hub.save_target(TargetInput(name='MSAL 合约',kind='email',config={'provider':'outlook','sender':'person@outlook.com','recipient':'to@example.test'}))['id']
    flow=hub.email.start(tid)
    hub.email.flows[tid]['next_at']=0
    assert hub.email.poll(tid,flow['id'])['state']=='authorized'
    target=hub.target(tid);cache=json.loads(target['config']['oauth_cache'])
    assert cache['RefreshToken'] and cache['Account']
    for access in cache['AccessToken'].values():access['expires_on']='1'
    hub.email.save_tokens(target,{'oauth_cache':json.dumps(cache)})
    restarted=Hub(environment)
    receipt=restarted.email.send(restarted.effective_config(restarted.target(tid)),{'title':'test','body':'test'},'delivery-1')
    assert receipt=='actual-sdk-mock-receipt' and state=={'token_calls':2,'graph_calls':1}
    assert 'secret-access-refreshed' in restarted.target(tid)['config']['oauth_cache']
    assert 'secret-' not in json.dumps(restarted.state())


def test_email_endpoints_require_login_and_csrf(environment):
    app=create_app(environment.directory,run_worker=False)
    with TestClient(app) as client:
        assert client.put('/api/admin/email/microsoft',json={'client_id':CLIENT}).status_code==401
        headers=login(client)
        assert client.put('/api/admin/email/microsoft',json={'client_id':CLIENT}).status_code==403
        assert client.put('/api/admin/email/microsoft',headers=headers,json={'client_id':'x'*36}).status_code==400
        assert client.put('/api/admin/email/microsoft',headers=headers,json={'client_id':CLIENT}).status_code==200
        r=client.post('/api/admin/targets',headers=headers,json={'name':'email','kind':'email','config':{'provider':'auto','sender':'person@outlook.com','recipient':'to@example.test'}})
        assert r.status_code==201;tid=r.json()['id']
        assert client.post('/api/admin/targets/'+tid+'/verify',headers=headers).status_code==400
        assert client.get('/api/admin/targets/'+tid+'/verification-status').json() is None
        assert client.post('/api/admin/targets/'+tid+'/email/poll',json={'flow_id':'a'*32}).status_code==403
        assert client.post('/api/admin/targets/'+tid+'/email/cancel',headers=headers,json={'flow_id':'a'*32}).status_code==200
        assert client.post('/api/admin/targets/'+tid+'/email/disconnect',headers=headers).status_code==200
