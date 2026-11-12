"""Run using a venv containing the wheel; refuses a source-tree import."""
import importlib.metadata,json,tempfile
from pathlib import Path
import posthub
from fastapi.testclient import TestClient
from posthub.storage import initialize
from posthub.web import create_app
assert 'site-packages' in str(Path(posthub.__file__)), 'Test must import installed wheel'
metadata=importlib.metadata.metadata('posthub')
assert metadata['Version']==posthub.__version__
assert 'MIT License' in metadata['License']
with tempfile.TemporaryDirectory(prefix='posthub-wheel-') as directory:
    initialize(Path(directory),'wheel-smoke-password')
    app=create_app(directory,run_worker=False,sender=lambda *args:'local-fixture')
    with TestClient(app) as c:
        assert c.get('/healthz').json()['version']==posthub.__version__
        assert c.get('/api/admin/state').status_code==401
        for asset in ['setup.js','recovery.js','app.js','help-data.js','fonts/NotoSansSC-ui.woff2']:
            assert c.get('/'+asset).status_code==200,asset
        login=c.post('/api/login',headers={'X-PostHub-Request':'1'},json={'username':'admin','password':'wheel-smoke-password'});assert login.status_code==200
        headers={'X-PostHub-Request':'1','X-CSRF-Token':login.json()['csrf']}
        response=c.post('/api/admin/apps',headers=headers,json={'name':'wheel smoke','target_ids':[]});assert response.status_code==201
        key=response.json()['api_key'];payload={'title':'local smoke','body':'No real recipients','request_id':'wheel-smoke-1'}
        sent=c.post('/api/v1/messages',headers={'Authorization':'Bearer '+key},json=payload);assert sent.status_code==202 and sent.json()['routing_state']=='awaiting_routing'
        repeat=c.post('/api/v1/messages',headers={'Authorization':'Bearer '+key},json=payload);assert repeat.json()['deduplicated'] and repeat.json()['id']==sent.json()['id']
print(json.dumps({'installed_wheel':'passed','version':posthub.__version__,'license':'MIT','external_notifications':0,'checks':['metadata','static assets','initialization','login','admin protection','upstream API','idempotency']},ensure_ascii=True))
