import json
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from posthub import cli
from posthub.models import AppInput
from posthub.storage import Store, digest, initialize, password_matches
from posthub.web import create_app
from .test_api import login
from .conftest import make_target

OLD='test-password-12345'
NEW='new-account-password-67890'


def change_body(current=OLD,new=NEW):
    return {'current_password':current,'new_password':new,'confirm_password':new}


def test_login_username_and_backward_compatible_password_only(environment):
    app=create_app(environment.directory,run_worker=False)
    with TestClient(app) as client:
        r=client.post('/api/login',headers={'X-PostHub-Request':'1'},json={'username':'wrong','password':OLD})
        assert r.status_code==401 and '账号或密码' in r.json()['detail']
        headers=login(client)
        assert client.get('/api/session').json()['username']=='admin'
        assert client.get('/api/admin/account').json()=={'username':'admin','must_change_password':False}
        assert 'password_hash' not in client.get('/api/admin/state').text


def test_change_password_invalidates_all_sessions_but_not_api_keys(environment):
    app=create_app(environment.directory,run_worker=False,sender=lambda *a:'local')
    hub=app.state.hub
    upstream=hub.create_app(AppInput(name='independent'))
    before=json.loads(environment.config_path.read_text(encoding='utf-8'))
    with TestClient(app) as first,TestClient(app) as second:
        headers=login(first);login(second)
        saved_cookie=first.cookies.get('posthub_session')
        with environment.connect() as conn:saved_session=dict(conn.execute('SELECT * FROM sessions WHERE token_hash=?',(digest(saved_cookie),)).fetchone())
        r=first.post('/api/admin/account/password',headers=headers,json=change_body())
        assert r.status_code==200 and r.json()['relogin_required']
        assert first.get('/api/session').status_code==401
        assert second.get('/api/session').status_code==401
        # Even an old session row restored after a partial failure cannot authenticate.
        with environment.connect(True) as conn:conn.execute('INSERT INTO sessions VALUES (?,?,?,?)',tuple(saved_session[k] for k in ('token_hash','csrf','expires','auth_version')))
        first.cookies.set('posthub_session',saved_cookie)
        assert first.get('/api/session').status_code==401
        for password,expected in [(OLD,401),(NEW,200)]:
            r=first.post('/api/login',headers={'X-PostHub-Request':'1'},json={'username':'admin','password':password})
            assert r.status_code==expected
        assert first.post('/api/v1/messages',headers={'Authorization':'Bearer '+upstream['api_key']},json={'title':'after change','body':'still receives','request_id':'account-test'}).status_code==202
    after=json.loads(environment.config_path.read_text(encoding='utf-8'))
    assert before['encryption_key']==after['encryption_key'] and password_matches(NEW,after['password_hash'])
    assert not after['must_change_password'] and NEW not in environment.config_path.read_text(encoding='utf-8')


def test_password_validation_auth_csrf_and_rate_limit(environment):
    app=create_app(environment.directory,run_worker=False)
    endpoint='/api/admin/account/password'
    with TestClient(app) as client:
        assert client.post(endpoint,json=change_body()).status_code==401
        headers=login(client)
        assert client.post(endpoint,json=change_body()).status_code==403
        before=environment.config_path.read_bytes()
        r=client.post(endpoint,headers=headers,json=change_body(new='short'))
        assert r.status_code==422 and 'short' not in r.text
        r=client.post(endpoint,headers=headers,json=change_body()|{'confirm_password':'different-password'})
        assert r.status_code==400
        assert client.post(endpoint,headers=headers,json=change_body(new=OLD)).status_code==400
        for i in range(5):assert client.post(endpoint,headers=headers,json=change_body(current='invalid-current')).status_code==400
        assert client.post(endpoint,headers=headers,json=change_body()).status_code==429
        assert environment.config_path.read_bytes()==before
        assert client.get('/api/session').status_code==200


def test_generated_initial_password_requires_change_and_cli_reset(tmp_path,monkeypatch,capsys):
    generated='generated-initial-98765'
    monkeypatch.setattr(cli.secrets,'token_urlsafe',lambda _:generated)
    monkeypatch.setattr('sys.argv',['posthub','init','--generate-password','--data-dir',str(tmp_path)])
    cli.main()
    output=capsys.readouterr().out
    assert generated in output and 'admin' in output
    app=create_app(tmp_path,run_worker=False)
    with TestClient(app) as client:
        r=client.post('/api/login',headers={'X-PostHub-Request':'1'},json={'username':'admin','password':generated})
        headers={'X-PostHub-Request':'1','X-CSRF-Token':r.json()['csrf']}
        assert r.json()['must_change_password']
        assert client.get('/api/admin/state').status_code==200
        assert client.post('/api/admin/apps',headers=headers,json={'name':'blocked'}).status_code==403
        assert client.post('/api/admin/account/password',headers=headers,json=change_body(current=generated)).status_code==200
        r=client.post('/api/login',headers={'X-PostHub-Request':'1'},json={'username':'admin','password':NEW})
        headers={'X-PostHub-Request':'1','X-CSRF-Token':r.json()['csrf']}
        assert not r.json()['must_change_password']
        assert client.post('/api/admin/apps',headers=headers,json={'name':'allowed'}).status_code==201
        monkeypatch.setattr('sys.argv',['posthub','reset-password','--data-dir',str(tmp_path)])
        monkeypatch.setattr(cli,'getpass',lambda _: 'offline-recovery-password')
        cli.main();capsys.readouterr()
        assert client.get('/api/session').status_code==401
        assert client.post('/api/login',headers={'X-PostHub-Request':'1'},json={'password':'offline-recovery-password'}).status_code==200


def test_v6_session_upgrade_preserves_current_password_and_cookie(environment):
    app=create_app(environment.directory,run_worker=False)
    with TestClient(app) as client:
        login(client);cookie=client.cookies.get('posthub_session')
    with sqlite3.connect(environment.path) as conn:
        conn.execute('ALTER TABLE sessions DROP COLUMN auth_version')
        conn.execute('PRAGMA user_version=6')
    before=environment.config_path.read_bytes()
    upgraded=create_app(environment.directory,run_worker=False)
    with TestClient(upgraded) as client:
        client.cookies.set('posthub_session',cookie)
        assert client.get('/api/session').status_code==200
        assert client.get('/api/admin/account').json()['username']=='admin'
    assert environment.config_path.read_bytes()==before
    Store(environment.directory)
    with environment.connect() as conn:assert conn.execute('PRAGMA user_version').fetchone()[0]==10


def test_password_update_preserves_spaces_and_channel_secrets(hub):
    tid=make_target(hub,config={'url':'https://example.test/secret','bearer_token':'private-channel-key'})
    before=hub.target(tid)
    hub.store.change_password(' spaced-password-98765 ',current_password=OLD)
    assert hub.target(tid)==before
    cfg=json.loads(hub.store.config_path.read_text(encoding='utf-8'))
    assert password_matches(' spaced-password-98765 ',cfg['password_hash'])
    assert not password_matches('spaced-password-98765',cfg['password_hash'])
