"""Real Chrome and local SMTP plus controlled Microsoft authorization responses."""
import argparse
from contextlib import contextmanager
from email import policy
from email.parser import BytesParser
import json
from pathlib import Path
import re
import socket
import tempfile
import threading
import time
from unittest.mock import Mock

from aiosmtpd.controller import Controller
import httpx
from playwright.sync_api import sync_playwright,expect
import uvicorn

from posthub.storage import initialize
from posthub.web import create_app


def run(args):
    out=Path(args.artifacts);out.mkdir(parents=True,exist_ok=True);received=[];graph=[];errors=[]
    class Inbox:
        async def handle_DATA(self,server,session,envelope):received.append(envelope.content);return '250 accepted'
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));smtp_port=sock.getsockname()[1]
    mailbox=Controller(Inbox(),hostname='127.0.0.1',port=smtp_port);mailbox.start()
    stop=threading.Event()
    with tempfile.TemporaryDirectory(dir=args.work_dir) as directory:
        password='browser-email-password';initialize(Path(directory),password);app=create_app(directory,run_worker=False);hub=app.state.hub
        class Cache:
            has_state_changed=True
            def serialize(self):return 'browser-private-cache'
        microsoft=Mock();microsoft.initiate_device_flow.return_value={'user_code':'MOCK-CODE','device_code':'private-device','interval':5,'expires_in':900}
        microsoft.acquire_token_by_device_flow.return_value={'access_token':'private-token'}
        microsoft.get_accounts.return_value=[{'home_account_id':'browser-account'}]
        microsoft.acquire_token_silent_with_error.return_value={'access_token':'private-token'}
        http=Mock();http.get.return_value=httpx.Response(200,json={'mail':'test@outlook.com','userPrincipalName':'test@outlook.com','id':'browser-account'})
        def post(url,**kwargs):graph.append(kwargs['json']);return httpx.Response(202)
        http.post.side_effect=post
        @contextmanager
        def application(config,client_id,serialized=''):yield microsoft,Cache(),http
        hub.email.application=application
        def worker():
            while not stop.is_set():hub.store.set_setting('heartbeat',time.time());hub.tick();stop.wait(.1)
        background=threading.Thread(target=worker,daemon=True);background.start()
        listener=socket.socket();listener.bind(('127.0.0.1',0));base=f'http://127.0.0.1:{listener.getsockname()[1]}'
        server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False));thread=threading.Thread(target=lambda:server.run(sockets=[listener]),daemon=True);thread.start()
        try:
            until=time.monotonic()+10
            while not server.started:assert time.monotonic()<until;time.sleep(.05)
            with sync_playwright() as p:
                browser=p.chromium.launch(executable_path=args.browser,headless=True)
                page=browser.new_page(viewport={'width':1366,'height':1000});page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(base+'/#channels');page.get_by_label('管理员密码',exact=True).fill(password);page.get_by_role('button',name='登录工作台',exact=True).click()
                page.get_by_role('button',name='配置邮箱',exact=True).click()
                page.get_by_label('渠道名称',exact=True).fill('邮箱简化测试')
                page.get_by_label('发件邮箱',exact=True).fill('123456@qq.com')
                expect(page.locator('#email-provider-hint')).to_contain_text('QQ 邮箱')
                expect(page.get_by_label('SMTP 服务器',exact=True)).to_have_value('smtp.qq.com')
                expect(page.get_by_label('收件邮箱',exact=True)).to_have_value('123456@qq.com')
                expect(page.locator('#email-advanced')).not_to_have_attribute('open','')
                page.screenshot(path=str(out/'qq-preset-desktop.png'),full_page=True)
                page.get_by_label('发件邮箱',exact=True).fill('tester@gmail.com')
                expect(page.locator('#email-credential-label')).to_have_text('Google 应用专用密码')
                expect(page.get_by_label('SMTP 服务器',exact=True)).to_have_value('smtp.gmail.com')
                page.set_viewport_size({'width':390,'height':844});assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                page.screenshot(path=str(out/'gmail-preset-mobile.png'))
                mobile_submit=page.get_by_role('button',name='保存并发送验证码',exact=True)
                mobile_submit.scroll_into_view_if_needed();expect(mobile_submit).to_be_in_viewport()
                page.screenshot(path=str(out/'gmail-preset-mobile-submit.png'))
                page.set_viewport_size({'width':1366,'height':1000})
                page.get_by_label('邮箱服务商',exact=True).select_option('custom')
                page.get_by_label('发件邮箱',exact=True).fill('sender@example.test')
                page.get_by_label('发给自己（收件邮箱与发件邮箱相同）',exact=True).uncheck()
                page.get_by_label('收件邮箱',exact=True).fill('recipient@example.test')
                page.get_by_label('SMTP 服务器',exact=True).fill('127.0.0.1')
                page.get_by_label('端口',exact=True).fill(str(smtp_port))
                page.get_by_label('连接安全',exact=True).select_option('insecure')
                page.get_by_label('SMTP 登录账号',exact=True).fill('')
                page.get_by_role('button',name='保存并发送验证码',exact=True).click()
                expect(page.get_by_role('heading',name='邮箱渠道配置 · 邮箱简化测试',exact=True)).to_be_visible()
                expect(page.locator('#email-test-status')).to_contain_text('邮件服务已接受',timeout=15000)
                assert len(received)==1
                body=BytesParser(policy=policy.default).parsebytes(received[0]).get_content();code=re.search(r'验证码：(\d{6})',body)[1]
                page.screenshot(path=str(out/'smtp-verification.png'),full_page=True)
                page.get_by_label('收件验证码',exact=True).fill(code);page.get_by_role('button',name='完成验证',exact=True).click()
                expect(page.locator('#dialog')).not_to_be_visible()
                smtp_target=next(t for t in hub.state()['targets'] if t['kind']=='email');assert smtp_target['verified']
                # Re-open must show completed verification, not ask to send again.
                page.get_by_role('button',name='渠道管理',exact=False).first.click()
                page.locator(f'button[data-action="bind"][data-id="{smtp_target["id"]}"]').click()
                expect(page.get_by_role('heading',name='已完成验证',exact=True)).to_be_visible()
                page.get_by_role('button',name='关闭对话框',exact=True).click()
                page.get_by_role('button',name='添加渠道',exact=True).click();page.get_by_role('button',name=re.compile('邮箱')).last.click()
                page.get_by_label('渠道名称',exact=True).fill('微软授权测试');page.get_by_label('发件邮箱',exact=True).fill('test@outlook.com')
                expect(page.locator('#email-credential')).to_be_hidden();expect(page.locator('#email-advanced')).to_be_hidden()
                page.get_by_role('button',name='保存并连接微软邮箱',exact=True).click()
                expect(page.get_by_label('应用客户端 ID',exact=True)).to_be_visible()
                page.get_by_label('应用客户端 ID',exact=True).fill('11111111-2222-3333-4444-555555555555');page.get_by_role('button',name='保存客户端 ID',exact=True).click()
                page.get_by_role('button',name='连接微软邮箱',exact=True).click()
                expect(page.locator('#email-device-code')).to_have_value('MOCK-CODE')
                expect(page.get_by_role('link',name='打开微软授权页面 ↗',exact=True)).to_have_attribute('href','https://microsoft.com/devicelogin')
                page.screenshot(path=str(out/'outlook-authorization-mock.png'),full_page=True)
                expect(page.locator('#email-auth-status')).to_contain_text('微软邮箱已授权',timeout=15000)
                expect(page.locator('#email-test-status')).to_contain_text('邮件服务已接受',timeout=15000)
                assert len(graph)==1
                code=re.search(r'验证码：(\d{6})',graph[0]['message']['body']['content'])[1]
                page.get_by_label('收件验证码',exact=True).fill(code);page.get_by_role('button',name='完成验证',exact=True).click()
                expect(page.locator('#dialog')).not_to_be_visible()
                assert len(hub.state()['targets'])==2 and all(t['verified'] for t in hub.state()['targets'])
                assert not errors,errors;browser.close()
        finally:stop.set();background.join(5);server.should_exit=True;thread.join(10);mailbox.stop()
    result={'passed':True,'provider_detection':'passed','smtp_real_messages':len(received),'smtp_receipt_verification':'passed','outlook_authorization':'controlled responses, no real Microsoft account','outlook_graph_messages_simulated':len(graph),'completed_verification_ui':'passed','desktop_mobile':'passed','page_errors':errors,'external_mail_sent':0}
    (out/'browser-results.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--browser',required=True);parser.add_argument('--work-dir',required=True);parser.add_argument('--artifacts',required=True);run(parser.parse_args())
