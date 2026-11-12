import json
import time

import httpx
import pytest

from posthub import adapters
from posthub.bindings import Bindings, qq_key
from posthub.models import TargetInput


MESSAGE = {"id":"message-test", "title":"通知", "body":"中文内容", "ack_url":"https://example.test/ack/token"}


@pytest.mark.parametrize('response,receipt', [({},''),({'ret':0},''),({'ret':'0'},''),({'message_id':18446744073709551615},'18446744073709551615')])
def test_weixin_optional_zero_ret_and_lossless_message_id(monkeypatch,response,receipt):
    monkeypatch.setattr(adapters,'client',lambda *a,**k:httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=response))))
    assert adapters.send('weixin',{'bot_token':'t','context_token':'c','address':'u'},MESSAGE,'d')==receipt


@pytest.mark.parametrize('response', [{'ret':-14},{'ret':1},{'errcode':40014},[],None])
def test_weixin_failure_or_invalid_json_shape_not_accepted(monkeypatch,response):
    monkeypatch.setattr(adapters,'client',lambda *a,**k:httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,content=json.dumps(response)))))
    with pytest.raises(adapters.DeliveryError):
        adapters.send('weixin',{'bot_token':'t','context_token':'c','address':'u'},MESSAGE,'d')


@pytest.mark.parametrize('kind,config,result',[
 ('wecom', {'webhook_url':'https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test'}, {'errcode':0}),
 ('feishu', {'webhook_url':'https://open.feishu.cn/open-apis/bot/v2/hook/test','secret':'test-secret'}, {'code':0}),
 ('webhook', {'url':'https://example.test/hook'}, {'accepted':True}),
 ('weixin', {'bot_token':'test-token','context_token':'context','address':'user'}, {'ret':0}),
])
def test_protocol_success_and_ack_links(monkeypatch, kind, config, result):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200,json=result)
    monkeypatch.setattr(adapters,'client',lambda *a,**k:httpx.Client(transport=httpx.MockTransport(handler)))
    adapters.send(kind,config,MESSAGE,'delivery-123')
    assert len(requests)==1
    payload=json.loads(requests[0].content)
    assert '中文内容' in json.dumps(payload,ensure_ascii=False)
    assert 'https://example.test/ack/token' in json.dumps(payload)
    if kind=='feishu': assert payload['sign'] and payload['timestamp']
    if kind=='webhook': assert requests[0].headers['idempotency-key']=='delivery-123'
    if kind=='weixin': assert payload['msg']['context_token']=='context'


@pytest.mark.parametrize('status,body,expected',[(429,{},'retry'),(503,{},'unknown'),(403,{},'blocked'),(200,{'accepted':False},'unknown'),(200,{},'unknown')])
def test_http_failures_not_success(monkeypatch,status,body,expected):
    monkeypatch.setattr(adapters,'client',lambda *a,**k:httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(status,json=body))))
    with pytest.raises(adapters.DeliveryError) as caught:
        adapters.send('webhook',{'url':'https://example.test'},MESSAGE,'d')
    assert caught.value.kind==expected


@pytest.mark.parametrize('exc,expected',[(httpx.ConnectError,'retry'),(httpx.ReadTimeout,'unknown'),(httpx.WriteError,'unknown')])
def test_connection_vs_ambiguous_failures(monkeypatch,exc,expected):
    def handler(request): raise exc('private token must not appear',request=request)
    monkeypatch.setattr(adapters,'client',lambda *a,**k:httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(adapters.DeliveryError) as caught:
        adapters.send('webhook',{'url':'https://example.test'},MESSAGE,'d')
    assert caught.value.kind==expected
    assert 'private token' not in str(caught.value)


@pytest.mark.parametrize('kind', ['wecom','feishu','weixin'])
def test_provider_error_in_http_200_not_success(monkeypatch,kind):
    configs={'wecom':{'webhook_url':'https://qyapi.weixin.qq.com/test'},'feishu':{'webhook_url':'https://open.feishu.cn/test'},'weixin':{'bot_token':'t','context_token':'c','address':'u'}}
    monkeypatch.setattr(adapters,'client',lambda *a,**k:httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'errcode':40014,'errmsg':'secret-value'}))))
    with pytest.raises(adapters.DeliveryError) as caught:
        adapters.send(kind,configs[kind],MESSAGE,'d')
    assert 'secret-value' not in str(caught.value)


def test_qq_and_dingtalk_auth_then_send(monkeypatch):
    requests=[]
    def handler(request):
        requests.append(request)
        if request.url.path=='/app/getAppAccessToken': return httpx.Response(200,json={'access_token':'qq-token'})
        if request.url.path.endswith('accessToken'): return httpx.Response(200,json={'accessToken':'ding-token'})
        return httpx.Response(200,json={'id':'qq-message','processQueryKey':'ding-message'})
    monkeypatch.setattr(adapters,'client',lambda *a,**k:httpx.Client(transport=httpx.MockTransport(handler)))
    assert adapters.send('qq',{'app_id':'123','app_secret':'s','address':'openid','target_type':'group'},MESSAGE,'d')=='qq-message'
    assert requests[1].url.path=='/v2/groups/openid/messages'
    assert requests[1].headers['Authorization']=='QQBot qq-token'
    adapters.send('dingtalk',{'app_key':'a','app_secret':'s','robot_code':'r','address':'member','target_type':'user'},MESSAGE,'d')
    assert json.loads(requests[-1].content)['userIds']==['member']


def test_weixin_binding_and_context_isolation(hub,monkeypatch):
    target=hub.save_target(TargetInput(name='微信1',kind='weixin'))['id']
    binding=Bindings(hub)
    def handler(request):
        if 'get_bot_qrcode' in request.url.path:return httpx.Response(200,json={'qrcode':'qr-id','qrcode_img_content':'https://example.test/qr'})
        if 'get_qrcode_status' in request.url.path:return httpx.Response(200,json={'status':'confirmed','bot_token':'secret','ilink_bot_id':'bot','ilink_user_id':'owner','baseurl':adapters.ILINK})
        return httpx.Response(200,json={'ret':0,'get_updates_buf':'cursor','msgs':[{'from_user_id':'attacker','context_token':'bad','message_type':1},{'from_user_id':'owner','context_token':'good','message_type':1}]})
    import posthub.bindings as module
    monkeypatch.setattr(module,'client',lambda *a,**k:httpx.Client(transport=httpx.MockTransport(handler)))
    assert binding.qr_start(target)['image'].startswith('data:image/png;base64,')
    assert binding.qr_poll(target)['status']=='confirmed'
    assert binding.wx_sync(target)['has_session']
    assert hub.target(target)['config']['context_token']=='good'
    assert 'secret' not in str(hub.state()['targets'][0]['config'])


@pytest.mark.parametrize('url',['http://ilinkai.weixin.qq.com','https://evil.example','https://ilinkai.weixin.qq.com.evil.example','https://user:pass@ilinkai.weixin.qq.com','https://ilinkai.weixin.qq.com:8443'])
def test_weixin_redirect_cannot_exfiltrate_tokens(url):
    with pytest.raises(ValueError):adapters.ilink_base(url)


def test_qq_challenge_and_expired_signature(hub):
    target=hub.save_target(TargetInput(name='QQ',kind='qq',config={'app_id':'123','app_secret':'secret','target_type':'user'}))['id']
    b=Bindings(hub)
    challenge=b.qq_event('123',{'op':13,'d':{'plain_token':'test','event_ts':'12345'}},b'', '', '')
    qq_key('secret').public_key().verify(bytes.fromhex(challenge['signature']),b'12345test')
    stamp=str(int(hub.clock())-400)
    raw=b'{"op":0,"d":{}}'
    sig=qq_key('secret').sign(stamp.encode()+raw).hex()
    with pytest.raises(ValueError):b.qq_event('123',json.loads(raw),raw,sig,stamp)


@pytest.mark.parametrize('proxy',['http://127.0.0.1:7890','socks5://127.0.0.1:7891','socks5h://127.0.0.1:7891'])
def test_proxy_constructs_real_httpx_client(proxy):
    adapters.validate_config('weixin',{'proxy':proxy})
    with adapters.client({'proxy':proxy}) as client:assert isinstance(client,httpx.Client)


def test_custom_proxy_mode_requires_address():
    with pytest.raises(ValueError):adapters.validate_config('weixin',{'network_mode':'custom'})
