"""QQ UI acceptance with a local WebSocket and a fake outbound sender."""
import argparse
import json
from pathlib import Path
from queue import Empty, Queue
import re
import socket
import tempfile
import threading
import time
from unittest.mock import patch

import httpx
from playwright.sync_api import expect, sync_playwright
import uvicorn
from websockets.sync.server import serve

from posthub.adapters import DeliveryError
from posthub.qq_gateway import QQSession
from posthub.storage import initialize
from posthub.web import create_app


def wait_for(predicate):
    end=time.monotonic()+15
    while not predicate():
        assert time.monotonic()<end,'Local test timed out'
        time.sleep(.03)


def run(args):
    out=Path(args.artifacts);out.mkdir(parents=True,exist_ok=True)
    work=Path(args.work_dir);work.mkdir(parents=True,exist_ok=True)
    errors=[];sent=[];events=Queue();stop=threading.Event()
    def gateway(ws):
        ws.send(json.dumps({'op':10,'d':{'heartbeat_interval':1000}}))
        identify=json.loads(ws.recv(timeout=5));assert identify['op'] in (2,6)
        ws.send(json.dumps({'op':0,'s':1,'t':'READY','d':{'session_id':'browser-test'}}))
        while not stop.is_set():
            try:ws.send(json.dumps(events.get_nowait()))
            except Empty:pass
            try:payload=json.loads(ws.recv(timeout=.1))
            except TimeoutError:continue
            except Exception:break
            if payload['op']==1:ws.send(json.dumps({'op':11}))
    def probe(config,**kwargs):
        if config['app_secret']=='invalid-secret':raise DeliveryError('QQ 凭据校验被拒绝，请核对 AppID 和 AppSecret','blocked')
        return 'test-token','wss://api.bot.qq.com/websocket/'
    def sender(kind,config,message,delivery_id):
        sent.append({'kind':kind,'address':config.get('address'),'message':message});return 'local-provider-receipt'
    with tempfile.TemporaryDirectory(dir=work) as directory,serve(gateway,'127.0.0.1',0) as gw,patch('posthub.web.qq_credentials',probe):
        gwthread=threading.Thread(target=gw.serve_forever,daemon=True);gwthread.start()
        initialize(Path(directory),'qq-browser-password')
        app=create_app(directory,sender=sender)
        wsurl=f'ws://127.0.0.1:{gw.socket.getsockname()[1]}'
        app.state.qq.session_factory=lambda config,binding:QQSession(config,binding,authenticate=lambda c:('test-token',wsurl))
        listener=socket.socket();listener.bind(('127.0.0.1',0));base=f'http://127.0.0.1:{listener.getsockname()[1]}'
        server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False))
        thread=threading.Thread(target=lambda:server.run(sockets=[listener]),daemon=True);thread.start()
        try:
            wait_for(lambda:server.started)
            with sync_playwright() as p,httpx.Client(base_url=base,trust_env=False) as http:
                browser=p.chromium.launch(executable_path=args.browser,headless=True)
                page=browser.new_page(viewport={'width':1440,'height':1000})
                page.on('pageerror',lambda e:errors.append(str(e)))
                def shot(name):
                    page.evaluate('document.fonts.ready')
                    page.evaluate('Promise.all([...document.images].map(i=>i.decode().catch(()=>{})))')
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    page.screenshot(path=str(out/(name+'.png')))
                page.goto(base);page.get_by_label('管理员密码').fill('qq-browser-password');page.get_by_role('button',name='登录工作台').click()
                page.get_by_role('link',name='上游分发',exact=True).click()
                page.get_by_role('button',name='创建上游接口',exact=True).click()
                page.get_by_label('上游名称',exact=True).fill('QQ 上游测试应用')
                page.get_by_role('button',name='＋ 添加渠道',exact=True).click()
                page.locator('#dialog [data-kind="qq"]').click()
                page.get_by_label('渠道名称',exact=True).fill('QQ 测试接收人')
                page.get_by_label('机器人 AppID',exact=True).fill('123456')
                page.get_by_label('机器人 AppSecret',exact=True).fill('invalid-secret')
                expect(page.get_by_role('combobox',name='机器人接入方式',exact=True)).to_have_value('websocket')
                page.get_by_role('button',name='校验并保存机器人',exact=True).click()
                expect(page.locator('#toast')).to_contain_text('凭据校验被拒绝')
                assert app.state.hub.state()['targets']==[]
                expect(page.locator('#choice-dialog')).not_to_be_visible()
                shot('01-rejected-without-saving')
                page.get_by_label('机器人 AppSecret',exact=True).fill('fixture-secret-only')
                page.get_by_role('button',name='校验并保存机器人',exact=True).click()
                expect(page.locator('#qq-connection-status')).to_contain_text('机器人已连接',timeout=12000)
                expect(page.locator('#dialog')).not_to_contain_text('生成绑定码')
                expect(page.locator('#qq-conversations')).to_contain_text('暂未收到最近会话')
                expect(page.get_by_role('button',name='发送收件验证码',exact=True)).to_be_disabled()
                target_id=app.state.hub.state()['targets'][0]['id']
                assert not app.state.hub.target(target_id)['verified']
                shot('02-robot-connected')
                events.put({'op':0,'s':2,'t':'C2C_MESSAGE_CREATE','d':{'content':'文思量化通知','author':{'user_openid':'test-recipient-openid'}}})
                expect(page.locator('#qq-conversations')).to_contain_text('文思量化通知',timeout=10000)
                assert not app.state.hub.target(target_id)['config'].get('address')
                assert sent==[]
                page.set_viewport_size({'width':390,'height':844});shot('03-mobile-conversation-selection')
                page.get_by_role('button',name='确认并发送验证码',exact=True).click()
                wait_for(lambda:len(sent)==1)
                page.set_viewport_size({'width':1440,'height':1000})
                expect(page.locator('#qq-test-status')).to_contain_text('发送测试通过',timeout=10000)
                assert not app.state.hub.target(target_id)['verified']
                assert 'QQ 收件目标：QQ 测试接收人' in sent[-1]['message']['body']
                code=re.search(r'验证码：(\d{6})',sent[-1]['message']['body'])[1]
                assert sent[-1]['address']=='test-recipient-openid'
                page.get_by_label('收件验证码',exact=True).fill('123')
                events.put({'op':0,'s':3,'t':'C2C_MESSAGE_CREATE','d':{'content':'其他人的会话','author':{'user_openid':'different-person'}}})
                expect(page.locator('#qq-conversations')).to_contain_text('其他人的会话',timeout=10000)
                expect(page.get_by_label('收件验证码',exact=True)).to_have_value('123')
                assert app.state.hub.target(target_id)['config']['address']=='test-recipient-openid'
                assert len(sent)==1
                shot('04-test-accepted-awaiting-receipt-code')
                page.get_by_label('收件验证码',exact=True).fill(code)
                page.get_by_role('button',name='完成验证',exact=True).click()
                expect(page.locator('#app-form')).to_be_visible()
                expect(page.get_by_label('上游名称',exact=True)).to_have_value('QQ 上游测试应用')
                expect(page.locator('#app-form input[name=target_ids]')).to_be_checked()
                assert app.state.hub.state()['apps']==[]
                shot('05-returned-to-app')
                with page.expect_response(lambda r:r.url.endswith('/api/admin/apps') and r.request.method=='POST') as response:
                    page.locator('#app-form button[type=submit]').click()
                key=response.value.json()['api_key']
                result=http.post('/api/v1/hooks/'+key,json={'title':'local test','body':'Notification through isolated QQ adapter','request_id':'qq-browser-test'})
                assert result.status_code==202
                wait_for(lambda:len(sent)==2)
                assert sent[-1]['kind']=='qq' and sent[-1]['address']=='test-recipient-openid'
                page.get_by_role('button',name='关闭对话框',exact=True).click()
                page.get_by_role('link',name='下游渠道',exact=True).click()
                def open_verified():
                    page.locator(f'[data-action="bind"][data-id="{target_id}"]').first.click()
                    expect(page.get_by_role('heading',name='QQ 渠道配置 · QQ 测试接收人',exact=True)).to_be_visible()
                    expect(page.get_by_role('heading',name='机器人连接',exact=True)).to_be_visible()
                    expect(page.get_by_role('heading',name='已完成验证',exact=True)).to_be_visible()
                    expect(page.locator('#verify-form')).to_have_count(0)
                    expect(page.get_by_role('button',name='发送收件验证码',exact=True)).to_have_count(0)
                open_verified()
                page.locator('#receipt-verification').scroll_into_view_if_needed()
                shot('07-verified-result')
                page.get_by_role('button',name='重新验证收件目标',exact=True).click()
                expect(page.locator('#verify-form')).to_be_visible()
                assert len(sent)==2  # Opening the form doesn't send or revoke anything.
                assert app.state.hub.target(target_id)['verified']
                page.get_by_label('收件验证码',exact=True).fill('123')
                page.get_by_role('button',name='刷新会话与状态',exact=True).click()
                expect(page.get_by_label('收件验证码',exact=True)).to_have_value('123')
                page.get_by_role('button',name='返回验证结果',exact=True).click()
                expect(page.get_by_role('heading',name='已完成验证',exact=True)).to_be_visible()
                expect(page.locator('#verify-form')).to_have_count(0)
                assert len(sent)==2
                page.get_by_role('button',name='重新验证收件目标',exact=True).click()
                page.get_by_role('button',name='发送收件验证码',exact=True).click()
                wait_for(lambda:len(sent)==3)
                code=re.search(r'验证码：(\d{6})',sent[-1]['message']['body'])[1]
                page.get_by_label('收件验证码',exact=True).fill(code)
                page.get_by_role('button',name='完成验证',exact=True).click()
                expect(page.locator('#dialog')).not_to_be_visible()
                open_verified()
                # Choosing a different recipient must restore the required verification form.
                page.locator('.qq-conversation').filter(has_text='其他人的会话').get_by_role('button',name='确认并发送验证码',exact=True).click()
                page.get_by_role('button',name='更换并测试',exact=True).click()
                wait_for(lambda:len(sent)==4)
                expect(page.locator('#verify-form')).to_be_visible()
                expect(page.get_by_role('heading',name='已完成验证',exact=True)).to_have_count(0)
                assert not app.state.hub.target(target_id)['verified']
                assert sent[-1]['address']=='different-person'
                code=re.search(r'验证码：(\d{6})',sent[-1]['message']['body'])[1]
                page.get_by_label('收件验证码',exact=True).fill(code)
                page.get_by_role('button',name='完成验证',exact=True).click()
                expect(page.locator('#dialog')).not_to_be_visible()
                page.get_by_role('button',name='添加渠道',exact=True).click()
                page.locator('#dialog [data-kind="qq"]').click()
                page.get_by_label('渠道名称',exact=True).fill('高级回调测试')
                page.get_by_label('机器人 AppID',exact=True).fill('654321')
                page.get_by_label('机器人 AppSecret',exact=True).fill('fixture-secret-only')
                page.get_by_role('combobox',name='机器人接入方式',exact=True).select_option('webhook')
                page.get_by_role('button',name='校验并保存机器人',exact=True).click()
                expect(page.locator('#dialog')).to_contain_text('当前访问地址不能作为 QQ 公网回调')
                assert '/api/qq/' not in page.locator('#dialog').inner_text()
                shot('06-private-callback-blocked')
                page.get_by_role('button',name='关闭对话框',exact=True).click()
                expect(page.locator('#choice-title')).to_have_text('收件目标尚未验证')
                page.get_by_role('button',name='保留草稿并关闭',exact=True).click()
                expect(page.locator('#dialog')).not_to_be_visible()
                assert not errors,errors
                browser.close()
            (out/'results.json').write_text(json.dumps({'passed':True,'browser_errors':errors,'local_sender_calls':len(sent),'real_qq_messages_sent':0,'checks':['invalid_credentials_do_not_save','actual_local_websocket_ready','binding_code_removed','incoming_message_only_adds_candidate','explicit_selection_before_test_sends','test_message_names_recipient','provider_receipt_does_not_auto_verify_recipient','receipt_code_retained','other_users_cannot_replace_recipient','polling_preserves_input','verification_returns_to_unsaved_app','upstream_routes_to_confirmed_recipient','private_callback_not_presented_as_usable','mobile_conversation_selection','unfinished_target_can_be_kept','verified_result_hides_send_and_form','explicit_reverify_does_not_send_or_revoke','cancel_reverify_restores_result','reverification_completes','changed_recipient_requires_verification','robot_and_recipient_are_separate_concepts']},ensure_ascii=False,indent=2),encoding='utf-8')
        finally:
            stop.set();server.should_exit=True;thread.join(25);gw.shutdown();gwthread.join(5)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--browser',required=True);parser.add_argument('--artifacts',required=True);parser.add_argument('--work-dir',default='work')
    run(parser.parse_args())

