from datetime import datetime, timezone
import time
from uuid import uuid4

import httpx
import pytest

from posthub.adapters import DeliveryError, post_json
from posthub.core import Hub
from posthub.models import AppInput, MessageInput
from posthub.storage import Store
from .conftest import make_target


def submit(hub, target, **kwargs):
    return hub.submit(MessageInput(title='恢复测试', body='仅测试', target_ids=[target], request_id=uuid4().hex, **kwargs))['id']


def item(hub, mid):
    return hub.message(mid)['deliveries'][0]


def test_long_outage_restart_and_partial_success_never_resends(hub, target):
    other=make_target(hub, '暂时断线', {'url':'https://example.test/offline'})
    down=[True];calls=[]
    def send(kind, config, message, did):
        calls.append(config['url'])
        if config['url'].endswith('offline') and down[0]:raise DeliveryError('未连接','retry')
        return 'accepted-once'
    hub.sender=send
    payload=MessageInput(title='恢复测试',body='test',target_ids=[target,other],request_id='retry-partial')
    mid=hub.submit(payload)['id']
    hub.tick();hub.tick()
    for _ in range(5):
        hub.clock.advance(60);hub.tick()
    ds=hub.message(mid)['deliveries']
    assert [(d['state'],d['attempts']) for d in ds]==[('accepted',1),('retry',6)]
    assert len([a for a in hub.state()['alerts'] if a['reason']=='retrying' and not a['resolved']])==1
    assert hub.submit(payload)['deduplicated']
    # Durable retry survives an ordinary restart with no manual action.
    again=Hub(hub.store,sender=send,clock=hub.clock);again.recover();down[0]=False
    again.tick();assert len(calls)==7
    hub.clock.advance(60);again.tick()
    assert [(d['state'],d['attempts']) for d in again.message(mid)['deliveries']]==[('accepted',1),('accepted',7)]
    for _ in range(5):hub.clock.advance(60);again.tick()
    assert calls.count('https://example.test/receive')==1 and len(calls)==8
    assert all(a['resolved'] for a in hub.state()['alerts'] if a['reason']=='retrying')


def test_retry_stops_exactly_at_deadline_even_if_next_attempt_is_later(hub,target):
    calls=[]
    def fail(*args):calls.append(1);raise DeliveryError('断线','retry')
    hub.sender=fail
    mid=submit(hub,target,expires_at=datetime.fromtimestamp(hub.clock()+100,timezone.utc))
    hub.tick();hub.clock.advance(30);hub.tick();hub.clock.advance(60);hub.tick()
    assert item(hub,mid)['next_at']==hub.clock()+10
    with hub.store.connect(True) as conn:conn.execute('UPDATE deliveries SET next_at=? WHERE message_id=?',(hub.clock()+1000,mid))
    hub.clock.advance(10);hub.tick()
    assert item(hub,mid)['state']=='expired' and len(calls)==3
    assert item(hub,mid)['attempts']==3
    assert all(a['resolved'] for a in hub.state()['alerts'] if a['reason']=='retrying')
    hub.clock.advance(2000);hub.tick();assert len(calls)==3


def test_failure_returning_after_deadline_expires_without_retry(hub,target):
    def fail(*args):hub.clock.advance(120);raise DeliveryError('断线','retry')
    hub.sender=fail
    mid=submit(hub,target,expires_at=datetime.fromtimestamp(hub.clock()+60,timezone.utc))
    hub.tick();assert item(hub,mid)['state']=='expired'


def test_cancel_stops_automatic_retry_and_resolves_retry_alert(hub,target):
    def fail(*args):raise DeliveryError('断线','retry')
    hub.sender=fail;mid=submit(hub,target)
    for _ in range(4):hub.tick();hub.clock.advance(60)
    assert hub.cancel(mid)['cancelled']==1
    hub.clock.advance(60);hub.tick()
    assert item(hub,mid)['state']=='cancelled'
    assert all(a['resolved'] for a in hub.state()['alerts'] if a['reason']=='retrying')


def test_unknown_or_success_is_not_requeued_by_stale_completion(hub,target):
    mid=submit(hub,target);hub.tick();d=item(hub,mid)
    hub._finish(d,'retry','stale error')
    assert item(hub,mid)['state']=='accepted'
    def unknown(*args):raise DeliveryError('回执不明','unknown')
    hub.sender=unknown;mid2=submit(hub,target)
    hub.tick()
    for _ in range(10):hub.clock.advance(60);hub.tick()
    assert item(hub,mid2)['state']=='unknown' and item(hub,mid2)['attempts']==1


@pytest.mark.parametrize('exclusion',[None,'expired','disabled','unverified','deleted_target','revision','deleted_app','accepted_history','unknown_history','accepted','cancelled','permanent'])
def test_upgrade_resumes_only_safe_legacy_limit_failures(hub,target,exclusion):
    app=hub.create_app(AppInput(name='原上游',target_ids=[target]))
    mid=hub.submit(MessageInput(title='legacy',body='test',target_ids=[target],request_id='legacy'),app=hub.authenticate_app(app['api_key']))['id']
    did=item(hub,mid)['id']
    with hub.store.connect(True) as conn:
        conn.execute("UPDATE deliveries SET state='failed',attempts=3,completed=?,last_error='已达到 3 次尝试上限：连接拒绝' WHERE id=?",(hub.clock(),did))
        conn.execute("INSERT INTO attempts(delivery_id,at,state,detail) VALUES (?,?,'failed','原三次上限')",(did,hub.clock()))
        conn.execute("INSERT INTO alerts(id,delivery_id,reason,detail,created) VALUES ('legacy-alert',?,'failed','旧限制',?)",(did,hub.clock()))
        if exclusion=='expired':conn.execute('UPDATE messages SET expires=? WHERE id=?',(time.time()-1,mid))
        if exclusion=='disabled':conn.execute('UPDATE targets SET enabled=0 WHERE id=?',(target,))
        if exclusion=='unverified':conn.execute('UPDATE targets SET verified=0 WHERE id=?',(target,))
        if exclusion=='deleted_target':conn.execute('UPDATE targets SET deleted=? WHERE id=?',(hub.clock(),target))
        if exclusion=='revision':conn.execute('UPDATE targets SET revision=revision+1 WHERE id=?',(target,))
        if exclusion=='deleted_app':conn.execute('UPDATE apps SET deleted=? WHERE id=?',(hub.clock(),app['id']))
        if exclusion in {'accepted_history','unknown_history'}:conn.execute('INSERT INTO attempts(delivery_id,at,state,detail) VALUES (?,?,?,?)',(did,hub.clock(),exclusion.split('_')[0],'曾有结果'))
        if exclusion in {'accepted','cancelled'}:conn.execute('UPDATE deliveries SET state=? WHERE id=?',(exclusion,did))
        if exclusion=='permanent':conn.execute("UPDATE deliveries SET last_error='权限错误' WHERE id=?",(did,))
        conn.execute('PRAGMA user_version=7')
    migrated=Store(hub.store.directory)
    with migrated.connect() as conn:
        row=conn.execute('SELECT * FROM deliveries WHERE id=?',(did,)).fetchone()
        assert (row['state']=='retry')==(exclusion is None)
        assert row['attempts']==3
        if exclusion is None:
            assert row['completed'] is None
            assert conn.execute("SELECT resolved FROM alerts WHERE id='legacy-alert'").fetchone()[0]==1
        count=conn.execute("SELECT count(*) FROM audit WHERE action='delivery.resume_retry_limit'").fetchone()[0]
        assert count==(1 if exclusion is None else 0)
    Store(hub.store.directory)
    with migrated.connect() as conn:assert conn.execute("SELECT count(*) FROM audit WHERE action='delivery.resume_retry_limit'").fetchone()[0]==count


def test_proxy_explicit_rejection_is_safe_to_retry_and_ambiguous_5xx_is_not():
    for status,kind in [(407,'retry'),(429,'retry'),(502,'unknown')]:
        with httpx.Client(transport=httpx.MockTransport(lambda req:httpx.Response(status))) as client:
            with pytest.raises(DeliveryError) as error:post_json(client,'https://example.test',{})
        assert error.value.kind==kind
