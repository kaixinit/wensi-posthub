import pytest
from fastapi.testclient import TestClient

from posthub.core import HubError
from posthub.models import AppInput, HookMessageInput, ProfileInput, TargetInput
from posthub.web import create_app
from .conftest import make_target
from .test_api import login


def test_unverified_target_can_be_deleted_without_disabling(hub):
    target=hub.save_target(TargetInput(name='unused',kind='weixin'))['id']
    assert hub.deletion_info('targets',target)['verified'] is False
    assert hub.delete_item('targets',target)=={'deleted':True,'cancelled':0}
    assert hub.state()['targets']==[]


def test_delete_draft_cancels_verification_and_prevents_confirmation(hub):
    target=hub.save_target(TargetInput(name='draft',kind='webhook',config={'url':'https://example.test'}))['id']
    message=hub.verify_start(target)
    assert hub.delete_item('targets',target)['cancelled']==1
    assert hub.message(message['id'])['deliveries'][0]['state']=='cancelled'
    with pytest.raises(HubError):hub.verify_finish(target,'123456')
    assert not hub.tick()


def test_hook_defaults_are_resolved_transactionally_and_deduplicated(hub,target):
    other=make_target(hub,'second')
    app=hub.create_app(AppInput(name='upstream',target_ids=[target,other]))
    who=hub.authenticate_app(app['api_key'])
    payload=HookMessageInput(body='notify',request_id='event1')
    first=hub.submit(payload,app=who,use_app_targets=True)
    assert len(hub.message(first['id'])['deliveries'])==2
    hub.update_app(app['id'],AppInput(name='upstream',target_ids=[other]))
    assert hub.submit(payload,app=who,use_app_targets=True)['deduplicated']
    second=hub.submit(HookMessageInput(body='notify',request_id='event2'),app=who,use_app_targets=True)
    assert [d['target_id'] for d in hub.message(second['id'])['deliveries']]==[other]
    with pytest.raises(HubError):
        hub.submit(HookMessageInput(body='forbidden',target_ids=[target]),app=who,use_app_targets=True)


def test_hook_independent_config_and_unusable_recipient_fail_closed(hub,target):
    other=make_target(hub,'second')
    profile=hub.save_profile(ProfileInput(name='selected',target_ids=[other]))
    app=hub.create_app(AppInput(name='upstream',target_ids=[other]))
    who=hub.authenticate_app(app['api_key'])
    sent=hub.submit(HookMessageInput(body='body'),app=who,use_app_targets=True)
    assert [d['target_id'] for d in hub.message(sent['id'])['deliveries']]==[other]
    hub.set_enabled('targets',other,False)
    with pytest.raises(HubError):hub.submit(HookMessageInput(body='body'),app=who,use_app_targets=True)


def test_hook_http_contract_key_rotation_and_disabled_keys(environment):
    service=create_app(environment.directory,run_worker=False,sender=lambda *a:'accepted')
    hub=service.state.hub
    # make_target requires captured verification messages, not a real provider.
    hub.calls=[]
    hub.sender=lambda *args: hub.calls.append(args) or 'accepted'
    target=make_target(hub)
    app=hub.create_app(AppInput(name='app',target_ids=[target]))
    url='/api/v1/hooks/'+app['api_key']
    with TestClient(service) as client:
        payload={'title':'文思量化通知','body':'hello','request_id':'event-one'}
        first=client.post(url,json=payload)
        assert first.status_code==202
        assert client.post(url,json=payload).json()['deduplicated']
        assert client.post(url,json=payload|{'body':'changed'}).status_code==409
        assert client.post('/api/v1/hooks/ph_invalid',json=payload).status_code==401
        before=len(hub.state()['messages'])
        assert client.get(url).status_code in (404,405)
        assert len(hub.state()['messages'])==before
        assert client.post(url,json={}).status_code==422
        one=client.post(url,json={'body':'minimal'});two=client.post(url,json={'body':'minimal'})
        assert one.status_code==two.status_code==202 and one.json()['id']!=two.json()['id']
        headers=login(client)
        route='/api/admin/apps/'+app['id']+'/rotate-key'
        assert client.post(route).status_code==403
        renewed=client.post(route,headers=headers)
        assert renewed.status_code==200
        key=renewed.json()['api_key'];new_url='/api/v1/hooks/'+key
        assert key not in client.get('/api/admin/state').text
        assert client.post(url,json=payload).status_code==401
        assert client.post(new_url,json=payload).json()['deduplicated']
        hub.set_enabled('apps',app['id'],False)
        assert client.post(new_url,json=payload).status_code==401
        again=hub.rotate_app_key(app['id'])
        assert hub.authenticate_app(again['api_key']) is None
        hub.delete_item('apps',app['id'])
        with pytest.raises(HubError):hub.rotate_app_key(app['id'])


def test_hook_and_standard_api_share_rate_limit(environment):
    service=create_app(environment.directory,run_worker=False)
    hub=service.state.hub;hub.calls=[]
    hub.sender=lambda *args:hub.calls.append(args) or 'ok'
    target=make_target(hub)
    app=hub.create_app(AppInput(name='limit',target_ids=[target]))
    who=hub.authenticate_app(app['api_key'])
    with environment.connect(True) as conn:
        conn.execute('INSERT INTO limits VALUES (?,120,?)',('app:'+who['id'],hub.clock()+60))
    with TestClient(service) as client:
        assert client.post('/api/v1/hooks/'+app['api_key'],json={'body':'test'}).status_code==429
        assert client.get('/api/v1/messages/none',headers={'Authorization':'Bearer '+app['api_key']}).status_code==429
