"""Progress derived from saved configuration and deliveries, never UI clicks."""
import json
from .models import MessageInput


def message_progress(hub, conn, app_id, purpose, allowed):
    row=conn.execute('SELECT id,created FROM messages WHERE app_id=? AND purpose=? ORDER BY created DESC,rowid DESC LIMIT 1',(app_id,purpose)).fetchone()
    if not row:return None
    deliveries=conn.execute('SELECT d.*,t.enabled,t.verified,t.deleted,t.revision AS current_revision FROM deliveries d JOIN targets t ON t.id=d.target_id WHERE message_id=? ORDER BY d.rowid',(row['id'],)).fetchall()
    current=bool(deliveries) and {d['target_id'] for d in deliveries}==set(allowed) and all(d['enabled'] and d['verified'] and d['deleted'] is None and d['revision']==d['current_revision'] for d in deliveries)
    return {'id':row['id'],'created':row['created'],'current_configuration':current,
            'accepted':current and all(d['state'] in {'accepted','acknowledged'} for d in deliveries),
            'confirmed':current and all(d['state']=='acknowledged' for d in deliveries),
            'states':{state:sum(d['state']==state for d in deliveries) for state in {d['state'] for d in deliveries}},
            'routing_state':hub.routing_state(conn,row['id'])}


def progress(hub, conn, app):
    allowed=app['target_ids']
    ready=[]
    for tid in allowed:
        row=conn.execute('SELECT enabled,verified,deleted FROM targets WHERE id=?',(tid,)).fetchone()
        if row and row['enabled'] and row['verified'] and row['deleted'] is None:ready.append(tid)
    return {'ready_count':len(ready),'configured_count':len(allowed),'channels_ready':bool(allowed) and not app.get('route_error') and len(ready)==len(allowed),
            'test':message_progress(hub,conn,app['id'],'setup_test',allowed),
            'upstream':message_progress(hub,conn,app['id'],'notification',allowed)}


def send_test(hub, app_id, data):
    from .core import HubError
    with hub.store.connect() as conn:
        app=conn.execute('SELECT * FROM apps WHERE id=? AND deleted IS NULL',(app_id,)).fetchone()
        if not app:raise HubError('上游接口不存在',404)
        if not app['enabled']:raise HubError('此上游已停用，请先启用接口',409)
        if not json.loads(app['allowed']):raise HubError('请先为此上游选择并保存至少一个已验证渠道',409)
        app=dict(app)
        previous=conn.execute('SELECT id FROM messages WHERE app_id=? AND request_id=?',(app_id,'setup-test-'+data.request_id)).fetchone()
    if not previous and hub.store.limited('setup-test:'+app_id,5,60,hub.clock()):raise HubError('一分钟内最多试发 5 次，请先核对已有测试结果',429)
    # Server owns the content, scope and purpose. An admin test cannot count as upstream ingress.
    payload=MessageInput(title='文思邮差 · 接入试发 · '+app['name'],body='这是平台发出的下游接入测试，不是上游业务消息。\n测试编号：'+data.request_id+'\n请核对实际收件，收到后在接入向导中确认。',request_id='setup-test-'+data.request_id,require_ack=False)
    return hub.submit(payload,app=app,purpose='setup_test')
