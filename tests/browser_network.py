"""Browser acceptance on a disposable server and loopback proxy only."""
import argparse
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import uvicorn
from playwright.sync_api import sync_playwright, expect

from posthub.models import AppInput
from posthub.network import PROBES
from posthub.storage import initialize
from posthub.web import create_app
from tests.conftest import make_target
from tests.test_qq_gateway import target as qq_target


def run(args):
    out=Path(args.artifacts);out.mkdir(parents=True,exist_ok=True)
    work=Path(args.work_dir);work.mkdir(parents=True,exist_ok=True)
    requests=[]
    class Proxy(BaseHTTPRequestHandler):
        def do_HEAD(self):
            requests.append({'method':self.command,'path':self.path,'authenticated':bool(self.headers.get('Proxy-Authorization'))})
            self.send_response(404);self.end_headers()
        def do_POST(self):
            raise AssertionError('Diagnostic unexpectedly sent a notification')
        def log_message(self,*args):pass
    proxy=ThreadingHTTPServer(('127.0.0.1',0),Proxy)
    pt=threading.Thread(target=proxy.serve_forever,daemon=True);pt.start()
    old_probe=PROBES['qq'];PROBES['qq']=f'http://127.0.0.1:{proxy.server_port}/'
    errors=[]
    with tempfile.TemporaryDirectory(dir=work) as data:
        initialize(Path(data),'browser-network-test-password')
        captures=[]
        app=create_app(data,run_worker=False,sender=lambda *a:captures.append(a) or 'local')
        hub=app.state.hub;hub.calls=captures;hub.store.set_setting('heartbeat',time.time())
        tid=make_target(hub,'本机测试 HTTP',config={'url':f'http://127.0.0.1:{proxy.server_port}/private-hook'})
        q1=qq_target(hub);q2=qq_target(hub)
        before=hub.target(tid)
        upstream=hub.create_app(AppInput(name='独立上游',target_ids=[tid]))
        listener=socket.socket();listener.bind(('127.0.0.1',0));base=f'http://127.0.0.1:{listener.getsockname()[1]}'
        server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False))
        st=threading.Thread(target=lambda:server.run(sockets=[listener]),daemon=True);st.start()
        try:
            until=time.monotonic()+10
            while not server.started:
                assert time.monotonic()<until
                time.sleep(.05)
            with sync_playwright() as p:
                browser=p.chromium.launch(executable_path=args.browser,headless=True)
                context=browser.new_context(viewport={'width':1440,'height':1000})
                page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                def shot(name):
                    page.evaluate('document.fonts.ready')
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                    page.screenshot(path=str(out/(name+'.png')),full_page=True)
                def confirm():page.locator('#choice-dialog').get_by_role('button',name='确认保存',exact=True).click()
                page.goto(base+'/#network')
                page.get_by_label('管理员密码').fill('browser-network-test-password')
                page.get_by_role('button',name='登录工作台').click()
                expect(page.get_by_role('heading',name='网络通信',exact=True)).to_be_visible()
                expect(page.get_by_role('heading',name='下游通信检测',exact=True)).to_be_visible()
                expect(page.locator('.net-state-guide dt')).to_have_text(['网络可达','账号已授权','渠道已接受','收件人已确认'])
                expect(page.locator('#net-test-form select[name=proxy_id] option:checked')).to_have_text('默认：直接连接')
                page.get_by_role('button',name='连接方式',exact=True).click()
                expect(page.locator('#net-proxy-settings')).not_to_have_attribute('open','')
                page.locator('#net-proxy-settings summary').click()
                page.get_by_role('button',name='添加代理',exact=True).click()
                f=page.locator('#net-proxy-form')
                f.get_by_label('代理名称',exact=True).fill('本机 Clash 测试')
                f.get_by_label('代理主机',exact=True).fill('127.0.0.1')
                f.get_by_label('代理端口',exact=True).fill(str(proxy.server_port))
                f.locator('.net-proxy-auth summary').click()
                f.get_by_label('代理用户名（可选）',exact=True).fill('user')
                f.get_by_label('代理密码（可选）',exact=True).fill('local-secret')
                f.get_by_role('button',name='检测当前配置').click()
                expect(page.locator('#net-preview-result')).to_contain_text('网络可达')
                assert not hub.network.state()['proxies']
                shot('proxy-before-save')
                f.get_by_role('button',name='保存代理',exact=True).click()
                expect(page.locator('#dialog')).not_to_be_visible()
                pid=hub.network.state()['proxies'][0]['id']
                page.locator('#net-default-form select').select_option('proxy:'+pid)
                page.get_by_role('button',name='保存默认连接方式').click();confirm()
                expect(page.locator('#net-default-form select')).to_have_value('proxy:'+pid)
                expect(page.locator('tr').filter(has_text='本机 Clash 测试').get_by_text('默认连接方式',exact=True)).to_be_visible()
                page.locator(f'[data-action="net-proxy-edit"][data-id="{pid}"]').click()
                expect(page.get_by_label('代理密码（可选）',exact=True)).to_have_value('')
                page.get_by_label('代理名称',exact=True).fill('办公室代理')
                page.get_by_role('button',name='保存代理',exact=True).click();confirm()
                expect(page.locator('#dialog')).not_to_be_visible()
                assert hub.effective_config(hub.target(tid))['proxy'].startswith('http://user:local-secret@')
                # A per-channel override leaves existing recipient verification intact.
                page.locator(f'[data-action="net-route"][data-id="{tid}"]').click()
                page.locator('#net-route-form select').select_option('system')
                page.get_by_role('button',name='保存连接方式',exact=True).click()
                expect(page.locator('#dialog')).not_to_be_visible()
                assert hub.target(tid)['verified'] and hub.target(tid)['revision']==before['revision']
                # Both QQ channels must use the same explicit bot route.
                page.locator(f'[data-action="net-route"][data-id="{q1}"]').click()
                page.locator('#net-route-form select').select_option('proxy:'+pid)
                expect(page.locator('#dialog')).to_contain_text('2 个渠道')
                page.get_by_role('button',name='保存连接方式',exact=True).click();confirm()
                expect(page.locator('#dialog')).not_to_be_visible()
                assert hub.network.state()['routes'][q1]['proxy_id']==hub.network.state()['routes'][q2]['proxy_id']==pid
                page.locator(f'[data-action="net-proxy-delete"][data-id="{pid}"]').click()
                expect(page.locator('#toast')).to_contain_text('仍被')
                assert len(hub.network.state()['proxies'])==1
                shot('network-outlets-desktop')
                page.get_by_role('button',name='通信检测',exact=True).click()
                expect(page.locator('#net-test-form select[name=proxy_id] option:checked')).to_have_text('默认：使用代理 · 办公室代理')
                page.get_by_role('button',name='开始检测',exact=True).click()
                expect(page.locator('#net-inline-result')).to_contain_text('网络可达')
                page.locator(f'[data-action="net-test-target"][data-id="{tid}"]').click()
                expect(page.locator('#net-dialog-result')).to_contain_text('网络可达')
                page.get_by_role('button',name='关闭对话框',exact=True).click()
                shot('network-diagnostics-desktop')
                page.get_by_role('button',name='平台访问地址',exact=True).click()
                expect(page.locator('#settings-form')).to_contain_text('不会验证公网访问是否正常')
                shot('platform-address-desktop')
                page.get_by_label('上游与收件人可访问的平台地址').fill('https://notify.example.test')
                page.get_by_role('button',name='保存访问设置').click()
                expect(page.locator('#toast')).to_have_text('设置已保存')
                expect(page.get_by_label('上游与收件人可访问的平台地址')).to_have_value('https://notify.example.test')
                assert hub.network.state()['default_proxy_id']==pid
                # Return, remove references, and exercise deletion end to end.
                page.get_by_role('button',name='连接方式',exact=True).click()
                page.locator('#net-default-form select').select_option('system')
                page.get_by_role('button',name='保存默认连接方式').click();confirm()
                expect(page.locator('#net-default-form select')).to_have_value('system')
                expect(page.locator('#net-default-form')).to_have_attribute('data-previous-proxy-id','')
                page.locator(f'[data-action="net-route"][data-id="{q2}"]').click()
                page.locator('#net-route-form select').select_option('system')
                page.get_by_role('button',name='保存连接方式',exact=True).click();confirm()
                expect(page.locator('#dialog')).not_to_be_visible()
                expect(page.locator('#net-proxy-settings tr').filter(has_text='办公室代理')).to_contain_text('暂未引用')
                assert not hub.network.state()['proxies'][0]['is_default']
                assert not hub.network.state()['proxies'][0]['target_ids'], hub.network.state()['routes']
                page.locator(f'[data-action="net-proxy-delete"][data-id="{pid}"]').click()
                page.locator('#choice-dialog').get_by_role('button',name='删除代理',exact=True).click()
                expect(page.get_by_text('未配置代理，可直接连接。需要代理时再添加。',exact=True)).to_be_visible()
                assert not hub.network.state()['proxies']
                page.set_viewport_size({'width':390,'height':844});shot('network-outlets-mobile')
                page.get_by_role('button',name='添加代理',exact=True).click();shot('proxy-form-mobile')
                page.get_by_role('button',name='关闭对话框',exact=True).click()
                page.get_by_role('button',name='通信检测',exact=True).click();shot('network-diagnostics-mobile')
                assert not errors,errors
                assert hub.state()['apps'][0]['target_ids']==[tid]
                assert all(r['method']=='HEAD' and 'private-hook' not in r['path'] for r in requests)
                assert any(r['authenticated'] for r in requests)
                assert not captures
                browser.close()
        finally:
            server.should_exit=True;st.join(10);PROBES['qq']=old_probe
            proxy.shutdown();pt.join(5);proxy.server_close()
    result={'status':'passed','proxy_crud':'passed','password_kept_on_edit':True,'shared_delete_protected':True,'qq_outlet_consistent':True,'recipient_verification_preserved':True,'desktop_mobile':'passed','real_notifications_sent':0,'local_head_requests':len(requests),'browser_errors':errors,'communication_default_tab':True,'optional_proxy_collapsed_initially':True,'four_status_meanings':True,'direct_and_proxy_labels_match_routes':True}
    (out/'results.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--browser',required=True);parser.add_argument('--work-dir',required=True);parser.add_argument('--artifacts',required=True)
    run(parser.parse_args())
