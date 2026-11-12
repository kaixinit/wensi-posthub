"""Application -> channel picker -> binding -> return, using isolated data."""
import argparse
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

from posthub.storage import initialize
from posthub.core import HubError
from posthub.web import create_app


def wait_for(predicate):
    deadline=time.monotonic()+15
    while not predicate():
        assert time.monotonic()<deadline, 'Local delivery timed out'
        time.sleep(.05)


def run(args):
    out=Path(args.artifacts);out.mkdir(parents=True,exist_ok=True)
    work=Path(args.work_dir);work.mkdir(parents=True,exist_ok=True)
    received=[];errors=[]
    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
            self.wfile.write(b'{"accepted":true}')
        def log_message(self,*args):pass
    receiver=ThreadingHTTPServer(('127.0.0.1',0),Receiver)
    receiver_thread=threading.Thread(target=receiver.serve_forever,daemon=True);receiver_thread.start()
    listener=socket.socket();listener.bind(('127.0.0.1',0))
    base=f'http://127.0.0.1:{listener.getsockname()[1]}'
    with tempfile.TemporaryDirectory(dir=work) as directory:
        initialize(Path(directory),'isolated-channel-flow-password')
        app=create_app(directory)
        def isolated_qr_failure(target_id):
            raise HubError('隔离测试模拟二维码网络不可用', 502)
        app.state.bindings.qr_start=isolated_qr_failure
        server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False))
        thread=threading.Thread(target=lambda:server.run(sockets=[listener]),daemon=True);thread.start()
        try:
            wait_for(lambda:server.started)
            with httpx.Client(base_url=base,trust_env=False,timeout=10) as http, sync_playwright() as p:
                login=http.post('/api/login',json={'password':'isolated-channel-flow-password'},headers={'X-PostHub-Request':'1'})
                login.raise_for_status()
                http.headers.update({'X-PostHub-Request':'1','X-CSRF-Token':login.json()['csrf']})
                browser=p.chromium.launch(executable_path=args.browser,headless=True)
                page=browser.new_page(viewport={'width':1440,'height':1000})
                page.on('pageerror',lambda e:errors.append(str(e)))
                def shot(name):
                    page.evaluate('document.fonts.ready')
                    page.evaluate('Promise.all([...document.images].map(img => img.decode().catch(() => {})))')
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.screenshot(path=str(out/(name+'.png')))
                    if name=='02-channel-picker':
                        page.locator('#dialog').screenshot(path=str(out/'channel-picker-preview.png'))
                def choose(kind):
                    page.get_by_role('button',name='＋ 添加渠道',exact=True).click()
                    expect(page.get_by_role('heading',name='选择渠道',exact=True)).to_be_visible()
                    expect(page.locator('#dialog .channel-card')).to_have_count(8)
                    page.locator(f'#dialog [data-kind="{kind}"]').click()
                    expect(page.locator('#target-form')).to_be_visible()
                def draft(name):
                    choose('weixin')
                    page.get_by_label('渠道名称',exact=True).fill(name)
                    page.get_by_role('button',name='保存并进入扫码',exact=True).click()
                    expect(page.locator('#choice-dialog')).not_to_be_visible()
                    expect(page.locator('#verify-form')).to_be_visible()
                def http_target(name):
                    choose('webhook')
                    page.get_by_label('渠道名称',exact=True).fill(name)
                    page.get_by_label('接收端 HTTP(S) 地址',exact=True).fill(f'http://127.0.0.1:{receiver.server_port}/inbox')
                    page.get_by_role('button',name='继续绑定',exact=True).click()
                    page.get_by_role('button',name='保存草稿并继续绑定',exact=True).click()
                    before=len(received)
                    page.get_by_role('button',name='发送收件验证码',exact=True).click()
                    wait_for(lambda:len(received)>before)
                    code=re.search(r'验证码：(\d{6})',received[-1]['body'])[1]
                    page.get_by_role('button',name='查看发送结果',exact=True).click()
                    expect(page.get_by_role('heading',name='文思邮差 收件验证',exact=True)).to_be_visible()
                    page.get_by_role('button',name='关闭对话框',exact=True).click()
                    expect(page.locator('#verify-form')).to_be_visible()
                    page.get_by_label('收件验证码',exact=True).fill(code)
                    page.get_by_role('button',name='完成验证',exact=True).click()
                    expect(page.locator('#app-form')).to_be_visible()
                    expect(page.locator('#app-form').get_by_role('checkbox',name=re.compile(name))).to_be_checked()
                    return next(t['id'] for t in app.state.hub.state()['targets'] if t['name']==name)
                page.goto(base)
                page.get_by_label('管理员密码').fill('isolated-channel-flow-password')
                page.get_by_role('button',name='登录工作台').click()
                expect(page.get_by_role('heading',name='接入向导',exact=True)).to_be_visible()
                page.get_by_role('link',name='上游分发',exact=True).click()
                page.get_by_role('button',name='创建上游接口',exact=True).click()
                page.get_by_label('上游名称',exact=True).fill('未保存的应用内容')
                expect(page.locator('#app-downstream-options')).not_to_be_visible()
                shot('01-app-entry')
                page.get_by_role('button',name='＋ 添加渠道',exact=True).click()
                expect(page.get_by_role('heading',name='选择渠道',exact=True)).to_be_visible()
                shot('02-channel-picker')
                page.mouse.click(5,5)
                expect(page.get_by_role('heading',name='选择渠道',exact=True)).to_be_visible()
                page.keyboard.press('Escape')
                expect(page.get_by_label('上游名称',exact=True)).to_have_value('未保存的应用内容')
                choose('qq')
                page.get_by_role('button',name='取消',exact=True).click()
                expect(page.locator('#app-form')).to_be_visible()
                assert not app.state.hub.state()['apps'] and not app.state.hub.state()['targets']
                draft('保留的未验证草稿')
                page.get_by_role('button',name='关闭对话框',exact=True).click()
                page.get_by_role('button',name='继续绑定',exact=True).click()
                expect(page.locator('#verify-form')).to_be_visible()
                page.get_by_role('button',name='关闭对话框',exact=True).click()
                page.get_by_role('button',name='保留草稿并关闭',exact=True).click()
                expect(page.get_by_label('上游名称',exact=True)).to_have_value('未保存的应用内容')
                expect(page.locator('#app-form input[name=target_ids]:checked')).to_have_count(0)
                assert len(app.state.hub.state()['targets'])==1
                draft('待删除的草稿')
                page.get_by_role('button',name='关闭对话框',exact=True).click()
                page.get_by_role('button',name='删除草稿',exact=True).click()
                page.get_by_role('button',name='取消',exact=True).click()
                expect(page.locator('#verify-form')).to_be_visible()
                page.get_by_role('button',name='删除目标',exact=True).click()
                page.get_by_role('button',name='确认删除',exact=True).click()
                expect(page.get_by_label('上游名称',exact=True)).to_have_value('未保存的应用内容')
                assert len(app.state.hub.state()['targets'])==1
                target_a=http_target('新增 HTTP 目标 A')
                expect(page.get_by_label('上游名称',exact=True)).to_have_value('未保存的应用内容')
                expect(page.locator('#app-downstream-summary')).to_contain_text('已选 1 个目标')
                assert not app.state.hub.state()['apps']
                shot('03-bound-return-before-save')
                page.get_by_role('button',name='创建接口并生成地址',exact=True).click()
                expect(page.locator('#new-api-key')).to_be_visible()
                token=page.locator('#new-api-key').inner_text()
                app_id=app.state.hub.state()['apps'][0]['id']
                page.get_by_role('button',name='关闭对话框',exact=True).click()
                http.put('/api/admin/apps/'+app_id,json={'name':'已接入上游','target_ids':[target_a],'ack_minutes':17}).raise_for_status()
                page.reload()
                page.get_by_role('button',name='下游配置',exact=True).click()
                page.get_by_label('上游名称',exact=True).fill('编辑中尚未保存')
                page.locator('.app-receipt-settings > summary').click()
                page.get_by_label('确认等待时间（分钟）',exact=True).fill('27')
                target_b=http_target('新增 HTTP 目标 B')
                expect(page.get_by_label('上游名称',exact=True)).to_have_value('编辑中尚未保存')
                expect(page.locator('#app-default-profile')).to_have_count(0)
                expect(page.get_by_label('确认等待时间（分钟）',exact=True)).to_have_value('27')
                expect(page.locator('#app-channel-feedback')).to_contain_text('保存上游配置后生效')
                expect(page.locator('#app-form input[name=target_ids]:checked')).to_have_count(2)
                saved=app.state.hub.state()['apps'][0]
                assert saved['name']=='已接入上游' and saved['target_ids']==[target_a] and saved['ack_minutes']==17
                shot('04-independent-settings-preserved')
                expect(page.locator('#app-channel-feedback')).to_contain_text('保存上游配置后生效')
                page.locator('#app-form').get_by_role('checkbox',name=re.compile('新增 HTTP 目标 B')).uncheck()
                expect(page.locator('#app-channel-feedback')).not_to_be_visible()
                page.locator('#app-form').get_by_role('checkbox',name=re.compile('新增 HTTP 目标 B')).check()
                expect(page.locator('#app-channel-feedback')).to_be_visible()
                page.get_by_role('button',name='保存上游配置',exact=True).click()
                expect(page.locator('#dialog')).not_to_be_visible()
                ticket=http.post('/api/v1/hooks/'+token,json={'title':'新增渠道后的实际消息','body':'两端收到','request_id':'app-channel-flow'})
                ticket.raise_for_status();mid=ticket.json()['id']
                wait_for(lambda:len([r for r in received if r['message_id']==mid])==2)
                wait_for(lambda:all(d['state']=='accepted' for d in app.state.hub.message(mid)['deliveries']))
                assert set(app.state.hub.state()['apps'][0]['target_ids'])=={target_a,target_b}
                page.set_viewport_size({'width':390,'height':844})
                page.get_by_role('button',name='下游配置',exact=True).click()
                expect(page.locator('#app-downstream-options')).not_to_be_visible()
                shot('05-mobile-entry')
                page.get_by_role('button',name='＋ 添加渠道',exact=True).click()
                shot('06-mobile-picker')
                page.get_by_role('button',name='返回上游配置',exact=True).click()
                expect(page.locator('#app-form input[name=target_ids]:checked')).to_have_count(2)
                expect(page.get_by_label('上游名称',exact=True)).to_have_value('编辑中尚未保存')
                page.get_by_role('button',name='关闭对话框',exact=True).click()
                browser.close()
            assert not errors,errors
            result={'passed':True,'page_errors':errors,'real_http_notifications':2,'user_data_modified':False,'checks':['actual eight-channel picker from app','picker Escape restores unsaved form','outside click keeps picker open','unsaved QQ config cancelled without persistence','unverified draft retained without authorizing','cancel deletion returns to binding','delete draft restores app','verification detail closes back to binding','new target verified then selected in original form','app not saved during binding','name targets and independent receipt settings preserved','no shared profile entry','same key sends to newly authorized targets after save','mobile picker and return keep selections']}
            (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(result,ensure_ascii=False))
        finally:
            server.should_exit=True;thread.join(timeout=15);listener.close()
            receiver.shutdown();receiver.server_close();receiver_thread.join(timeout=3)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--browser',required=True)
    parser.add_argument('--work-dir',required=True)
    parser.add_argument('--artifacts',required=True)
    run(parser.parse_args())
