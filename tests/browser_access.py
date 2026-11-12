"""Local HTTP/HTTPS reverse-proxy and modal UI acceptance; isolated data only."""
import argparse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
from pathlib import Path
import socket
import ssl
import tempfile
import threading
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import httpx
from playwright.sync_api import sync_playwright, expect
import uvicorn

from posthub.storage import initialize
from posthub.web import create_app


def run(args):
    out=Path(args.artifacts);out.mkdir(parents=True,exist_ok=True)
    work=Path(args.work_dir);work.mkdir(parents=True,exist_ok=True)
    errors=[];checks=[]
    with tempfile.TemporaryDirectory(dir=work) as directory:
        folder=Path(directory);initialize(folder,'local-proxy-test-password')
        service=create_app(folder,run_worker=False)
        listener=socket.socket();listener.bind(('127.0.0.1',0))
        internal='http://127.0.0.1:'+str(listener.getsockname()[1])
        server=uvicorn.Server(uvicorn.Config(service,log_level='error',access_log=False,proxy_headers=False))
        thread=threading.Thread(target=lambda:server.run(sockets=[listener]),daemon=True);thread.start()
        deadline=time.monotonic()+10
        while not server.started:
            assert time.monotonic()<deadline
            time.sleep(.05)
        forward=httpx.Client(base_url=internal,trust_env=False,timeout=15)
        class Proxy(BaseHTTPRequestHandler):
            def forward_request(self):
                body=self.rfile.read(int(self.headers.get('Content-Length','0')))
                headers={k:v for k,v in self.headers.items() if k.lower() not in {'host','content-length','connection','accept-encoding'}}
                # Deliberately rewrite Host and omit forwarded headers. The UI
                # must obtain the public origin from the browser, not the ASGI socket.
                request=forward.build_request(self.command,self.path,headers=headers,content=body)
                # A proxy must never inject cookies retained by its HTTP client.
                if not self.headers.get('Cookie'):
                    request.headers.pop('Cookie',None)
                response=forward.send(request)
                self.send_response(response.status_code)
                for key,value in response.headers.multi_items():
                    if key.lower() not in {'content-length','transfer-encoding','connection','content-encoding','server','date'}:
                        self.send_header(key,value)
                self.send_header('Content-Length',str(len(response.content)));self.end_headers()
                self.wfile.write(response.content)
            do_GET=do_POST=do_PUT=do_DELETE=forward_request
            def log_message(self,*args):pass
        plain=ThreadingHTTPServer(('127.0.0.1',0),Proxy)
        secure=ThreadingHTTPServer(('127.0.0.1',0),Proxy)
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'localhost')])
        now=datetime.now(timezone.utc)
        cert=x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1)).not_valid_after(now+timedelta(days=1)).add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False).sign(key,hashes.SHA256())
        (folder/'cert.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (folder/'key.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.load_cert_chain(folder/'cert.pem',folder/'key.pem')
        secure.socket=tls.wrap_socket(secure.socket,server_side=True)
        for proxy in (plain,secure):threading.Thread(target=proxy.serve_forever,daemon=True).start()
        try:
            with sync_playwright() as p, httpx.Client(trust_env=False,verify=False,timeout=15) as http:
                browser=p.chromium.launch(executable_path=args.browser,headless=True)
                for scheme,proxy in [('http',plain),('https',secure)]:
                    origin=scheme+'://localhost:'+str(proxy.server_port)
                    context=browser.new_context(ignore_https_errors=True,viewport={'width':1440,'height':1000})
                    context.grant_permissions(['clipboard-read','clipboard-write'],origin=origin)
                    page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                    def screenshot(name):
                        page.evaluate('document.fonts.ready')
                        masks=[page.locator('#'+id) for id in ['new-hook-url','new-api-key','existing-app-key','saved-hook-url'] if page.locator('#'+id).count()]
                        page.screenshot(path=str(out/(scheme+'-'+name+'.png')),mask=masks,mask_color='#cbd5e1')
                        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    def toast_visible():
                        expect(page.locator('#toast')).to_be_visible()
                        assert page.locator('#toast').evaluate("e => e.matches(':popover-open') && document.elementFromPoint(e.getBoundingClientRect().x+e.getBoundingClientRect().width/2,e.getBoundingClientRect().y+e.getBoundingClientRect().height/2)===e")
                    page.goto(origin)
                    page.get_by_label('管理员密码').fill('local-proxy-test-password')
                    page.get_by_role('button',name='登录工作台').click()
                    expect(page.get_by_role('heading',name='下游渠道',exact=True)).to_be_visible()
                    page.get_by_role('button',name='扫码绑定',exact=True).click()
                    page.get_by_label('渠道名称',exact=True).fill('不应丢失的输入')
                    page.mouse.click(10,10)
                    expect(page.locator('#dialog')).to_be_visible()
                    expect(page.get_by_label('渠道名称',exact=True)).to_have_value('不应丢失的输入')
                    page.get_by_role('button',name='继续绑定',exact=True).click()
                    expect(page.locator('#choice-dialog')).to_be_visible()
                    page.mouse.click(10,10)
                    expect(page.locator('#choice-dialog')).to_be_visible()
                    page.get_by_role('button',name='返回修改',exact=True).click()
                    page.get_by_role('button',name='关闭对话框',exact=True).click()
                    expect(page.locator('#dialog')).not_to_be_visible()
                    page.get_by_role('link',name='上游分发',exact=True).click()
                    page.get_by_role('button',name='创建上游接口',exact=True).click()
                    page.get_by_label('上游名称',exact=True).fill(scheme+' 代理测试')
                    page.mouse.click(10,10)
                    expect(page.get_by_label('上游名称',exact=True)).to_have_value(scheme+' 代理测试')
                    page.get_by_role('button',name='创建接口并生成地址',exact=True).click()
                    expect(page.locator('#new-api-key')).to_be_visible()
                    token=page.locator('#new-api-key').inner_text();url=origin+'/api/v1/hooks/'+token
                    assert page.locator('#new-hook-url').input_value()==url
                    page.mouse.click(10,10)
                    expect(page.locator('#dialog')).to_be_visible()
                    page.get_by_role('button',name='复制完整地址',exact=True).click();toast_visible()
                    assert page.evaluate('navigator.clipboard.readText()')==url
                    response=http.post(url,json={'body':'through '+scheme+' proxy','request_id':'proxy-intake'})
                    assert response.status_code==202 and response.json()['routing_state']=='awaiting_routing'
                    page.get_by_role('button',name='已保存，查看接入配置',exact=True).click()
                    expect(page.locator('#integration-base')).to_have_value(origin)
                    page.get_by_role('button',name='生成完整接入地址',exact=True).click()
                    expect(page.locator('#toast')).to_contain_text('请输入创建上游接口时保存的完整')
                    toast_visible();screenshot('error-above-dialog')
                    page.get_by_label('已保存的接入密钥',exact=True).fill(token)
                    page.get_by_role('button',name='生成完整接入地址',exact=True).click()
                    expect(page.locator('#saved-hook-url')).to_have_value(url)
                    toast_visible();screenshot('success-above-dialog')
                    page.get_by_role('button',name='复制完整地址',exact=True).click();toast_visible()
                    page.locator('#integration-base').fill('http://override.example.test:29101');page.locator('#integration-base').press('Tab')
                    expect(page.locator('#saved-hook-url')).to_have_value('')
                    expect(page.locator('#integration-endpoint')).to_have_value('http://override.example.test:29101/api/v1/messages')
                    page.get_by_role('button',name='使用当前网址',exact=True).click()
                    expect(page.locator('#integration-endpoint')).to_have_value(origin+'/api/v1/messages')
                    page.locator('#integration-base').fill('invalid address');page.locator('#integration-base').press('Tab')
                    expect(page.locator('#integration-endpoint')).to_have_value('')
                    page.get_by_role('button',name='使用当前网址',exact=True).click()
                    page.mouse.click(10,10)
                    expect(page.locator('#dialog')).to_be_visible()
                    page.get_by_role('button',name='重置密钥并生成地址',exact=True).click()
                    page.mouse.click(10,10)
                    expect(page.locator('#choice-dialog')).to_be_visible()
                    page.get_by_role('button',name='取消',exact=True).click()
                    page.set_viewport_size({'width':390,'height':844})
                    page.get_by_label('已保存的接入密钥',exact=True).fill('')
                    page.get_by_role('button',name='生成完整接入地址',exact=True).click()
                    toast_visible();screenshot('mobile-error-above-dialog')
                    page.keyboard.press('Escape')
                    expect(page.locator('#dialog')).not_to_be_visible()
                    page.set_viewport_size({'width':1440,'height':1000})
                    service.state.hub.store.set_setting('public_base_url','http://fixed.example.test:18087')
                    page.reload()
                    row=page.get_by_role('row').filter(has_text=scheme+' 代理测试')
                    row.get_by_role('button',name='接入配置',exact=True).click()
                    expect(page.locator('#integration-base')).to_have_value('http://fixed.example.test:18087')
                    page.get_by_role('button',name='使用当前网址',exact=True).click()
                    expect(page.locator('#integration-base')).to_have_value(origin)
                    assert service.state.hub.store.setting('public_base_url')=='http://fixed.example.test:18087'
                    screenshot('proxy-origin')
                    page.get_by_role('button',name='关闭对话框',exact=True).click()
                    service.state.hub.store.set_setting('public_base_url','')
                    checks.append({'protocol':scheme,'browser_origin_preserved':True,'external_port_differs_from_backend':proxy.server_port!=listener.getsockname()[1],'real_post_received':True,'backdrop_keeps_all_dialogs':True,'toast_hit_test_above_dialog':True,'mobile_toast_visible':True,'fixed_address_priority_and_explicit_override':True})
                    context.close()
                browser.close()
            assert not errors,errors
            (out/'results.json').write_text(json.dumps({'passed':True,'page_errors':errors,'checks':checks,'actual_frps_used':False,'proxy':'local HTTP and TLS reverse proxies with rewritten Host'},indent=2),encoding='utf-8')
            print('HTTP/HTTPS proxy, explicit close and top-layer toast checks passed.')
        finally:
            for proxy in (plain,secure):proxy.shutdown();proxy.server_close()
            forward.close();server.should_exit=True;thread.join(timeout=15);listener.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--browser',required=True);parser.add_argument('--work-dir',required=True);parser.add_argument('--artifacts',required=True)
    run(parser.parse_args())
