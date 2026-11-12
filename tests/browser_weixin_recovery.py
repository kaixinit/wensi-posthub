"""Real Chrome + isolated Hub/API, controlled provider rejection and inbound context."""
import argparse
import json
from pathlib import Path
import socket
import tempfile
import threading
import time

import uvicorn
from playwright.sync_api import sync_playwright, expect
from posthub.adapters import check_weixin_send
from posthub.bindings import Bindings
from posthub.models import MessageInput, TargetInput
from posthub.storage import initialize
from posthub.web import create_app


def run(args):
    out=Path(args.artifacts);out.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=args.work_dir) as directory:
        password='isolated-browser-weixin-password';initialize(Path(directory),password)
        app=create_app(directory,run_worker=False);hub=app.state.hub;calls=[]
        def send(kind,config,message,did):
            calls.append((kind,did))
            if kind=='weixin' and config['context_token']=='old-context':
                check_weixin_send({'ret':-2,'errmsg':'prepare failed'})
            return 'local-provider-fixture'
        hub.sender=send
        other=hub.save_target(TargetInput(name='已成功渠道',kind='webhook',config={'url':'https://example.test/success'}))['id']
        wx=hub.save_target(TargetInput(name='微信会话恢复测试',kind='weixin'))['id']
        binding=Bindings(hub);binding._save(hub.target(wx),{'bot_token':'fixture-token','address':'fixture-owner','context_token':'old-context'})
        with hub.store.connect(True) as conn:conn.execute('UPDATE targets SET verified=1')
        mid=hub.submit(MessageInput(title='微信自动续发验收',body='隔离环境模拟，不发送外部通知',target_ids=[other,wx],request_id='browser-context'))['id']
        hub.tick();hub.tick()
        hub.store.set_setting('heartbeat',time.time())
        listener=socket.socket();listener.bind(('127.0.0.1',0));base=f'http://127.0.0.1:{listener.getsockname()[1]}'
        server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False));thread=threading.Thread(target=lambda:server.run(sockets=[listener]),daemon=True);thread.start()
        try:
            until=time.monotonic()+10
            while not server.started:assert time.monotonic()<until;time.sleep(.05)
            with sync_playwright() as p:
                browser=p.chromium.launch(executable_path=args.browser,headless=True)
                page=browser.new_page(viewport={'width':1366,'height':900});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(base+'/#records');page.get_by_label('管理员密码',exact=True).fill(password);page.get_by_role('button',name='登录工作台',exact=True).click()
                expect(page.locator('main')).to_contain_text('等待微信会话')
                page.locator(f'button[data-action="message"][data-id="{mid}"]').click()
                expect(page.locator('#dialog')).to_contain_text('无需重新绑定或手动重发')
                expect(page.get_by_role('button',name='重新发送此目标',exact=True)).to_have_count(0)
                expect(page.get_by_role('button',name='取消待发送任务',exact=True)).to_be_visible()
                page.screenshot(path=str(out/'waiting-context-desktop.png'))
                page.set_viewport_size({'width':390,'height':844})
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                page.screenshot(path=str(out/'waiting-context-mobile.png'))
                binding._save(hub.target(wx),{'context_token':'fresh-context'});hub.tick();hub.tick()
                page.get_by_role('button',name='刷新状态',exact=True).click()
                expect(page.locator('#dialog .badge').filter(has_text='渠道已接受')).to_have_count(2)
                expect(page.get_by_role('button',name='取消待发送任务',exact=True)).to_have_count(0)
                assert len(calls)==3 and sum(kind=='webhook' for kind,did in calls)==1
                page.set_viewport_size({'width':1366,'height':900});page.screenshot(path=str(out/'context-resumed.png'))
                page.get_by_role('button',name='关闭',exact=True).click()
                binding._save(hub.target(wx),{'context_token':'old-context'})
                cancelled=hub.submit(MessageInput(title='取消微信等待验收',body='不发送外部通知',target_ids=[wx],request_id='cancel-wait'))['id'];hub.tick()
                page.reload();page.locator(f'button[data-action="message"][data-id="{cancelled}"]').click()
                page.get_by_role('button',name='取消待发送任务',exact=True).click()
                expect(page.locator('#dialog .badge')).to_have_text('已取消')
                binding._save(hub.target(wx),{'context_token':'next-fresh-context'});hub.tick()
                assert len(calls)==4
                page.screenshot(path=str(out/'context-wait-cancelled.png'))
                assert not errors;browser.close()
        finally:server.should_exit=True;thread.join(10)
    result={'passed':True,'waiting_status_desktop_mobile':True,'no_manual_retry_while_waiting':True,'fresh_context_auto_resume':True,'success_not_resent':True,'cancelled_not_resumed':True,'external_notifications':0,'browser_errors':errors}
    (out/'browser-results.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--browser',required=True);parser.add_argument('--work-dir',required=True);parser.add_argument('--artifacts',required=True);run(parser.parse_args())
