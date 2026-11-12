"""Isolated acceptance for bundled documentation and its copyable API example."""
import argparse
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
from posthub.models import AppInput
from posthub.storage import initialize
from posthub.web import create_app

def run(args):
    out=Path(args.artifacts);out.mkdir(parents=True,exist_ok=True)
    project=Path(__file__).resolve().parents[1]
    sample=json.loads(re.search(r'```json\n(.*?)\n```',(project/'docs/guide/02-upstream.md').read_text(encoding='utf-8'),re.S)[1])
    listener=socket.socket();listener.bind(('127.0.0.1',0));base=f'http://127.0.0.1:{listener.getsockname()[1]}'
    with tempfile.TemporaryDirectory(dir=args.work_dir) as directory:
        initialize(Path(directory),'help-browser-test-password');app=create_app(directory,run_worker=False)
        key=app.state.hub.create_app(AppInput(name='操作手册测试应用'))['api_key']
        server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False))
        thread=threading.Thread(target=lambda:server.run(sockets=[listener]),daemon=True);thread.start()
        try:
            deadline=time.monotonic()+10
            while not server.started:assert time.monotonic()<deadline;time.sleep(.03)
            with httpx.Client(base_url=base,trust_env=False) as http:
                reply=http.post('/api/v1/hooks/'+key,json=sample);assert reply.status_code==202 and reply.json()['routing_state']=='awaiting_routing'
            errors=[]
            with sync_playwright() as p:
                browser=p.chromium.launch(executable_path=args.browser,headless=True)
                context=browser.new_context(viewport={'width':1440,'height':1000},permissions=['clipboard-read','clipboard-write'])
                page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(base+'/#help/upstream');page.get_by_label('管理员密码').fill('help-browser-test-password');page.get_by_role('button',name='登录工作台',exact=True).click()
                expect(page.get_by_role('heading',name='上游 API 接入',exact=True)).to_be_visible()
                expect(page.locator('.sidebar a[data-page="help"]')).to_have_attribute('aria-current','page')
                expect(page).to_have_title('使用帮助 · 文思邮差')
                expect(page.locator('.help-nav nav a')).to_have_count(7)
                page.get_by_role('button',name='复制示例 1',exact=True).click()
                expect(page.locator('#toast')).to_contain_text('已复制')
                assert json.loads(page.evaluate('navigator.clipboard.readText()'))==sample
                page.evaluate('document.fonts.ready');page.screenshot(path=str(out/'01-help-desktop.png'))
                for term,title in [('手机授权验证码','渠道绑定与收件验证'),('SOCKS5','网络通信与访问设置')]:
                    page.get_by_role('searchbox',name='搜索操作文档').fill(term)
                    expect(page.locator('.help-result').filter(has=page.get_by_role('heading',name=title,exact=True))).to_be_visible()
                page.get_by_role('searchbox',name='搜索操作文档').fill('<img src=x onerror=alert(1)>')
                expect(page.get_by_text('找到 0 篇相关说明',exact=True)).to_be_visible()
                assert page.locator('#help-layout img').count()==0
                page.get_by_role('button',name='清除搜索',exact=True).click()
                with page.expect_download() as event:page.get_by_role('link',name='下载操作手册',exact=True).click()
                event.value.save_as(str(out/'downloaded-guide.md'))
                assert (out/'downloaded-guide.md').read_text(encoding='utf-8')==(project/'posthub/static/user-guide.md').read_text(encoding='utf-8')
                page.get_by_role('link',name='前往上游分发 →',exact=True).click()
                expect(page.get_by_role('heading',name='上游分发',exact=True)).to_be_visible()
                page.get_by_role('link',name='使用说明',exact=True).click()
                expect(page.get_by_role('heading',name='上游 API 接入',exact=True)).to_be_visible()
                page.set_viewport_size({'width':390,'height':844})
                for slug in ['quickstart','upstream','channels','delivery','network','troubleshooting']:
                    page.goto(base+'/#help/'+slug)
                    expect(page.locator('#help-article-title')).to_be_visible()
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),slug
                page.goto(base+'/#help/channels');page.evaluate('document.fonts.ready')
                page.screenshot(path=str(out/'02-help-mobile.png'),full_page=True)
                assert not errors,errors;browser.close()
        finally:server.should_exit=True;thread.join(10)
    result={'passed':True,'page_errors':errors,'checks':['deep link login','seven local chapters','search and empty state','search markup escaped','copyable JSON verified against API','manual download matches source','contextual help link','mobile no horizontal overflow'],'real_external_messages_sent':0}
    (out/'results.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--browser',required=True);parser.add_argument('--work-dir',required=True);parser.add_argument('--artifacts',required=True);run(parser.parse_args())
