"""Isolated browser + real HTTP acceptance; never touches the preview/user database.

python tests/browser_management.py --browser PATH_TO_CHROME --work-dir TEMP_DIR --artifacts ARTIFACT_DIR
Requires the development-only playwright package and a local Chrome executable.
"""
import argparse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import socket
import tempfile
import threading
import time

import httpx
from playwright.sync_api import sync_playwright, expect
import uvicorn

from posthub.core import HubError
from posthub.storage import initialize
from posthub.web import create_app


def run(args):
    out=Path(args.artifacts);out.mkdir(parents=True,exist_ok=True)
    workspace=Path(args.work_dir);workspace.mkdir(parents=True,exist_ok=True)
    received=[]
    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
            self.wfile.write(b'{"accepted":true}')
        def log_message(self,*args):pass
    receiver=ThreadingHTTPServer(('127.0.0.1',0),Receiver)
    receiver_thread=threading.Thread(target=receiver.serve_forever,daemon=True);receiver_thread.start()
    listener=socket.socket();listener.bind(('127.0.0.1',0))
    base='http://127.0.0.1:'+str(listener.getsockname()[1])
    errors=[];console=[]
    with tempfile.TemporaryDirectory(dir=workspace) as directory:
        initialize(Path(directory),'isolated-test-password-123')
        app=create_app(directory)
        def isolated_qr_failure(target_id):
            raise HubError('隔离测试模拟二维码网络不可用', 502)
        app.state.bindings.qr_start=isolated_qr_failure
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',log_level='error',access_log=False))
        service=threading.Thread(target=lambda:server.run(sockets=[listener]),daemon=True);service.start()
        try:
            deadline=time.monotonic()+10
            while not server.started:
                assert time.monotonic()<deadline,'Test server failed to start'
                time.sleep(.05)
            with sync_playwright() as p, httpx.Client(trust_env=False,timeout=10) as http:
                browser=p.chromium.launch(executable_path=args.browser,headless=True)
                context=browser.new_context(viewport={'width':1440,'height':1000},device_scale_factor=1)
                context.grant_permissions(['clipboard-read','clipboard-write'],origin=base)
                page=context.new_page()
                page.on('pageerror',lambda e:errors.append(str(e)))
                page.on('console',lambda e:console.append(e.text) if e.type=='error' and '401' not in e.text and not ('502' in e.text and e.location.get('url','').endswith('/weixin/qr')) else None)
                def shot(name):
                    page.evaluate('document.fonts.ready')
                    page.screenshot(path=str(out/(name+'.png')),full_page=False)
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),name
                def nav(name):
                    page.get_by_role('link',name=name,exact=True).click()
                    expect(page.get_by_role('heading',name=name,exact=True)).to_be_visible()
                def wait_for(predicate):
                    deadline=time.monotonic()+10
                    while not predicate():
                        assert time.monotonic()<deadline,'Timed out waiting for real receiver'
                        time.sleep(.1)
                page.goto(base)
                page.get_by_label('管理员密码').fill('isolated-test-password-123')
                page.get_by_role('button',name='登录工作台').click()
                expect(page.get_by_role('heading',name='下游渠道',exact=True)).to_be_visible()
                # Weixin explicitly saves and enters QR binding without a second draft dialog.
                page.get_by_role('button',name='扫码绑定',exact=True).click()
                page.get_by_label('渠道名称',exact=True).fill('未绑定配置测试')
                assert not app.state.hub.state()['targets']
                page.get_by_role('button',name='保存并进入扫码',exact=True).click()
                expect(page.locator('#choice-dialog')).not_to_be_visible()
                expect(page.get_by_role('button',name='发送收件验证码')).to_be_disabled()
                assert len(app.state.hub.state()['targets'])==1
                shot('00a-direct-scan-after-save')
                page.keyboard.press('Escape')
                expect(page.get_by_role('heading',name='尚未完成绑定与验证')).to_be_visible()
                shot('00b-unfinished-binding-choice')
                page.get_by_role('button',name='继续绑定',exact=True).click()
                expect(page.locator('#dialog').get_by_role('button',name='扫码绑定',exact=True)).to_be_visible()
                page.get_by_role('button',name='关闭',exact=True).click()
                page.get_by_role('button',name='保留草稿并关闭',exact=True).click()
                assert len(app.state.hub.state()['targets'])==1
                page.get_by_role('button',name='绑定',exact=True).click()
                page.get_by_role('button',name='关闭',exact=True).click()
                page.get_by_role('button',name='删除草稿',exact=True).click()
                page.get_by_role('button',name='确认删除',exact=True).click()
                expect(page.locator('#dialog')).not_to_be_visible()
                assert not app.state.hub.state()['targets']
                nav('上游分发')
                page.get_by_role('button',name='创建上游接口',exact=True).click()
                page.get_by_label('上游名称',exact=True).fill('先接入测试应用')
                expect(page.locator('#app-downstream-options')).not_to_be_visible()
                expect(page.locator('#app-downstream-summary')).to_have_text('暂未添加 · 可稍后配置')
                expect(page.get_by_role('button', name='＋ 添加渠道', exact=True)).to_be_visible()
                expect(page.get_by_text('密钥无自动到期时间，可随时停用或重置。', exact=True)).to_be_visible()
                shot('00d-create-without-downstream')
                page.get_by_role('button',name='创建接口并生成地址',exact=True).click()
                expect(page.locator('#new-hook-url')).to_be_visible()
                intake_url=page.locator('#new-hook-url').input_value()
                intake_key=page.locator('#new-api-key').inner_text()
                waiting=http.post(intake_url,json={'title':'先接入测试消息','body':'没有下游也应先保存','request_id':'intake-before-targets'})
                assert waiting.status_code==202 and waiting.json()['routing_state']=='awaiting_routing'
                intake_mid=waiting.json()['id']
                assert app.state.hub.message(intake_mid)['deliveries']==[]
                page.get_by_role('button',name='关闭',exact=True).click()
                page.reload()
                expect(page.get_by_text('已收到推送',exact=True)).to_be_visible()
                expect(page.get_by_text('待配置分发',exact=True)).to_be_visible()
                shot('00e-upstream-received-downstream-pending')
                nav('下游渠道')
                for name in ['测试接收端 A','测试接收端 B']:
                    page.get_by_role('button',name='添加接口',exact=True).click()
                    page.get_by_label('渠道名称',exact=True).fill(name)
                    page.get_by_label('接收端 HTTP(S) 地址',exact=True).fill(f'http://127.0.0.1:{receiver.server_port}/inbox')
                    page.get_by_role('button',name='继续绑定',exact=True).click()
                    page.get_by_role('button',name='保存草稿并继续绑定',exact=True).click()
                    expect(page.get_by_role('button',name='发送收件验证码')).to_be_visible()
                    before=len(received)
                    page.get_by_role('button',name='发送收件验证码').click()
                    wait_for(lambda:len(received)>before)
                    code=re.search(r'验证码：(\d{6})',received[-1]['body'])[1]
                    page.get_by_label('收件验证码',exact=True).fill(code)
                    page.get_by_role('button',name='完成验证',exact=True).click()
                    expect(page.get_by_role('dialog')).not_to_be_visible()
                nav('上游分发')
                page.get_by_role('button',name='下游配置',exact=True).click()
                expect(page.locator('#app-downstream-options')).not_to_be_visible()
                shot('08-edit-empty-collapsed')
                page.locator('#app-downstream > summary').focus()
                page.keyboard.press('Enter')
                expect(page.locator('#app-downstream-options')).to_be_visible()
                for name in ['测试接收端 A','测试接收端 B']:
                    page.locator('#app-form').get_by_role('checkbox',name=re.compile(name)).check()
                expect(page.locator('#app-downstream-summary')).to_contain_text('已选 2 个目标')
                shot('09-select-downstream-expanded')
                page.locator('#app-downstream > summary').click()
                expect(page.locator('#app-downstream-options')).not_to_be_visible()
                expect(page.locator('#app-form input[name=target_ids]:checked')).to_have_count(2)
                shot('10-selected-collapsed')
                page.get_by_role('button',name='保存上游配置',exact=True).click()
                expect(page.locator('#dialog')).not_to_be_visible()
                assert app.state.hub.message(intake_mid)['deliveries']==[]
                nav('投递记录')
                page.get_by_role('row').filter(has_text='先接入测试消息').get_by_role('button',name='详情',exact=True).click()
                expect(page.locator('.detail-summary')).to_contain_text('投递截止：')
                expect(page.locator('.message-expiry-note')).to_contain_text('不是密钥有效期')
                shot('00f-waiting-message')
                page.get_by_role('button',name='按当前配置分发',exact=True).click()
                page.get_by_role('button',name='确认分发',exact=True).click()
                wait_for(lambda:len([m for m in received if m['title']=='先接入测试消息'])==2)
                wait_for(lambda:all(d['state']=='accepted' for d in app.state.hub.message(intake_mid)['deliveries']))
                page.get_by_role('button',name='刷新状态',exact=True).click()
                expect(page.locator('.delivery-card')).to_have_count(2)
                expect(page.locator('.delivery-card').get_by_text('渠道已接受',exact=True)).to_have_count(2)
                shot('00g-released-to-two-targets')
                assert http.post(intake_url,json={'title':'先接入测试消息','body':'没有下游也应先保存','request_id':'intake-before-targets'}).json()['deduplicated']
                page.get_by_role('button',name='关闭',exact=True).click()
                nav('上游分发')
                page.get_by_role('button',name='停用密钥',exact=True).click()
                page.get_by_role('button',name='删除',exact=True).click()
                page.get_by_role('button',name='确认删除',exact=True).click()
                expect(page.locator('#dialog')).not_to_be_visible()
                expect(page.get_by_role('button',name='新建分发方案',exact=True)).to_have_count(0)
                page.get_by_role('button',name='创建上游接口',exact=True).click()
                page.get_by_label('上游名称',exact=True).fill('上游测试应用')
                expect(page.locator('#app-profile-options')).to_have_count(0)
                expect(page.locator('#app-downstream-options')).not_to_be_visible()
                page.locator('#app-downstream > summary').click()
                for name in ['测试接收端 A','测试接收端 B']:
                    page.get_by_role('dialog').get_by_role('checkbox',name=re.compile(name)).check()
                expect(page.locator('#app-form input[name=target_ids]:checked')).to_have_count(2)
                page.locator('#app-downstream > summary').click()
                page.get_by_role('button',name='创建接口并生成地址',exact=True).click()
                expect(page.locator('#new-api-key')).to_be_visible()
                token=page.locator('#new-api-key').inner_text()
                hook_url=page.locator('#new-hook-url').input_value()
                assert hook_url==base+'/api/v1/hooks/'+token
                page.get_by_role('button',name='复制完整地址',exact=True).click()
                assert page.evaluate('navigator.clipboard.readText()')==hook_url
                page.screenshot(path=str(out/'00c-generated-dedicated-url.png'),mask=[page.locator('#new-hook-url'),page.locator('#new-api-key')])
                page.get_by_role('button',name='已保存，查看接入配置',exact=True).click()
                expect(page.locator('#integration-json')).to_be_visible()
                assert page.locator('#integration-endpoint').input_value()==base+'/api/v1/messages'
                page.get_by_role('button',name='复制 JSON',exact=True).click()
                payload=json.loads(page.evaluate('navigator.clipboard.readText()'))
                assert 'target_ids' not in payload and 'profile_id' not in payload
                shot('01-upstream-config')
                page.get_by_text('Python 调用示例',exact=True).click()
                python_example=page.locator('#integration-python').inner_text()
                compile(python_example,'generated-upstream-example','exec')
                bearer={'Authorization':'Bearer '+token}
                ticket=http.post(hook_url,json=payload)
                assert ticket.status_code==202,ticket.text
                mid=ticket.json()['id']
                assert http.post(hook_url,json=payload).json()['deduplicated']
                wait_for(lambda:len([m for m in received if m['title']==payload['title']])==2)
                wait_for(lambda:all(d['state']=='accepted' for d in http.get(base+'/api/v1/messages/'+mid,headers=bearer).json()['deliveries']))
                assert len(http.get(base+'/api/v1/messages/'+mid,headers=bearer).json()['deliveries'])==2
                page.get_by_role('button',name='关闭对话框',exact=True).click()
                shot('02-apps-and-routes')
                page.get_by_role('button',name='下游配置',exact=True).click()
                page.locator('#app-downstream > summary').click()
                page.get_by_role('dialog').get_by_role('checkbox',name=re.compile('测试接收端 B')).uncheck()
                page.get_by_role('button',name='保存上游配置',exact=True).click()
                expect(page.get_by_role('dialog')).not_to_be_visible()
                second=http.post(hook_url,json=payload|{'request_id':'new-event-2'})
                assert second.status_code==202
                wait_for(lambda:len([m for m in received if m['title']==payload['title']])==3)
                assert len(http.get(base+'/api/v1/messages/'+second.json()['id'],headers=bearer).json()['deliveries'])==1
                assert len(http.get(base+'/api/v1/messages/'+mid,headers=bearer).json()['deliveries'])==2
                page.get_by_role('button',name='下游配置',exact=True).click()
                expect(page.locator('#app-downstream-options')).not_to_be_visible()
                expect(page.locator('#app-downstream-summary')).to_contain_text('已选 1 个目标')
                saved_target_ids=page.locator('#app-form input[name=target_ids]:checked').evaluate_all('(inputs) => inputs.map(input => input.value)')
                saved_ack_minutes=page.locator('#app-form input[name=ack_minutes]').input_value()
                page.set_viewport_size({'width':390,'height':844})
                shot('11-mobile-collapsed')
                page.locator('#app-downstream > summary').click()
                shot('12-mobile-expanded')
                page.locator('#app-downstream > summary').click()
                page.set_viewport_size({'width':1440,'height':1000})
                page.get_by_label('上游名称',exact=True).fill('上游测试应用（已配置）')
                page.get_by_role('button',name='保存上游配置',exact=True).click()
                expect(page.get_by_role('dialog')).not_to_be_visible()
                assert http.get(base+'/api/v1/messages/'+mid,headers=bearer).status_code==200
                page.get_by_role('button',name='下游配置',exact=True).click()
                expect(page.locator('#app-downstream-options')).not_to_be_visible()
                assert page.locator('#app-form input[name=ack_minutes]').input_value()==saved_ack_minutes
                assert page.locator('#app-form input[name=target_ids]:checked').evaluate_all('(inputs) => inputs.map(input => input.value)')==saved_target_ids
                page.get_by_role('button',name='关闭对话框',exact=True).click()
                page.set_viewport_size({'width':390,'height':844})
                shot('03-apps-mobile')
                page.get_by_role('button',name='接入配置',exact=True).click()
                shot('04-config-mobile')
                page.get_by_role('button',name='关闭对话框',exact=True).click()
                page.set_viewport_size({'width':1440,'height':1000})
                page.get_by_role('button',name='接入配置',exact=True).click()
                page.get_by_role('button',name='重置密钥并生成地址',exact=True).click()
                page.get_by_role('button',name='取消',exact=True).click()
                assert http.post(hook_url,json=payload).json()['deduplicated']
                page.get_by_role('button',name='重置密钥并生成地址',exact=True).click()
                page.get_by_role('button',name='重置并生成地址',exact=True).click()
                expect(page.locator('#new-api-key')).to_be_visible()
                new_token=page.locator('#new-api-key').inner_text()
                assert new_token!=token
                assert http.post(hook_url,json=payload).status_code==401
                hook_url=page.locator('#new-hook-url').input_value()
                bearer={'Authorization':'Bearer '+new_token}
                assert http.post(hook_url,json=payload).json()['deduplicated']
                page.get_by_role('button',name='关闭',exact=True).click()
                pending=http.post(base+'/api/v1/messages',headers=bearer,json=payload|{'request_id':'scheduled-delete','scheduled_at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()})
                assert pending.status_code==202
                page.get_by_role('button',name='停用密钥',exact=True).click()
                expect(page.get_by_role('button',name='删除',exact=True)).to_be_visible()
                page.get_by_role('button',name='删除',exact=True).click()
                expect(page.get_by_role('heading',name='删除上游应用',exact=True)).to_be_visible()
                shot('05-delete-app-confirmation')
                page.get_by_role('button',name='取消',exact=True).click()
                expect(page.get_by_role('button',name='启用密钥',exact=True)).to_be_visible()
                page.get_by_role('button',name='删除',exact=True).click()
                page.get_by_role('button',name='确认删除',exact=True).click()
                expect(page.get_by_role('dialog')).not_to_be_visible()
                expect(page.get_by_role('button',name='接入配置',exact=True)).to_have_count(0)
                assert http.get(base+'/api/v1/messages/'+mid,headers=bearer).status_code==401
                assert app.state.hub.message(pending.json()['id'])['deliveries'][0]['state']=='cancelled'
                nav('下游渠道')
                page.get_by_role('button',name='渠道管理',exact=False).click()
                row=page.get_by_role('row').filter(has_text='测试接收端 A')
                row.get_by_role('button',name='停用',exact=True).click()
                expect(row.get_by_role('button',name='删除',exact=True)).to_be_visible()
                shot('06-disabled-target-delete')
                row.get_by_role('button',name='删除',exact=True).click()
                expect(page.get_by_role('heading',name='删除收件目标',exact=True)).to_be_visible()
                shot('07-delete-target-confirmation')
                page.get_by_role('button',name='确认删除',exact=True).click()
                expect(page.get_by_role('dialog')).not_to_be_visible()
                expect(page.get_by_role('row').filter(has_text='测试接收端 A')).to_have_count(0)
                assert len(app.state.hub.message(mid)['deliveries'])==2
                assert all(d['state']=='accepted' for d in app.state.hub.message(mid)['deliveries'])
                nav('上游分发')
                expect(page.get_by_role('button',name='下游配置',exact=True)).to_have_count(0)
                assert not errors,errors
                assert not console,console
                browser.close()
            (out/'results.json').write_text(json.dumps({'passed':True,'page_errors':errors,'console_errors':console,'real_http_notifications':len([m for m in received if m['title']!='文思邮差 收件验证']),'checks':['create upstream with no recipients or channels', 'real intake persisted while unrouted', 'upstream received status and downstream pending status independent', 'configure downstream preserves key and does not replay old messages', 'explicitly release held message to two real receivers', 'idempotent resubmit after release']+['no target saved before confirmation', 'cancel draft creation', 'unbound verification button disabled', 'Escape prompts draft choice', 'continue and keep draft', 'delete enabled draft', 'copy dedicated URL', 'POST without Authorization header', 'reset key cancels old URL', 'cancel reset retains old URL']+['real recipient verification','independent downstream selection','create upstream with independent channels','copy JSON','generated Python compiles','202 intake without target IDs','real two-recipient delivery','idempotency','downstream update affects only new messages','edit app preserves key','mobile views','cancel deletion','delete disabled app cancels scheduled task and revokes key','delete disabled target clears connection and preserves history','no shared upstream profile controls']},ensure_ascii=False,indent=2),encoding='utf-8')
            print('Isolated browser + real upstream HTTP acceptance passed.')
        finally:
            server.should_exit=True;service.join(timeout=15)
            receiver.shutdown();receiver.server_close();receiver_thread.join(timeout=3)
            listener.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--browser',required=True)
    parser.add_argument('--work-dir',required=True)
    parser.add_argument('--artifacts',required=True)
    run(parser.parse_args())
