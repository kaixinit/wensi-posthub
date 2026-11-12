"""Delivery details reflect automatic retry and do not offer a duplicate resend."""
import argparse
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import uvicorn
from playwright.sync_api import sync_playwright,expect
from posthub.adapters import DeliveryError
from posthub.models import MessageInput,TargetInput
from posthub.storage import initialize
from posthub.web import create_app


def run(args):
    out=Path(args.artifacts);out.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=args.work_dir) as directory:
        password='browser-retry-only-password';initialize(Path(directory),password)
        app=create_app(directory,run_worker=False);hub=app.state.hub
        down=[True];calls=[];now=[time.time()];hub.clock=lambda:now[0]
        def send(kind,config,message,did):
            calls.append(config['url'])
            if config['url'].endswith('offline') and down[0]:raise DeliveryError('测试代理未连接','retry')
            return 'local-fixture'
        hub.sender=send;ids=[]
        for name,url in [('已成功渠道','https://example.test/success'),('自动补发渠道','https://example.test/offline')]:
            tid=hub.save_target(TargetInput(name=name,kind='webhook',config={'url':url}))['id'];ids.append(tid)
            with hub.store.connect(True) as conn:conn.execute('UPDATE targets SET verified=1 WHERE id=?',(tid,))
        mid=hub.submit(MessageInput(title='UI 自动补发验证',body='隔离测试，不向外部发送',target_ids=ids,request_id='ui-retry'))['id']
        hub.tick();hub.tick()
        listener=socket.socket();listener.bind(('127.0.0.1',0));base=f'http://127.0.0.1:{listener.getsockname()[1]}'
        server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False));thread=threading.Thread(target=lambda:server.run(sockets=[listener]),daemon=True);thread.start()
        try:
            until=time.monotonic()+10
            while not server.started:assert time.monotonic()<until;time.sleep(.05)
            with sync_playwright() as p:
                browser=p.chromium.launch(executable_path=args.browser,headless=True)
                page=browser.new_page(viewport={'width':1366,'height':900});errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(base+'/#records');page.get_by_label('管理员密码',exact=True).fill(password);page.get_by_role('button',name='登录工作台',exact=True).click()
                page.locator(f'button[data-action="message"][data-id="{mid}"]').click()
                expect(page.locator('#dialog')).to_contain_text('正在自动重试 · 下次尝试：')
                expect(page.get_by_role('button',name='重新发送此目标',exact=True)).to_have_count(0)
                expect(page.locator('#dialog .delivery-card')).to_have_count(2)
                page.screenshot(path=str(out/'automatic-retry.png'))
                down[0]=False;now[0]+=60;hub.tick()
                page.get_by_role('button',name='刷新状态',exact=True).click()
                expect(page.locator('#dialog')).not_to_contain_text('正在自动重试')
                expect(page.locator('#dialog .badge').filter(has_text='渠道已接受')).to_have_count(2)
                assert calls.count('https://example.test/success')==1
                page.screenshot(path=str(out/'automatic-retry-completed.png'))
                assert not errors;browser.close()
        finally:server.should_exit=True;thread.join(10)
    result={'passed':True,'next_retry_visible':True,'no_manual_retry_button':True,'recovery_refresh':'accepted','successful_target_sent_once':True,'external_notifications':0}
    (out/'browser-results.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--browser',required=True);parser.add_argument('--work-dir',required=True);parser.add_argument('--artifacts',required=True);run(parser.parse_args())
