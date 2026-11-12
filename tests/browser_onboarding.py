"""Real Chrome + local TCP webhook; no external recipients or live data."""
import argparse,json,re,socket,tempfile,threading,time
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import httpx,uvicorn
from playwright.sync_api import sync_playwright,expect
from posthub.storage import initialize
from posthub.web import create_app
from posthub.models import MessageInput,AppInput

def run(args):
    out=Path(args.artifacts);out.mkdir(parents=True,exist_ok=True);received=[];errors=[]
    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))));self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(b'{"accepted":true}')
        def log_message(self,*a):pass
    rx=ThreadingHTTPServer(('127.0.0.1',0),Receiver);threading.Thread(target=rx.serve_forever,daemon=True).start()
    with tempfile.TemporaryDirectory(dir=args.work_dir) as data:
        initialize(Path(data),'isolated-onboarding-password');app=create_app(data,run_worker=False);hub=app.state.hub
        listener=socket.socket();listener.bind(('127.0.0.1',0));base=f'http://127.0.0.1:{listener.getsockname()[1]}'
        server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False));thread=threading.Thread(target=lambda:server.run(sockets=[listener]),daemon=True);thread.start()
        def drain():
            while hub.tick():pass
            hub.store.set_setting('heartbeat',time.time())
        try:
            while not server.started:time.sleep(.05)
            drain()
            with sync_playwright() as p,httpx.Client(trust_env=False,timeout=10) as http:
                browser=p.chromium.launch(executable_path=args.browser,headless=True);page=browser.new_page(viewport={'width':1366,'height':960});page.on('pageerror',lambda e:errors.append(str(e)))
                def shot(name):
                    page.evaluate('document.fonts.ready');assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.screenshot(path=str(out/(name+'.png')),full_page=True,mask=[page.locator('#setup-url')])
                page.goto(base);page.get_by_label('管理员密码').fill('isolated-onboarding-password');page.get_by_role('button',name='登录工作台').click();expect(page.get_by_role('heading',name='接入向导',exact=True)).to_be_visible();shot('01-start')
                page.get_by_label('上游名称',exact=True).fill('首次接入演示');page.get_by_role('button',name='创建接口，进入下游配置').click();expect(page.get_by_role('heading',name='2. 配置 首次接入演示 的下游渠道')).to_be_visible()
                aid=hub.state()['apps'][0]['id'];page.get_by_role('button',name='配置此上游的下游渠道').click();page.get_by_role('button',name='＋ 添加渠道').click();page.locator('#dialog [data-kind="webhook"]').click();page.get_by_label('渠道名称',exact=True).fill('本机 HTTP 收件');page.get_by_label('接收端 HTTP(S) 地址',exact=True).fill(f'http://127.0.0.1:{rx.server_port}/inbox');page.get_by_role('button',name='继续绑定',exact=True).click();page.get_by_role('button',name='保存草稿并继续绑定').click();page.get_by_role('button',name='发送收件验证码',exact=True).click();expect(page.get_by_role('button',name='查看发送结果')).to_be_visible();drain()
                code=re.search(r'验证码：(\d{6})',received[-1]['body'])[1];page.get_by_label('收件验证码',exact=True).fill(code);page.get_by_role('button',name='完成验证',exact=True).click();expect(page.locator('#app-form')).to_be_visible();page.get_by_role('button',name='保存上游配置').click();expect(page.locator('#dialog')).not_to_be_visible();shot('02-downstream-saved')
                tid=hub.state()['targets'][0]['id'];other=hub.create_app(AppInput(name='另一上游'))
                page.get_by_role('button',name='下一步：测试收件').click()
                def lose_response(route):
                    response=route.fetch();assert response.status==202;route.abort('failed')
                page.route('**/api/admin/apps/*/setup-test',lose_response,times=1)
                page.get_by_role('button',name='发送测试消息',exact=True).click();expect(page.locator('#toast')).to_have_class('error');expect(page.get_by_role('button',name='发送测试消息',exact=True)).to_be_enabled()
                page.get_by_role('button',name='发送测试消息',exact=True).click();expect(page.locator('#dialog')).to_be_visible();drain();page.get_by_role('button',name='刷新状态',exact=True).click();expect(page.locator('.recovery-advice')).to_contain_text('接口返回成功回执');assert len(received)==2
                mid=next(m['id'] for m in hub.state()['messages'] if m['purpose']=='setup_test');assert hub.state()['apps'][0]['received_count']==0
                page.get_by_role('button',name='我已收到，登记确认',exact=True).click();page.get_by_role('button',name='已核实，登记收到').click();expect(page.locator('#dialog')).to_contain_text('已登记实际收到');assert len(received)==2
                page.get_by_role('button',name='关闭',exact=True).click();page.get_by_role('button',name='刷新进度').click();expect(page.locator('.setup-result')).to_contain_text('当前渠道均已确认收件');shot('03-tested-and-confirmed')
                page.get_by_role('button',name='下一步：配置上游').click();expect(page.locator('#setup-url')).to_be_visible();url=page.locator('#setup-url').input_value();body=json.loads(page.locator('#setup-json').inner_text());expect(page.locator('.setup-result')).to_contain_text('等待上游首次推送')
                r=http.post(url,json=body);r.raise_for_status();assert r.status_code==202;again=http.post(url,json=body);assert again.json()['deduplicated'];drain();assert len(received)==3
                page.get_by_role('button',name='检查接入结果').click();expect(page.locator('.setup-success')).to_be_visible();shot('04-upstream-connected');assert next(a for a in hub.state()['apps'] if a['id']==other['id'])['received_count']==0
                page.reload();expect(page.locator('#setup-url')).to_have_count(0);expect(page.locator('.setup-success')).to_be_visible();assert not page.evaluate('JSON.stringify(localStorage)+JSON.stringify(sessionStorage)').find('ph_')>=0
                page.set_viewport_size({'width':390,'height':844});shot('05-mobile');page.set_viewport_size({'width':1366,'height':960})
                # Controlled state fixtures exercise guidance without external effects.
                failure=hub.submit(MessageInput(title='故障处理演示',body='本机模拟',request_id='fault-fixture',target_ids=[tid]));did=hub.message(failure['id'])['deliveries'][0]['id']
                for status,detail,expected in [('unknown','发送后未取得回执','先核实实际收件'),('retry','无法建立连接','等待自动重试'),('failed','授权已过期（401）','检查账号授权'),('expired','超过截止时间','已超过投递截止时间')]:
                    with hub.store.connect(True) as c:c.execute('UPDATE deliveries SET state=?,last_error=?,next_at=? WHERE id=?',(status,detail,time.time()+60,did))
                    page.evaluate('(id)=>showMessage(id)',failure['id']);expect(page.locator('.recovery-advice')).to_contain_text(expected);shot('06-'+status)
                    if status=='unknown':
                        page.get_by_role('button',name='重新发送此目标').click();expect(page.locator('#dialog')).to_contain_text('重新发送可能造成重复通知');page.get_by_role('button',name='关闭对话框').click();assert len(received)==3
                    if status=='retry':
                        # Stub only the diagnostic payload: browser verifies return path; backend tests cover diagnostic transport.
                        page.route('**/api/admin/network/test',lambda route:route.fulfill(json={'status':'reachable','id':tid,'endpoint':'webhook','route':'直接连接','latency_ms':1,'tested_at':time.time(),'detail':'本机检测响应','hint':'网络可达不等于收件成功'}))
                        page.get_by_role('button',name='检测此渠道通信').click();expect(page.locator('#recovery-network-result')).to_contain_text('网络可达');page.get_by_role('button',name='返回投递详情').click();expect(page.locator('.recovery-advice')).to_contain_text('等待自动重试');page.unroute('**/api/admin/network/test')
                    if page.locator('#dialog').is_visible():page.get_by_role('button',name='关闭对话框').click()
                # Keyboard navigation and no changes from background refresh while typing.
                page.goto(base+'/#setup');page.get_by_label('上游名称',exact=True).fill('未保存名称');page.evaluate('refresh(false)');expect(page.get_by_label('上游名称',exact=True)).to_have_value('未保存名称');page.get_by_label('上游名称',exact=True).focus();page.keyboard.press('Tab');expect(page.get_by_role('button',name='创建接口，进入下游配置')).to_be_focused()
                assert not errors,errors;browser.close()
            (out/'results.json').write_text(json.dumps({'status':'passed','real_local_http_receipts':len(received),'external_notifications':0,'checks':['new user landing','channel verification and return/save','scoped platform test','explicit actual receipt confirmation','API ingress and same-id deduplication','separate upstream counts','second upstream isolation','refresh resumes without key storage','mobile overflow','unknown/retry/auth/expired guidance','diagnostic return','keyboard focus','no JavaScript errors'],'diagnostic_response':'stubbed in browser; transport tested by backend suite'},ensure_ascii=False,indent=2),encoding='utf-8');print('Onboarding browser and local HTTP flow passed.')
        finally:server.should_exit=True;thread.join(5);rx.shutdown();rx.server_close()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--artifacts',required=True);parser.add_argument('--work-dir',required=True);parser.add_argument('--browser',default='C:/Program Files/Google/Chrome/Application/chrome.exe');run(parser.parse_args())
