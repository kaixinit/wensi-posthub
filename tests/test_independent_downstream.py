import json
import pytest
from posthub.core import Hub, HubError
from posthub.models import AppInput, HookMessageInput, MessageInput, ProfileInput
from posthub.storage import Store
from .conftest import make_target


def send(hub, app, request_id):
    return hub.submit(HookMessageInput(body='隔离验收', request_id=request_id), app=hub.authenticate_app(app['api_key']))


def destinations(hub, ticket):
    return [d['target_id'] for d in hub.message(ticket['id'])['deliveries']]


def test_two_upstreams_keep_destinations_ack_and_queued_work_independent(hub,target):
    other=make_target(hub,'B')
    hub.store.set_setting('public_base_url','https://notify.example.test')
    a=hub.create_app(AppInput(name='A',target_ids=[target],require_ack=True,ack_minutes=17))
    b=hub.create_app(AppInput(name='B',target_ids=[other],ack_minutes=44))
    old_a=send(hub,a,'old');old_b=send(hub,b,'old')
    hub.update_app(a['id'],AppInput(name='A edited',target_ids=[target,other],ack_minutes=9))
    assert destinations(hub,send(hub,a,'new'))==[target,other]
    new_b=send(hub,b,'new')
    assert destinations(hub,new_b)==[other]
    assert not hub.message(new_b['id'])['require_ack'] and hub.message(new_b['id'])['ack_minutes']==44
    assert destinations(hub,old_a)==[target] and hub.message(old_a['id'])['require_ack']
    assert destinations(hub,old_b)==[other]
    hub.update_app(a['id'],AppInput(name='A emptied'))
    assert send(hub,a,'empty')['routing_state']=='awaiting_routing'
    assert destinations(hub,send(hub,b,'still-b'))==[other]


def test_v4_profiles_detach_once_preserving_scope_keys_ack_history(hub,target):
    other=make_target(hub,'B')
    hub.store.set_setting('public_base_url','https://notify.example.test')
    profile=hub.save_profile(ProfileInput(name='legacy shared',target_ids=[target],require_ack=True,ack_minutes=13))
    a=hub.create_app(AppInput(name='A',target_ids=[target,other]))
    b=hub.create_app(AppInput(name='B',target_ids=[target,other]))
    old=send(hub,a,'queued-before-upgrade')
    with hub.store.connect(True) as conn:
        conn.execute('UPDATE apps SET default_profile_id=?',(profile['id'],))
        conn.execute('PRAGMA user_version=4')
    migrated=Hub(Store(hub.store.directory),sender=lambda *args:'local')
    for app in (a,b):
        ticket=send(migrated,app,'after-upgrade')
        assert destinations(migrated,ticket)==[target]
        assert migrated.message(ticket['id'])['require_ack']
        assert migrated.message(ticket['id'])['ack_minutes']==13
    assert destinations(migrated,old)==[target,other]
    migrated.save_profile(ProfileInput(name='edited old shared',target_ids=[other]),profile['id'])
    migrated.update_app(a['id'],AppInput(name='A edited',target_ids=[other]))
    restarted=Hub(Store(hub.store.directory))
    assert destinations(restarted,send(restarted,a,'later-a'))==[other]
    assert destinations(restarted,send(restarted,b,'later-b'))==[target]
    with restarted.store.connect() as conn:
        assert conn.execute('SELECT count(*) FROM apps WHERE default_profile_id IS NOT NULL').fetchone()[0]==0
        assert conn.execute("SELECT count(*) FROM audit WHERE action='app.independent_downstream'").fetchone()[0]==2
        assert conn.execute('PRAGMA user_version').fetchone()[0]==10


@pytest.mark.parametrize('broken',['missing','unauthorized','empty'])
def test_broken_legacy_profile_is_blocked_until_explicit_configuration(hub,target,broken):
    other=make_target(hub,'other')
    profile=hub.save_profile(ProfileInput(name='legacy',target_ids=[other]))
    app=hub.create_app(AppInput(name='A',target_ids=[target]))
    with hub.store.connect(True) as conn:
        conn.execute('UPDATE apps SET default_profile_id=? WHERE id=?',('missing' if broken=='missing' else profile['id'],app['id']))
        if broken=='empty':conn.execute("UPDATE profiles SET targets='[]' WHERE id=?",(profile['id'],))
        conn.execute('PRAGMA user_version=4')
    migrated=Hub(Store(hub.store.directory))
    with pytest.raises(HubError) as error:send(migrated,app,'blocked')
    assert error.value.status==409
    state=migrated.state()['apps'][0]
    assert state['target_ids']==[target] and state['route_error']
    migrated.update_app(app['id'],AppInput(name='A',target_ids=[target]))
    assert destinations(migrated,send(migrated,app,'explicitly-saved'))==[target]


def test_system_profile_changes_cannot_change_upstream_route(hub,target):
    other=make_target(hub,'other')
    profile=hub.save_profile(ProfileInput(name='manual only',target_ids=[target]))
    app=hub.create_app(AppInput(name='A',target_ids=[target]))
    hub.save_profile(ProfileInput(name='manual edited',target_ids=[other]),profile['id'])
    assert destinations(hub,send(hub,app,'upstream'))==[target]
    manual=hub.submit(MessageInput(title='手动',body='manual',request_id='manual',profile_id=profile['id']))
    assert destinations(hub,manual)==[other]


def test_older_client_update_preserves_receipt_settings_unless_explicit(hub,target):
    app=hub.create_app(AppInput(name='A',target_ids=[target],require_ack=True,ack_minutes=19))
    hub.update_app(app['id'],AppInput(name='A renamed',target_ids=[target]))
    row=hub.state()['apps'][0]
    assert row['require_ack'] and row['ack_minutes']==19
    hub.update_app(app['id'],AppInput(name='A renamed',target_ids=[target],require_ack=False,ack_minutes=7))
    row=hub.state()['apps'][0]
    assert not row['require_ack'] and row['ack_minutes']==7
