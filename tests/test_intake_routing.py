from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import sqlite3

import pytest
from fastapi.testclient import TestClient

from posthub.core import Hub, HubError
from posthub.models import AppInput, HookMessageInput, ProfileInput
from posthub.storage import Store
from posthub.web import create_app
from .conftest import make_target
from .test_api import login


def intake(hub, **fields):
    app=hub.create_app(AppInput(name='先接上游'))
    identity=hub.authenticate_app(app['api_key'])
    data=HookMessageInput(body='waiting content',request_id='intake-1',**fields)
    ticket=hub.submit(data,app=identity,use_app_targets=True)
    return app, identity, data, ticket


def test_create_without_any_targets_accepts_and_preserves_message(hub):
    app,who,data,ticket=intake(hub)
    assert ticket['routing_state']=='awaiting_routing'
    assert not hub.tick() and not hub.calls
    record=hub.message(ticket['id'])
    assert record['body']=='waiting content' and record['deliveries']==[]
    assert hub.submit(data,app=who,use_app_targets=True)['deduplicated']
    state=hub.state()
    assert state['stats']['awaiting_routing']==1
    assert state['apps'][0]['received_count']==1 and state['apps'][0]['awaiting_count']==1
    assert state['messages'][0]['states']=={'awaiting_routing':1}
    with pytest.raises(HubError,match='配置下游'):hub.release_message(ticket['id'])


def test_configuring_does_not_replay_old_messages_and_release_is_atomic(hub):
    app,who,data,ticket=intake(hub)
    target=make_target(hub);other=make_target(hub,'second')
    hub.update_app(app['id'],AppInput(name='configured',target_ids=[target,other]))
    assert hub.authenticate_app(app['api_key'])['id']==app['id']
    assert hub.message(ticket['id'])['deliveries']==[]
    assert not hub.tick()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:hub.release_message(ticket['id']),range(2)))
    assert sum(not r['deduplicated'] for r in results)==1
    assert len(hub.message(ticket['id'])['deliveries'])==2
    assert hub.submit(data,app=who,use_app_targets=True)['routing_state']=='routed'
    assert hub.tick() and hub.tick() and not hub.tick()
    assert len(hub.calls)==2
    new=hub.submit(HookMessageInput(body='new'),app=who,use_app_targets=True)
    assert new['routing_state']=='routed' and len(hub.message(new['id'])['deliveries'])==2


@pytest.mark.parametrize('action',['cancel','expire','delete','disable'])
def test_held_messages_cannot_bypass_lifecycle(hub,action):
    app,who,data,ticket=intake(hub)
    target=make_target(hub)
    hub.update_app(app['id'],AppInput(name='later',target_ids=[target]))
    if action=='cancel':
        assert hub.cancel(ticket['id'])['cancelled_messages']==1
        expected='cancelled'
    elif action=='expire':
        hub.clock.advance(86401);hub.tick();expected='expired'
    elif action=='delete':
        hub.set_enabled('apps',app['id'],False)
        assert hub.deletion_info('apps',app['id'])['awaiting_messages']==1
        assert hub.delete_item('apps',app['id'])['cancelled_messages']==1
        expected='cancelled'
    else:
        hub.set_enabled('apps',app['id'],False);expected='awaiting_routing'
    with pytest.raises(HubError):hub.release_message(ticket['id'])
    assert hub.message(ticket['id'])['routing_state']==expected
    assert hub.message(ticket['id'])['deliveries']==[] and not hub.calls


def test_release_inherits_independent_ack_and_schedule(hub):
    schedule=datetime.fromtimestamp(hub.clock()+3600,timezone.utc)
    app,who,data,ticket=intake(hub,scheduled_at=schedule)
    target=make_target(hub)
    hub.update_app(app['id'],AppInput(name='configured',target_ids=[target],require_ack=True,ack_minutes=12))
    hub.set_enabled('targets',target,False)
    with pytest.raises(HubError):hub.release_message(ticket['id'])
    assert not hub.message(ticket['id'])['deliveries']
    hub.set_enabled('targets',target,True)
    with pytest.raises(HubError,match='平台地址'):hub.release_message(ticket['id'])
    hub.store.set_setting('public_base_url','https://notify.example.test')
    hub.release_message(ticket['id'])
    record=hub.message(ticket['id'])
    assert record['require_ack'] and record['ack_minutes']==12
    assert record['deliveries'][0]['next_at']==schedule.timestamp()
    assert not hub.tick()


def test_standard_and_hook_intake_share_pending_message_and_admin_route_is_protected(environment):
    service=create_app(environment.directory,run_worker=False)
    with TestClient(service) as client:
        headers=login(client)
        result=client.post('/api/admin/apps',headers=headers,json={'name':'minimal'})
        assert result.status_code==201
        app=result.json();bearer={'Authorization':'Bearer '+app['api_key']}
        payload={'title':'upstream','body':'payload','request_id':'same-event'}
        ticket=client.post('/api/v1/hooks/'+app['api_key'],json=payload)
        assert ticket.status_code==202 and ticket.json()['routing_state']=='awaiting_routing'
        assert client.post('/api/v1/messages',headers=bearer,json=payload).json()['deduplicated']
        mid=ticket.json()['id']
        assert client.get('/api/v1/messages/'+mid,headers=bearer).json()['deliveries']==[]
        assert client.post('/api/admin/messages/'+mid+'/route').status_code==403
        assert client.post('/api/admin/messages/'+mid+'/route',headers=headers).status_code==409
        assert client.put('/api/admin/apps/'+app['id'],headers=headers,json={'name':'still no downstream'}).status_code==200


def test_v2_migration_and_restart_preserve_intake(environment):
    with sqlite3.connect(environment.path) as conn:
        conn.execute('DROP TABLE message_routes')
        conn.execute('PRAGMA user_version=2')
    upgraded=Hub(Store(environment.directory))
    app,who,data,ticket=intake(upgraded)
    restarted=Hub(Store(environment.directory))
    assert restarted.message(ticket['id'])['routing_state']=='awaiting_routing'
    assert restarted.authenticate_app(app['api_key'])['id']==app['id']
    assert restarted.submit(data,app=who,use_app_targets=True)['deduplicated']
    with restarted.store.connect() as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0]==10


def test_clearing_routes_preserves_already_queued_targets(hub,target):
    app=hub.create_app(AppInput(name='existing',target_ids=[target]))
    who=hub.authenticate_app(app['api_key'])
    first=hub.submit(HookMessageInput(body='before'),app=who,use_app_targets=True)
    hub.update_app(app['id'],AppInput(name='temporarily unconfigured'))
    second=hub.submit(HookMessageInput(body='after'),app=who,use_app_targets=True)
    assert first['routing_state']=='routed' and second['routing_state']=='awaiting_routing'
    assert hub.tick() and len(hub.calls)==1
    assert hub.message(first['id'])['deliveries'][0]['state']=='accepted'
