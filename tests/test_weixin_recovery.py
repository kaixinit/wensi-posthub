from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json

import httpx
import pytest

from posthub import adapters
from posthub.bindings import Bindings
from posthub.core import Hub, HubError
from posthub.models import AppInput, MessageInput, TargetInput
from posthub.storage import Store


@pytest.mark.parametrize('response,kind,waiting', [
    ({'ret':-2,'errmsg':'prepare failed'},'blocked',True),
    ({'ret':'-2','errmsg':'prepare failed'},'blocked',True),
    ({'ret':-2,'errmsg':'private-secret'},'failed',False),
    ({'ret':-2},'failed',False),
    ({'ret':-2,'errmsg':'prepare failed','message_id':'123'},'unknown',False),
    ({'ret':-14,'errmsg':'private-secret'},'blocked',False),
    ({'ret':-2,'errcode':40014,'errmsg':'prepare failed'},'blocked',False),
    ({'ret':-2,'errmsg':'rate limited'},'retry',False),
    ({'ret':'private-secret'},'unknown',False),
])
def test_weixin_specific_errors_are_safe(monkeypatch,response,kind,waiting):
    monkeypatch.setattr(adapters,'client',lambda *a,**k:httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json=response))))
    with pytest.raises(adapters.DeliveryError) as caught:
        adapters.send('weixin',{'bot_token':'t','context_token':'old-private-context','address':'owner'}, {'title':'test','body':'test'}, 'stable-client-id')
    assert caught.value.kind==kind and caught.value.wait_for_context is waiting
    assert 'private-secret' not in str(caught.value)


def setup_delivery(hub, *, siblings=(), verification=False, app=False):
    tid=hub.save_target(TargetInput(name='会话恢复',kind='weixin'))['id']
    Bindings(hub)._save(hub.target(tid), {'bot_token':'test-token','context_token':'old-private-context','address':'owner'})
    if not verification:
        with hub.store.connect(True) as conn:conn.execute('UPDATE targets SET verified=1 WHERE id=?',(tid,))
    calls=[]
    original=hub.sender
    def send(kind,config,message,did):
        calls.append((kind,config.get('context_token'),did))
        if kind=='weixin' and config['context_token']=='old-private-context':
            adapters.check_weixin_send({'ret':-2,'errmsg':'prepare failed'})
        return original(kind,config,message,did)
    hub.sender=send
    if verification:
        mid=hub.verify_start(tid)['id']
    else:
        application=None
        if app:
            application=hub.authenticate_app(hub.create_app(AppInput(name='isolated-upstream',target_ids=[tid,*siblings]))['api_key'])
        mid=hub.submit(MessageInput(title='14:40 fixture',body='same payload',target_ids=[tid,*siblings],request_id='recovery',expires_at=datetime.fromtimestamp(hub.clock()+120,timezone.utc)),app=application)['id']
    for _ in range(1+len(siblings)):hub.tick()
    return tid,mid,calls


def wx_delivery(hub,mid):
    return next(d for d in hub.message(mid)['deliveries'] if d['kind']=='weixin')


def fresh_context(hub,tid,context='fresh-private-context'):
    Bindings(hub)._save(hub.target(tid),{'context_token':context})


def test_new_context_resumes_once_after_restart_and_preserves_siblings(hub,target,monkeypatch):
    tid,mid,calls=setup_delivery(hub,siblings=[target],app=True)
    first=wx_delivery(hub,mid)
    assert first['state']=='blocked' and first['waiting_for_context'] and first['attempts']==1
    assert hub.state()['stats']['waiting_context']==1
    assert next(m for m in hub.state()['messages'] if m['id']==mid)['states']['waiting_context']==1
    with pytest.raises(HubError,match='自动续发'):hub.retry(first['id'])
    hub.clock.advance(30)
    for _ in range(5):hub.tick()
    assert len(calls)==2
    restarted=Hub(Store(hub.store.directory),sender=hub.sender,clock=hub.clock)
    restarted.recover()
    # Exercise real getupdates parsing; another person's conversation cannot resume it.
    incoming=[{'from_user_id':'other-person','message_type':1,'context_token':'not-owner'}]
    monkeypatch.setattr('posthub.bindings.client',lambda *a,**k:httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'msgs':incoming}))))
    Bindings(restarted).wx_sync(tid);restarted.tick()
    assert len(calls)==2
    incoming[0]={'from_user_id':'owner','message_type':1,'context_token':'fresh-private-context'}
    Bindings(restarted).wx_sync(tid)
    with ThreadPoolExecutor(8) as pool:list(pool.map(lambda _:restarted.tick(),range(8)))
    final=wx_delivery(restarted,mid)
    assert final['state']=='accepted' and not final['waiting_for_context'] and final['attempts']==2
    assert len(calls)==3 and sum(c[0]=='webhook' for c in calls)==1
    assert calls[0][2]==calls[-1][2]  # Stable client_id and the same delivery.
    assert not [a for a in hub.state()['alerts'] if not a['resolved']]
    assert 'private-context' not in json.dumps(restarted.message(mid))
    fresh_context(restarted,tid,'another-private-context');restarted.tick()
    assert len(calls)==3


@pytest.mark.parametrize('action', ['expire','cancel','delete_target','delete_app','rebind','disabled','unverified'])
def test_context_recovery_respects_lifecycle(hub,action):
    tid,mid,calls=setup_delivery(hub,app=True)
    appid=hub.message(mid)['app_id']
    if action=='expire':hub.clock.advance(120)
    elif action=='cancel':assert hub.cancel(mid)['cancelled']==1
    elif action.startswith('delete_'):
        table,item=('targets',tid) if action=='delete_target' else ('apps',appid)
        assert hub.deletion_info(table,item)['pending']==1
        hub.set_enabled(table,item,False)
        assert hub.delete_item(table,item)['cancelled']==1
    elif action=='rebind':Bindings(hub)._save(hub.target(tid),{'context_token':'new-account-context','address':'someone-else'},reauthorize=True)
    elif action=='disabled':hub.set_enabled('targets',tid,False)
    elif action=='unverified':
        with hub.store.connect(True) as conn:conn.execute('UPDATE targets SET verified=0 WHERE id=?',(tid,))
    if action not in {'delete_target','rebind','disabled'}:fresh_context(hub,tid)
    hub.tick();hub.tick()
    assert len(calls)==1
    final=wx_delivery(hub,mid)
    expected='expired' if action=='expire' else 'cancelled' if action in {'cancel','delete_target','delete_app'} else 'blocked'
    assert final['state']==expected
    if action=='disabled':
        hub.set_enabled('targets',tid,True);fresh_context(hub,tid);hub.tick()
        assert wx_delivery(hub,mid)['state']=='accepted' and len(calls)==2
    elif action=='unverified':
        hub.clock.advance(120);hub.tick()
        assert wx_delivery(hub,mid)['state']=='expired'
    else:assert not final['waiting_for_context']


def test_fresh_context_arriving_during_rejected_request_is_not_lost(hub):
    tid,mid,calls=setup_delivery(hub)
    fresh_context(hub,tid)
    original=hub.sender
    def send(kind,config,message,did):
        fresh_context(hub,tid,'second-fresh-context')
        adapters.check_weixin_send({'ret':-2,'errmsg':'prepare failed'})
    hub.sender=send;hub.tick()
    assert wx_delivery(hub,mid)['waiting_for_context']
    assert any(not a['resolved'] and a['reason']=='blocked' for a in hub.state()['alerts'])
    hub.sender=original;hub.tick()
    assert wx_delivery(hub,mid)['state']=='accepted'
    assert wx_delivery(hub,mid)['attempts']==3


def test_same_context_cannot_loop_and_old_failed_records_stay_unchanged(hub):
    tid,mid,calls=setup_delivery(hub)
    fresh_context(hub,tid,'old-private-context')
    for _ in range(10):hub.tick()
    assert len(calls)==1
    with hub.store.connect(True) as conn:
        conn.execute('DELETE FROM weixin_context_waits')
        conn.execute("UPDATE deliveries SET state='failed' WHERE message_id=?",(mid,))
    fresh_context(hub,tid);Store(hub.store.directory);hub.tick()
    assert len(calls)==1 and wx_delivery(hub,mid)['state']=='failed'


def test_verification_wait_expires_without_sending_stale_code(hub):
    tid,mid,calls=setup_delivery(hub,verification=True)
    with hub.store.connect(True) as conn:conn.execute('UPDATE targets SET verify_expires=? WHERE id=?',(hub.clock(),tid))
    fresh_context(hub,tid);hub.tick()
    assert len(calls)==1 and wx_delivery(hub,mid)['state']=='expired'


def test_replaced_verification_code_is_never_auto_sent(hub):
    tid,mid,calls=setup_delivery(hub,verification=True)
    newer=hub.verify_start(tid)['id']
    fresh_context(hub,tid)
    hub.tick();hub.tick()
    assert wx_delivery(hub,mid)['state']=='expired'
    assert wx_delivery(hub,newer)['state']=='accepted'
    assert len(calls)==2
