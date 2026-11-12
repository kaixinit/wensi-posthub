from pathlib import Path
import json
from playwright.sync_api import sync_playwright, expect

import argparse
parser=argparse.ArgumentParser(description='Visual and interaction audit of a local test instance only')
parser.add_argument('--base',default='http://127.0.0.1:8787')
parser.add_argument('--password-file',required=True)
parser.add_argument('--browser',required=True)
parser.add_argument('--artifacts',default='artifacts/product-audit-after')
args=parser.parse_args()
if not args.base.startswith('http://127.0.0.1:'):
    parser.error('Only loopback test instances are allowed')
out=Path(args.artifacts)
out.mkdir(parents=True,exist_ok=True)
result={'screens':[], 'page_errors':[], 'console_errors':[], 'network_failures':[]}
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path=args.browser,headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1000},device_scale_factor=1)
    page.on('pageerror',lambda e:result['page_errors'].append(str(e)))
    page.on('console',lambda e:result['console_errors'].append(e.text) if e.type=='error' and '401' not in e.text else None)
    page.on('requestfailed',lambda r:result['network_failures'].append(r.url))
    def capture(name):
        page.evaluate('document.fonts.ready')
        page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        # Fixed dialogs use the actual viewport; whole-page captures can
        # misplace them relative to an already scrolled background page.
        page.screenshot(path=str(out/name),full_page=not page.get_by_role('dialog').is_visible())
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),name+' horizontal overflow'
        result['screens'].append(name)
    def navigate(name):
        page.get_by_role('link',name=name,exact=True).click()
        expect(page.get_by_role('heading',name=name,exact=True)).to_be_visible()
    page.goto(args.base)
    expect(page.get_by_label('管理员密码')).to_be_visible()
    capture('01-login.png')
    page.get_by_label('管理员密码').fill('invalid-audit-password')
    page.get_by_role('button',name='登录工作台').click()
    expect(page.locator('#login-error')).not_to_be_empty()
    page.get_by_label('管理员密码').fill(json.loads(Path(args.password_file).read_text(encoding='utf-8'))['password'])
    page.get_by_role('button',name='登录工作台').click()
    expect(page.get_by_role('heading',name='下游渠道',exact=True)).to_be_visible()
    capture('02-channels.png')
    assert page.evaluate('document.fonts.check(\'14px "PostHub Sans"\', "文思邮差 消息中转与分发平台 123")')
    result['font_loaded']=True
    result['typography']=page.evaluate("""() => [...document.querySelectorAll('h1,h2,h3,p,small,button,label,.badge,.sidebar nav a')].filter(e=>e.getBoundingClientRect().width>0).map(e=>{let s=getComputedStyle(e);return {text:e.textContent.trim().slice(0,60),family:s.fontFamily,size:s.fontSize,weight:s.fontWeight,line:s.lineHeight,color:s.color}})""")
    page.get_by_role('button',name='配置邮箱',exact=True).click()
    expect(page.get_by_role('heading',name='添加邮箱目标')).to_be_visible()
    expect(page.get_by_label('渠道名称')).to_have_attribute('placeholder','例如：运维值班邮箱')
    capture('03-email-form.png')
    page.get_by_role('button',name='继续绑定').click()
    assert page.locator('#target-form').evaluate('f => !f.checkValidity()')
    expect(page.get_by_role('heading',name='添加邮箱目标')).to_be_visible()
    for _ in range(12):
        page.keyboard.press('Tab')
        focus=page.evaluate('({inside:document.querySelector("#dialog").contains(document.activeElement),tag:document.activeElement.tagName,name:document.activeElement.getAttribute("name"),documentFocused:document.hasFocus()})')
        assert focus['inside'] or not focus['documentFocused'],focus
    result['validation_and_modal_focus']=True
    page.keyboard.press('Escape')
    expect(page.get_by_role('dialog')).not_to_be_visible()
    page.get_by_role('button',name='渠道管理',exact=False).click()
    expect(page.get_by_label('搜索渠道名称')).to_be_visible()
    expect(page.get_by_text('等待绑定收件目标',exact=True)).to_have_count(0)
    capture('04-targets.png')
    navigate('系统分发')
    capture('05-compose.png')
    navigate('投递记录')
    capture('06-records.png')
    page.get_by_role('button',name='详情',exact=True).first.click()
    expect(page.get_by_role('dialog')).to_be_visible()
    capture('07-delivery.png')
    page.get_by_role('button',name='关闭对话框').click()
    for i,name in enumerate(['分发工作台','告警中心','上游分发','网络通信'],8):
        navigate(name)
        capture(f'{i:02}-{["dashboard","alerts","apps","network"][i-8]}.png')
    for width,height in [(1366,768),(1024,768),(390,844),(360,800)]:
        page.set_viewport_size({'width':width,'height':height})
        navigate('下游渠道')
        page.get_by_role('button',name='可接入渠道',exact=True).click()
        expect(page.get_by_role('button',name='配置邮箱',exact=True)).to_be_visible()
        capture(f'12-channels-{width}.png')
        if width==390:
            page.get_by_role('button',name='配置邮箱',exact=True).click()
            expect(page.get_by_role('heading',name='添加邮箱目标')).to_be_visible()
            capture('13-email-mobile.png')
            page.get_by_role('button',name='继续绑定',exact=True).scroll_into_view_if_needed()
            capture('13-email-mobile-actions.png')
            page.get_by_role('button',name='关闭对话框').click()
        navigate('系统分发')
        capture(f'14-compose-{width}.png')
        navigate('网络通信')
        capture(f'15-network-{width}.png')
    page.get_by_role('button',name='退出登录').filter(visible=True).click()
    expect(page.get_by_label('管理员密码')).to_be_visible()
    expect(page.locator('#login-error')).to_be_empty()
    assert not result['page_errors'],result['page_errors']
    assert not result['console_errors'],result['console_errors']
    assert not result['network_failures'],result['network_failures']
    result['passed']=True
    (out/'observations.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    browser.close()
print('Visual/interaction audit passed:',len(result['screens']),'screens, local font, responsive layout, close and logout.')
