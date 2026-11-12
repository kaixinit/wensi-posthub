"""Browser acceptance against an explicitly started LOCAL test instance.

Requires: pip install playwright; use installed Chrome via --browser.
The test adds two clearly named loopback HTTP targets and sends test messages.
Never point this script at a production instance.
"""
import argparse
import json
from pathlib import Path
import re
import time

from playwright.sync_api import sync_playwright, expect


def run(args):
    artifacts = Path(args.artifacts)
    artifacts.mkdir(parents=True,exist_ok=True)
    password = json.loads(Path(args.password_file).read_text(encoding='utf-8'))['password']
    inbox = Path(args.inbox_file)
    unique = str(int(time.time()))
    names = [f'本机验收 HTTP A · {unique}', f'本机验收 HTTP B · {unique}']
    errors=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(executable_path=args.browser,headless=True)
        context=browser.new_context(viewport={'width':1487,'height':1058},device_scale_factor=1)
        page=context.new_page()
        page.on('pageerror',lambda error:errors.append(str(error)))
        page.goto(args.base)
        page.get_by_label('管理员密码').fill(password)
        page.get_by_role('button',name='登录工作台').click()
        expect(page.get_by_role('heading',name='下游渠道')).to_be_visible()
        page.screenshot(path=str(artifacts/'channels-initial.png'),full_page=True)
        for name in names:
            page.get_by_role('button',name='添加接口',exact=True).click()
            page.get_by_label('渠道名称',exact=True).fill(name)
            page.get_by_label('接收端 HTTP(S) 地址',exact=True).fill(args.receiver)
            page.get_by_role('button',name='继续绑定',exact=True).click()
            page.get_by_role('button',name='保存草稿并继续绑定',exact=True).click()
            expect(page.get_by_role('button',name='发送收件验证码')).to_be_visible()
            before=len(inbox.read_text(encoding='utf-8').splitlines()) if inbox.exists() else 0
            page.get_by_role('button',name='发送收件验证码').click()
            code=None
            for _ in range(30):
                lines=inbox.read_text(encoding='utf-8').splitlines() if inbox.exists() else []
                if len(lines)>before:
                    code=re.search(r'验证码：(\d{6})',json.loads(lines[-1])['body'])[1]
                    break
                time.sleep(.5)
            assert code, 'Real receiver did not get verification message'
            page.get_by_label('收件验证码',exact=True).fill(code)
            page.get_by_role('button',name='完成验证',exact=True).click()
            expect(page.get_by_role('dialog')).not_to_be_visible()
        page.screenshot(path=str(artifacts/'channels-desktop.png'),full_page=True)
        page.get_by_role('link',name='网络通信',exact=True).click()
        page.get_by_label('收件人可访问的平台地址').fill(args.base)
        page.get_by_role('button',name='保存设置',exact=True).click()
        expect(page.get_by_text('设置已保存',exact=True)).to_be_visible()
        page.get_by_role('link',name='下游渠道',exact=True).click()
        page.get_by_role('button',name=re.compile('^渠道管理')).click()
        for name in names:page.get_by_role('checkbox',name='选择 '+name,exact=True).check()
        page.screenshot(path=str(artifacts/'targets-desktop.png'),full_page=True)
        page.get_by_role('button',name='用于系统分发').click()
        page.get_by_label('消息标题',exact=True).fill('浏览器端到端验收 · '+unique)
        page.get_by_label('消息正文',exact=True).fill('同一条消息已勾选两个本机目标，验证分发、回执和收件确认。')
        page.get_by_label('要求收件人点击链接确认收到').check()
        page.screenshot(path=str(artifacts/'compose-desktop.png'),full_page=True)
        page.get_by_role('button',name='提交分发',exact=True).click()
        expect(page.get_by_role('dialog')).to_be_visible()
        for _ in range(15):
            page.get_by_role('button',name='刷新状态',exact=True).click()
            if page.get_by_role('dialog').get_by_text('渠道已接受',exact=True).count()==2:break
            time.sleep(.5)
        expect(page.get_by_role('dialog').get_by_text('渠道已接受',exact=True)).to_have_count(2)
        lines=[json.loads(line) for line in inbox.read_text(encoding='utf-8').splitlines()]
        messages=[item for item in lines if item['title']=='浏览器端到端验收 · '+unique]
        assert len(messages)==2
        ack=context.new_page()
        ack.goto(messages[0]['ack_url'])
        ack.get_by_role('button',name='确认收到',exact=True).click()
        expect(ack.get_by_text('已记录收件确认。',exact=True)).to_be_visible()
        ack.close()
        page.get_by_role('button',name='刷新状态',exact=True).click()
        expect(page.get_by_role('dialog').get_by_text('收件人已确认',exact=True)).to_have_count(1)
        page.screenshot(path=str(artifacts/'delivery-desktop.png'),full_page=True)
        page.get_by_role('dialog').get_by_role('button',name='关闭',exact=True).click()
        for name in ['分发工作台','告警中心','上游分发','网络通信','下游渠道']:
            page.get_by_role('link',name=name,exact=True).click()
            expect(page.get_by_role('heading',name=name,exact=True)).to_be_visible()
        page.get_by_role('button',name='可接入渠道').click()
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Mobile horizontal overflow'
        page.screenshot(path=str(artifacts/'channels-mobile.png'),full_page=True)
        assert not errors, errors
        browser.close()
    (artifacts/'browser-results.json').write_text(json.dumps({'passed':True,'page_errors':errors,'verified':['login','create HTTP targets','real recipient verification','multi-target selection','submit message','provider receipts','explicit acknowledgment','navigation','390px responsive layout'],'targets':names},ensure_ascii=False,indent=2),encoding='utf-8')
    print('Browser acceptance passed; screenshots and report:', artifacts)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--base',default='http://127.0.0.1:8787')
    parser.add_argument('--receiver',default='http://127.0.0.1:8788/inbox')
    parser.add_argument('--password-file',required=True)
    parser.add_argument('--inbox-file',required=True)
    parser.add_argument('--browser',required=True)
    parser.add_argument('--artifacts',default='artifacts')
    args=parser.parse_args()
    if not args.base.startswith('http://127.0.0.1:') or not args.receiver.startswith('http://127.0.0.1:'):
        parser.error('This acceptance script only targets loopback test instances.')
    run(args)
