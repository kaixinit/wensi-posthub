"""Password lifecycle in real Chrome against a disposable local database."""
import argparse
import json
from pathlib import Path
import socket
import tempfile
import threading
import time

import uvicorn
from playwright.sync_api import sync_playwright,expect

from posthub.storage import initialize
from posthub.web import create_app


def run(args):
    out=Path(args.artifacts);out.mkdir(parents=True,exist_ok=True)
    work=Path(args.work_dir);work.mkdir(parents=True,exist_ok=True)
    old='initial-browser-password-123';new='updated-browser-password-456'
    errors=[]
    with tempfile.TemporaryDirectory(dir=work) as directory:
        initialize(Path(directory),old,must_change_password=True)
        app=create_app(directory,run_worker=False)
        listener=socket.socket();listener.bind(('127.0.0.1',0));base=f'http://127.0.0.1:{listener.getsockname()[1]}'
        server=uvicorn.Server(uvicorn.Config(app,log_level='error',access_log=False))
        thread=threading.Thread(target=lambda:server.run(sockets=[listener]),daemon=True);thread.start()
        try:
            deadline=time.monotonic()+10
            while not server.started:
                assert time.monotonic()<deadline;time.sleep(.05)
            with sync_playwright() as p:
                browser=p.chromium.launch(executable_path=args.browser,headless=True,args=['--host-resolver-rules=MAP posthub.test 127.0.0.1','--no-proxy-server'])
                context=browser.new_context(viewport={'width':1366,'height':900})
                page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                page.add_init_script("""const originalCopy=document.execCommand.bind(document);document.execCommand=(cmd,...args)=>{if(cmd==='copy'){window.lastCopied=document.activeElement.value;window.copyParent=document.activeElement.parentElement.id;}return originalCopy(cmd,...args);};""")
                page.goto(base.replace('127.0.0.1','posthub.test'))
                assert page.evaluate('!window.isSecureContext'), 'Exercise LAN HTTP clipboard fallback'
                expect(page.get_by_label('管理员账号',exact=True)).to_have_value('admin')
                page.screenshot(path=str(out/'login-desktop.png'))
                page.get_by_label('管理员密码',exact=True).fill(old)
                page.get_by_role('button',name='登录工作台',exact=True).click()
                expect(page.get_by_role('heading',name='账号设置',exact=True)).to_be_visible()
                expect(page.get_by_text('你正在使用安装时生成的初始密码，请先设置自己的密码，再使用管理功能。',exact=True)).to_be_visible()
                page.locator('.sidebar a[data-page="channels"]').click()
                expect(page.get_by_role('heading',name='账号设置',exact=True)).to_be_visible()
                # Incorrect current password is shown inline, with inputs retained.
                page.get_by_label('当前密码',exact=True).fill('wrong-password')
                page.get_by_label('新密码',exact=True).fill(new)
                page.get_by_label('再次输入新密码',exact=True).fill(new+'mismatch')
                page.get_by_role('button',name='修改密码并重新登录').click()
                expect(page.locator('#account-error')).to_have_text('两次新密码不一致')
                page.get_by_role('button',name='显示新密码',exact=True).click()
                expect(page.get_by_label('新密码',exact=True)).to_have_attribute('type','text')
                expect(page.get_by_label('再次输入新密码',exact=True)).to_have_attribute('type','text')
                page.get_by_role('button',name='复制新密码',exact=True).click()
                assert page.evaluate('window.lastCopied')==new
                expect(page.locator('.clipboard-buffer')).to_have_count(0)
                page.get_by_role('button',name='隐藏新密码',exact=True).click()
                expect(page.get_by_label('新密码',exact=True)).to_have_attribute('type','password')
                page.get_by_label('再次输入新密码',exact=True).fill(new)
                page.get_by_role('button',name='修改密码并重新登录').click()
                expect(page.locator('#account-error')).to_have_text('当前密码不正确')
                page.get_by_label('当前密码',exact=True).fill(old)
                page.screenshot(path=str(out/'change-password-desktop.png'))
                page.get_by_role('button',name='修改密码并重新登录').click()
                expect(page.get_by_role('heading',name='密码已修改',exact=True)).to_be_visible()
                expect(page.get_by_label('本次设置的新密码',exact=True)).to_have_value(new)
                expect(page.get_by_label('本次设置的新密码',exact=True)).to_have_attribute('type','password')
                page.get_by_role('button',name='显示新密码',exact=True).click()
                expect(page.get_by_label('本次设置的新密码',exact=True)).to_have_attribute('type','text')
                page.get_by_role('button',name='复制新密码',exact=True).click()
                assert page.evaluate('window.lastCopied')==new and page.evaluate('window.copyParent')=='dialog'
                expect(page.locator('.clipboard-buffer')).to_have_count(0)
                assert page.evaluate('JSON.stringify(localStorage)+JSON.stringify(sessionStorage)').find(new)==-1
                page.get_by_role('button',name='隐藏新密码',exact=True).click()
                page.screenshot(path=str(out/'password-saved-desktop.png'))
                page.set_viewport_size({'width':390,'height':844})
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                page.screenshot(path=str(out/'password-saved-mobile.png'),full_page=True)
                page.get_by_role('button',name='已保存，返回登录',exact=True).click()
                expect(page.locator('#changed-password')).to_have_count(0)
                page.set_viewport_size({'width':1366,'height':900})
                expect(page.get_by_role('button',name='登录工作台')).to_be_visible()
                expect(page.get_by_label('管理员密码',exact=True)).to_have_value('')
                page.get_by_label('管理员密码',exact=True).fill(old)
                page.get_by_role('button',name='登录工作台').click()
                expect(page.locator('#login-error')).to_contain_text('账号或密码不正确')
                page.get_by_label('管理员密码',exact=True).fill(new)
                page.get_by_role('button',name='登录工作台').click()
                expect(page.get_by_role('heading',name='接入向导',exact=True)).to_be_visible()
                expect(page.get_by_text('你正在使用安装时生成的初始密码，请先设置自己的密码，再使用管理功能。',exact=True)).to_have_count(0)
                page.locator('.sidebar a[data-page="channels"]').click()
                expect(page.get_by_role('heading',name='下游渠道',exact=True)).to_be_visible()
                page.locator('.sidebar a[data-page="account"]').click()
                page.set_viewport_size({'width':390,'height':844})
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                page.screenshot(path=str(out/'account-mobile.png'),full_page=True)
                assert not errors,errors
                browser.close()
        finally:server.should_exit=True;thread.join(10)
    result={'passed':True,'username_default':'admin','initial_password_change_enforced':True,'current_password_checked':True,'old_password_rejected':True,'new_password_login':'passed','desktop_mobile':'passed','password_show_hide_copy':'passed','lan_http_copy_inside_modal':'passed','close_clears_password':'passed','browser_storage_plaintext':False,'page_errors':errors,'production_password_changed':False}
    (out/'results.json').write_text(json.dumps(result,indent=2),encoding='utf-8');print(json.dumps(result))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--browser',required=True);parser.add_argument('--work-dir',required=True);parser.add_argument('--artifacts',required=True);run(parser.parse_args())
