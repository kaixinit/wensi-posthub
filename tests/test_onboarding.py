import pytest
from fastapi.testclient import TestClient
from posthub.core import HubError
from posthub.models import AppInput, MessageInput, SetupTestInput, TargetInput
from posthub.onboarding import send_test
from posthub.web import create_app
from .conftest import make_target
from .test_api import login

def setup_state(hub,aid):return next(a for a in hub.state()['apps'] if a['id']==aid)
def send(hub,aid,number=1):return send_test(hub,aid,SetupTestInput(request_id=f'{number:032x}'))

def test_test_and_real_upstream_are_separate_and_scoped(hub,target):
    other=make_target(hub,'other');a=hub.create_app(AppInput(name='A',target_ids=[target]));b=hub.create_app(AppInput(name='B',target_ids=[other]))
    ticket=send(hub,a['id']);assert hub.tick() and not hub.tick()
    record=hub.message(ticket['id']);assert [d['target_id'] for d in record['deliveries']]==[target]
    sa=setup_state(hub,a['id']);assert sa['received_count']==0 and sa['last_received'] is None
    assert sa['setup']['test']['accepted'] and not sa['setup']['test']['confirmed'] and sa['setup']['upstream'] is None
    assert setup_state(hub,b['id'])['setup']['test'] is None
    hub.confirm_received(record['deliveries'][0]['id'])
    assert setup_state(hub,a['id'])['setup']['test']['confirmed']
    real=hub.submit(MessageInput(title='real',body='real',request_id='real-1'),app=hub.authenticate_app(a['api_key']))
    hub.tick();sa=setup_state(hub,a['id']);assert sa['received_count']==1 and sa['setup']['upstream']['id']==real['id'] and sa['setup']['upstream']['accepted']
    assert len(hub.calls)==2

def test_setup_request_idempotency_and_purpose_collision(hub,target):
    a=hub.create_app(AppInput(name='A',target_ids=[target]));one=send(hub,a['id']);two=send(hub,a['id'])
    assert two['id']==one['id'] and two['deduplicated'];hub.tick();assert len(hub.calls)==1
    m=hub.message(one['id'])
    with pytest.raises(HubError,match='request_id'):
        hub.submit(MessageInput(title=m['title'],body=m['body'],request_id=m['request_id'],require_ack=False),app=hub.authenticate_app(a['api_key']))
    assert setup_state(hub,a['id'])['received_count']==0

@pytest.mark.parametrize('change',['selection','revision','disabled','unverified'])
def test_old_test_cannot_complete_changed_configuration(hub,target,change):
    a=hub.create_app(AppInput(name='A',target_ids=[target]));ticket=send(hub,a['id']);hub.tick();hub.confirm_received(hub.message(ticket['id'])['deliveries'][0]['id'])
    assert setup_state(hub,a['id'])['setup']['test']['confirmed']
    if change=='selection':hub.update_app(a['id'],AppInput(name='A',target_ids=[target,make_target(hub,'new')]))
    elif change=='revision':
        hub.save_target(TargetInput(name='changed',kind='webhook',config={'url':'https://example.test/new'}),target)
        with hub.store.connect(True) as c:c.execute('UPDATE targets SET verified=1 WHERE id=?',(target,))
    elif change=='disabled':hub.set_enabled('targets',target,False)
    else:
        with hub.store.connect(True) as c:c.execute('UPDATE targets SET verified=0 WHERE id=?',(target,))
    s=setup_state(hub,a['id'])['setup'];assert not s['test']['confirmed'] and not s['test']['current_configuration']

def test_partial_and_paginated_history_not_misreported(hub,target):
    second=make_target(hub,'second');a=hub.create_app(AppInput(name='A',target_ids=[target,second]));ticket=send(hub,a['id']);hub.tick()
    assert not setup_state(hub,a['id'])['setup']['test']['accepted']
    hub.tick();ds=hub.message(ticket['id'])['deliveries'];hub.confirm_received(ds[0]['id'])
    assert not setup_state(hub,a['id'])['setup']['test']['confirmed']
    hub.confirm_received(ds[1]['id'])
    hub.clock.advance(1)
    for i in range(51):hub.submit(MessageInput(title='extra',body='x',request_id=f'extra-{i}',target_ids=[target]))
    assert ticket['id'] not in {m['id'] for m in hub.state()['messages']}
    assert setup_state(hub,a['id'])['setup']['test']['confirmed']

def test_missing_or_disabled_downstream_and_rate_limit(hub,target):
    a=hub.create_app(AppInput(name='A'))
    with pytest.raises(HubError,match='至少一个'):send(hub,a['id'])
    hub.update_app(a['id'],AppInput(name='A',target_ids=[target]));hub.set_enabled('apps',a['id'],False)
    with pytest.raises(HubError,match='已停用'):send(hub,a['id'])
    hub.set_enabled('apps',a['id'],True);hub.set_enabled('targets',target,False)
    with pytest.raises(HubError):send(hub,a['id'])
    hub.set_enabled('targets',target,True)
    for i in range(1,5):send(hub,a['id'],i)
    assert send(hub,a['id'],1)['deduplicated']
    with pytest.raises(HubError,match='最多'):send(hub,a['id'],6)

def test_setup_api_auth_csrf_validation_and_unknown_app(environment):
    app=create_app(environment.directory,run_worker=False)
    with TestClient(app) as c:
        endpoint='/api/admin/apps/does-not-exist/setup-test';data={'request_id':'a'*32}
        assert c.post(endpoint,json=data).status_code==401
        headers=login(c);assert c.post(endpoint,json=data).status_code==403
        assert c.post(endpoint,json={'request_id':'bad'},headers=headers).status_code==422
        assert c.post(endpoint,json=data|{'target_ids':['other']},headers=headers).status_code==422
        assert c.post(endpoint,json=data,headers=headers).status_code==404
