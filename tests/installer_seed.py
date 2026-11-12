"""Seed an isolated instance using the actually installed old/current wheel."""
import json
from pathlib import Path
import re
import sys
import time
import urllib.request

import posthub
from posthub.core import Hub
from posthub.models import AppInput, MessageInput, ProxyInput, TargetInput
from posthub.storage import Store, initialize

data, receiver, case, output = sys.argv[1:]
data = Path(data)
if not (data / 'config.json').exists():
    initialize(data, 'installer-acceptance-password')
store = Store(data)
hub = Hub(store)
targets = []
for endpoint in ['a', 'b']:
    tid = hub.save_target(TargetInput(name=case + endpoint, kind='webhook', config={'url':receiver + '/' + endpoint, 'bearer_token':'fixture-receiver-secret'}))['id']
    hub.verify_start(tid)
    hub.tick()
    with urllib.request.urlopen(receiver + '/received') as response:
        received = json.load(response)
    code = re.search(r'验证码：(\d{6})', received[-1]['body'])[1]
    hub.verify_finish(tid, code)
    targets.append(tid)
for kind, config in [('qq', {'app_id':'123456', 'app_secret':'synthetic-qq-secret', 'target_type':'user', 'address':'fixture-openid', 'event_mode':'webhook'}),
        ('weixin', {}), ('email', {'host':'smtp.example.test','port':465,'username':'fixture@example.test','password':'synthetic-smtp-secret','sender':'fixture@example.test','recipient':'receiver@example.test','security':'ssl'})]:
    tid = hub.save_target(TargetInput(name=case + kind, kind=kind, config=config))['id']
    with store.connect(True) as conn:
        conn.execute('UPDATE targets SET enabled=0,verified=1 WHERE id=?',(tid,))
hub.network.save_proxy(ProxyInput(name=case+'-proxy',url='http://fixture:synthetic-proxy-secret@127.0.0.1:7890'))
app = hub.create_app(AppInput(name=case+'-upstream-A',target_ids=targets))
other = hub.create_app(AppInput(name=case+'-upstream-B',target_ids=[targets[0]],require_ack=True,ack_minutes=7))
empty = hub.create_app(AppInput(name=case+'-waiting'))
who = hub.authenticate_app(app['api_key'])
payload = lambda suffix, ids: MessageInput(title=case+'-'+suffix,body='Isolated installation acceptance only',request_id=case+'-'+suffix,target_ids=ids)
history = hub.submit(payload('history',[targets[0]]),app=who)['id']; hub.tick()
partial = hub.submit(payload('partial',targets),app=who)['id']; hub.tick(); hub.tick()
assert sorted(d['state'] for d in hub.message(partial)['deliveries']) == ['accepted','retry']
cancelled = hub.submit(payload('cancelled',[targets[0]]),app=who)['id']; hub.cancel(cancelled)
queued = hub.submit(payload('queued',[targets[0]]),app=who)['id']
waiting = hub.submit(payload('waiting',[]),app=hub.authenticate_app(empty['api_key']),use_app_targets=True)['id']
with store.connect() as conn:
    schema = conn.execute('PRAGMA user_version').fetchone()[0]
fixture = {'version':posthub.__version__,'schema':schema,'api_key':app['api_key'],'app_id':app['id'],
    'other':other,'targets':targets,'history':history,'partial':partial,'queued':queued,'cancelled':cancelled,'waiting':waiting}
Path(output).write_text(json.dumps(fixture),encoding='utf-8')
print('Isolated fixtures prepared.')
