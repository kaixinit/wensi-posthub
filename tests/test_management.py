from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from posthub.core import Hub, HubError
from posthub.models import AppInput, MessageInput, ProfileInput, TargetInput
from posthub.storage import Store, digest
from posthub.web import create_app
from .conftest import make_target
from .test_api import login


def message(targets=None, **fields):
    return MessageInput(**({'title':'上游通知','body':'消息内容','request_id':'event-1','target_ids':targets or []}|fields))


def test_delete_target_requires_disabled_and_preserves_history(hub, target):
    sent=hub.submit(message([target]))
    hub.tick()
    with pytest.raises(HubError,match='先停用'):
        hub.delete_item('targets',target)
    hub.set_enabled('targets',target,False)
    assert hub.delete_item('targets',target)=={'deleted':True,'cancelled':0}
    assert not hub.state()['targets']
    record=hub.message(sent['id'])['deliveries'][0]
    assert record['state']=='accepted' and record['target_name']=='值班目标'
    with hub.store.connect() as conn:
        row=conn.execute('SELECT * FROM targets WHERE id=?',(target,)).fetchone()
        assert hub.store.decrypt(row['config'])=={}
        assert row['verify_hash'] is None and row['binding_hash'] is None
        assert row['deleted'] is not None
    for action in [lambda:hub.target(target),lambda:hub.set_enabled('targets',target,True),lambda:hub.delete_item('targets',target),lambda:hub.save_target(TargetInput(name='back',kind='webhook',config={'url':'https://example.test'}),target)]:
        with pytest.raises(HubError) as exc:action()
        assert exc.value.status==404


def test_target_delete_cancels_only_its_pending_jobs_and_invalidates_route(hub,target):
    other=make_target(hub,'other')
    profile=hub.save_profile(ProfileInput(name='双渠道',target_ids=[target,other]))
    application=hub.create_app(AppInput(name='上游',target_ids=[target,other]))
    hub.store.set_setting('alert_target_id',target)
    ticket=hub.submit(message(),app=hub.authenticate_app(application['api_key']))
    hub.set_enabled('targets',target,False)
    info=hub.deletion_info('targets',target)
    assert info['pending']==1 and info['profiles']==['双渠道'] and info['apps']==['上游'] and info['alert_target']
    assert hub.delete_item('targets',target)['cancelled']==1
    hub.tick()
    records={d['target_id']:d for d in hub.message(ticket['id'])['deliveries']}
    assert records[target]['state']=='cancelled' and records[other]['state']=='accepted'
    assert len(hub.calls)==1
    assert hub.state()['apps'][0]['target_ids']==[other]
    assert hub.state()['profiles'][0]['target_ids']==[target,other]
    assert hub.store.setting('alert_target_id')==''
    new=hub.submit(message(request_id='new-event'),app=hub.authenticate_app(application['api_key']))
    assert [d['target_id'] for d in hub.message(new['id'])['deliveries']]==[other]
    assert len(hub.calls)==1


def test_target_delete_blocks_retry_but_ack_history_still_works(hub,target):
    hub.store.set_setting('public_base_url','https://notify.example.test')
    ticket=hub.submit(message([target],require_ack=True))
    hub.tick()
    token=hub.calls[-1][2]['ack_url'].rsplit('/',1)[1]
    did=hub.message(ticket['id'])['deliveries'][0]['id']
    hub.set_enabled('targets',target,False)
    hub.delete_item('targets',target)
    assert hub.acknowledge(token)['acknowledged']
    with hub.store.connect(True) as conn:
        conn.execute("UPDATE deliveries SET state='failed' WHERE id=?",(did,))
    with pytest.raises(HubError,match='目标不存在'):
        hub.retry(did)


def test_delete_app_revokes_key_cancels_only_its_jobs_and_keeps_history(hub,target):
    app=hub.create_app(AppInput(name='上游 A',target_ids=[target]))
    identity=hub.authenticate_app(app['api_key'])
    ticket=hub.submit(message([target]),app=identity)
    admin_ticket=hub.submit(message([target],request_id='admin-only'))
    with pytest.raises(HubError,match='先停用'):
        hub.delete_item('apps',app['id'])
    hub.set_enabled('apps',app['id'],False)
    assert hub.delete_item('apps',app['id'])['cancelled']==1
    assert hub.authenticate_app(app['api_key']) is None
    assert not hub.state()['apps']
    assert hub.message(ticket['id'])['deliveries'][0]['state']=='cancelled'
    hub.tick()
    assert hub.message(admin_ticket['id'])['deliveries'][0]['state']=='accepted'
    with hub.store.connect() as conn:
        row=conn.execute('SELECT * FROM apps WHERE id=?',(app['id'],)).fetchone()
        assert row['token_hash']!=digest(app['api_key']) and row['allowed']=='[]'
    for action in [lambda:hub.set_enabled('apps',app['id'],True),lambda:hub.update_app(app['id'],AppInput(name='back',target_ids=[target]))]:
        with pytest.raises(HubError) as exc:action()
        assert exc.value.status==404
    with pytest.raises(HubError):hub.submit(message([target],request_id='after-delete'),app=identity)


def test_deleted_app_cannot_retry_historical_failed_job(hub,target):
    app=hub.create_app(AppInput(name='上游',target_ids=[target]))
    ticket=hub.submit(message([target]),app=hub.authenticate_app(app['api_key']))
    did=hub.message(ticket['id'])['deliveries'][0]['id']
    with hub.store.connect(True) as conn:conn.execute("UPDATE deliveries SET state='failed' WHERE id=?",(did,))
    hub.set_enabled('apps',app['id'],False)
    hub.delete_item('apps',app['id'])
    with pytest.raises(HubError,match='原应用已删除'):hub.retry(did)


@pytest.mark.parametrize('table',['targets','apps'])
def test_actual_inflight_send_prevents_concurrent_deletion(hub,target,table):
    app=hub.create_app(AppInput(name='上游',target_ids=[target]))
    ticket=hub.submit(message([target]),app=hub.authenticate_app(app['api_key']))
    started,finish=threading.Event(),threading.Event()
    def sender(*args):
        started.set()
        assert finish.wait(5)
        return 'accepted'
    hub.sender=sender
    item=target if table=='targets' else app['id']
    with ThreadPoolExecutor() as pool:
        future=pool.submit(hub.tick)
        assert started.wait(5)
        try:
            hub.set_enabled(table,item,False)
            with pytest.raises(HubError,match='正在发送'):hub.delete_item(table,item)
        finally:finish.set()
        future.result(timeout=5)
    assert hub.message(ticket['id'])['deliveries'][0]['state']=='accepted'
    assert hub.delete_item(table,item)['deleted']


def test_app_routes_without_destination_fields_and_keeps_idempotency(hub,target):
    other=make_target(hub,'second')
    app=hub.create_app(AppInput(name='上游',target_ids=[target,other]))
    identity=hub.authenticate_app(app['api_key'])
    first=hub.submit(message(),app=identity)
    assert hub.submit(message(),app=identity)['deduplicated']
    hub.update_app(app['id'],AppInput(name='上游',target_ids=[other]))
    assert hub.submit(message(),app=identity)['id']==first['id']
    second=hub.submit(message(request_id='event-2'),app=identity)
    assert [d['target_id'] for d in hub.message(second['id'])['deliveries']]==[other]
    assert len(hub.message(first['id'])['deliveries'])==2


def test_app_edit_keeps_key_and_rechecks_permissions(hub,target):
    other=make_target(hub,'outside')
    app=hub.create_app(AppInput(name='app',target_ids=[target]))
    hub.update_app(app['id'],AppInput(name='updated',target_ids=[other]))
    identity=hub.authenticate_app(app['api_key'])
    assert identity['name']=='updated'
    ticket=hub.submit(message(),app=identity)
    assert hub.message(ticket['id'])['deliveries'][0]['target_id']==other
    with pytest.raises(HubError):hub.submit(message([target],request_id='unauthorized'),app=identity)


def test_standard_api_uses_this_apps_channels(hub,target):
    app=hub.create_app(AppInput(name='app',target_ids=[target]))
    ticket=hub.submit(message(),app=hub.authenticate_app(app['api_key']))
    assert [d['target_id'] for d in hub.message(ticket['id'])['deliveries']]==[target]


def test_upstream_cannot_reference_shared_profile(hub,target):
    profile=hub.save_profile(ProfileInput(name='route',target_ids=[target]))
    with pytest.raises(HubError,match='不再支持'):
        hub.create_app(AppInput(name='app',target_ids=[target],default_profile_id=profile['id']))
    app=hub.create_app(AppInput(name='app',target_ids=[target]))
    with pytest.raises(HubError,match='不再支持'):
        hub.update_app(app['id'],AppInput(name='app',target_ids=[target],default_profile_id=profile['id']))
    with pytest.raises(HubError,match='不再支持'):
        hub.submit(message(profile_id=profile['id']),app=hub.authenticate_app(app['api_key']))


def test_api_deletion_csrf_default_route_and_edit(environment):
    app=create_app(environment.directory,run_worker=False,sender=lambda *a:'accepted')
    hub=app.state.hub
    # Verify through a test hub and share the resulting database with the API.
    captured=[]
    test_hub=Hub(environment,sender=lambda *a:captured.append(a) or 'ok')
    test_hub.calls=captured
    target=make_target(test_hub)
    profile=hub.save_profile(ProfileInput(name='route',target_ids=[target]))
    with TestClient(app) as client:
        assert client.delete('/api/admin/targets/'+target).status_code==401
        headers=login(client)
        create=client.post('/api/admin/apps',headers=headers,json={'name':'upstream','target_ids':[target]})
        assert create.status_code==201
        identity=create.json()
        bearer={'Authorization':'Bearer '+identity['api_key']}
        ticket=client.post('/api/v1/messages',headers=bearer,json=message().model_dump())
        assert ticket.status_code==202
        edited=client.put('/api/admin/apps/'+identity['id'],headers=headers,json={'name':'renamed','target_ids':[target]})
        assert edited.status_code==200 and 'api_key' not in edited.json()
        assert client.delete('/api/admin/apps/'+identity['id'],headers=headers).status_code==409
        client.post('/api/admin/apps/'+identity['id']+'/enabled',headers=headers,json={'enabled':False})
        assert client.delete('/api/admin/apps/'+identity['id']).status_code==403
        assert client.get('/api/admin/apps/'+identity['id']+'/deletion').json()['pending']==1
        assert client.delete('/api/admin/apps/'+identity['id'],headers=headers).json()['cancelled']==1
        assert client.get('/api/v1/messages/'+ticket.json()['id'],headers=bearer).status_code==401
        client.post('/api/admin/targets/'+target+'/enabled',headers=headers,json={'enabled':False})
        assert client.delete('/api/admin/targets/'+target).status_code==403
        assert client.delete('/api/admin/targets/'+target,headers=headers).status_code==200
        assert client.get('/api/admin/messages/'+ticket.json()['id']).json()['deliveries'][0]['state']=='cancelled'


def test_upgrade_v1_preserves_targets_tokens_and_messages(environment):
    # Simulate the prior schema using a private test database, never user data.
    captured=[]
    hub=Hub(environment,sender=lambda *a:captured.append(a) or 'ok')
    hub.calls=captured
    target=make_target(hub)
    app=hub.create_app(AppInput(name='existing',target_ids=[target]))
    ticket=hub.submit(message([target]),app=hub.authenticate_app(app['api_key']))
    with sqlite3.connect(environment.path) as conn:
        conn.execute('ALTER TABLE targets DROP COLUMN deleted')
        conn.execute('ALTER TABLE apps DROP COLUMN deleted')
        conn.execute('ALTER TABLE apps DROP COLUMN default_profile_id')
        conn.execute('PRAGMA user_version=1')
    upgraded=Hub(Store(environment.directory))
    assert upgraded.target(target)['verified']
    assert upgraded.authenticate_app(app['api_key'])['id']==app['id']
    assert upgraded.message(ticket['id'])['deliveries'][0]['state']=='queued'
    assert upgraded.state()['apps'][0]['default_profile_id'] is None
    # Reopening is idempotent, and the recorded schema never gets downgraded.
    Store(environment.directory)
    with sqlite3.connect(environment.path) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0]==10
