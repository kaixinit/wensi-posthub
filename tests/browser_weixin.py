"""Local Weixin protocol simulation: no real QR account or external messages."""
import argparse
import json
from pathlib import Path
import re
import socket
import tempfile
import threading
import time
from unittest.mock import patch

import httpx
import uvicorn
from playwright.sync_api import sync_playwright, expect
from posthub.storage import initialize
from posthub.web import create_app


def run(args):
    out=Path(args.artifacts);out.mkdir(parents=True,exist_ok=True)
    control={'generation':0,'owner':False};sent=[];errors=[]
    def protocol(request):
        path=request.url.path
        if path.endswith('get_bot_qrcode'):
            assert request.method=='POST';control['generation']+=1
            return httpx.Response(200,json={'qrcode':'qr-'+str(control['generation']),'qrcode_img_content':'https://example.test/local-qr'})
        if path.endswith('get_qrcode_status'):
            if control['generation']==1:return httpx.Response(200,json={'status':'expired'})
            if request.url.params.get('verify_code')!='123456':return httpx.Response(200,json={'status':'need_verifycode'})
            return httpx.Response(200,json={'status':'confirmed','bot_token':'mock-private-token','ilink_user_id':'test-owner'})
        messages=[{'from_user_id':'other-person','message_type':1,'context_token':'wrong-context'}]
        if control['owner']:messages.append({'from_user_id':'test-owner','message_type':1,'context_token':'owner-context'})
        return httpx.Response(200,json={'ret':0,'get_updates_buf':'cursor','msgs':messages})
    def send(kind,config,message,did):
        assert config['address']=='test-owner' and config['context_token']=='owner-context'
        sent.append(message);return 'local-weixin-receipt'
    listener=socket.socket();listener.bind(('127.0.0.1',0));base=f'http://127.0.0.1:{listener.getsockname()[1]}'
    with tempfile.TemporaryDirectory(dir=args.work_dir) as directory,patch('posthub.bindings.client',lambda *a,**k:httpx.Client(transport=httpx.MockTransport(protocol))):
        initialize(Path(directory),'weixin-browser-test-password')
        app=create_app(directory,run_worker=False,sender=send)
        server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False))
        thread=threading.Thread(target=lambda:server.run(sockets=[listener]),daemon=True);thread.start()
        try:
            deadline=time.monotonic()+10
            while not server.started:
                assert time.monotonic()<deadline;time.sleep(.03)
            with sync_playwright() as p:
                browser=p.chromium.launch(executable_path=args.browser,headless=True)
                page=browser.new_page(viewport={'width':1440,'height':1000})
                page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(base);page.get_by_label('管理员密码').fill('weixin-browser-test-password');page.get_by_role('button',name='登录工作台',exact=True).click()
                page.get_by_role('button',name='扫码绑定',exact=True).click()
                expect(page.get_by_role('heading',name='微信渠道配置',exact=True)).to_be_visible()
                page.get_by_label('渠道名称',exact=True).fill('微信模拟收件人')
                page.get_by_role('button',name='保存并进入扫码',exact=True).click()
                expect(page.locator('#choice-dialog')).not_to_be_visible()
                expect(page.get_by_role('heading',name='微信渠道配置 · 微信模拟收件人',exact=True)).to_be_visible()
                expect(page.locator('#wx-auth-status')).to_contain_text('二维码已过期')
                page.locator('#dialog').get_by_role('button',name='扫码绑定',exact=True).click()
                expect(page.get_by_label('手机授权验证码',exact=True)).to_be_visible()
                expect(page.get_by_role('img',name='微信绑定二维码')).to_be_visible()
                page.locator('#dialog').screenshot(path=str(out/'01-qr-and-phone-code.png'))
                page.get_by_label('手机授权验证码',exact=True).fill('123456')
                page.get_by_role('button',name='提交手机授权验证码',exact=True).click()
                expect(page.locator('#wx-session-status')).to_contain_text('等待本人微信会话')
                expect(page.get_by_role('button',name='发送收件验证码',exact=True)).to_be_disabled()
                control['owner']=True
                page.get_by_role('button',name='刷新微信会话',exact=True).click()
                expect(page.get_by_role('button',name='发送收件验证码',exact=True)).to_be_enabled()
                page.get_by_role('button',name='发送收件验证码',exact=True).click()
                expect(page.locator('#verify-result')).to_contain_text('已加入发送队列')
                app.state.hub.tick();assert len(sent)==1
                expect(page.locator('#wx-test-status')).to_contain_text('微信接口已接受测试消息',timeout=10000)
                assert '微信 收件目标：微信模拟收件人' in sent[0]['body']
                code=re.search(r'验证码：(\d{6})',sent[0]['body'])[1]
                page.get_by_label('收件验证码',exact=True).fill('999999' if code!='999999' else '888888')
                page.get_by_role('button',name='完成验证',exact=True).click()
                expect(page.locator('#toast')).to_contain_text('验证码不正确')
                page.get_by_label('收件验证码',exact=True).fill(code)
                page.get_by_role('button',name='完成验证',exact=True).click()
                expect(page.locator('#dialog')).not_to_be_visible()
                page.get_by_role('button',name='管理',exact=True).click()
                expect(page.get_by_role('heading',name='已完成验证',exact=True)).to_be_visible()
                page.locator('#dialog').screenshot(path=str(out/'02-verified.png'))
                page.get_by_role('button',name='重新扫码绑定',exact=True).click()
                page.locator('#choice-dialog').get_by_role('button',name='取消',exact=True).click()
                expect(page.get_by_role('heading',name='已完成验证',exact=True)).to_be_visible()
                page.set_viewport_size({'width':390,'height':844})
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                page.locator('#dialog').screenshot(path=str(out/'03-mobile.png'))
                page.get_by_role('button',name='关闭对话框',exact=True).click()
                assert page.evaluate('wxFlow===null')
                assert not errors,errors
                browser.close()
        finally:server.should_exit=True;thread.join(10)
    result={'passed':True,'page_errors':errors,'local_sender_calls':len(sent),'real_weixin_messages_sent':0,'checks':['explicit scan entry','expired QR refresh','automatic status polling','phone authorization PIN','own-recipient context isolation','test receipt without auto-verification','wrong then correct receipt code','completed state','cancel reauthorization preserves binding','mobile layout','poll stopped on close']}
    (out/'results.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--browser',required=True);parser.add_argument('--artifacts',required=True);parser.add_argument('--work-dir',required=True)
    run(parser.parse_args())
