"""Exercise compose and real delivery over a non-loopback HTTP origin.

Uses temporary data and a local receiver; never sends to configured user channels.
Requires Playwright and a locally installed browser. --lan-host must be an IP
assigned to this computer, not localhost or a loopback IP.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
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

from posthub.storage import initialize
from posthub.web import create_app


def wait_for(predicate):
    deadline = time.monotonic() + 15
    while not predicate():
        assert time.monotonic() < deadline, 'Timed out waiting for local delivery'
        time.sleep(.05)


def run(args):
    assert not ipaddress.ip_address(args.lan_host).is_loopback
    out = Path(args.artifacts); out.mkdir(parents=True, exist_ok=True)
    work = Path(args.work_dir); work.mkdir(parents=True, exist_ok=True)
    received = []; errors = []; checks = []
    uuid_pattern = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$')
    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(b'{"accepted":true}')
        def log_message(self, *args): pass
    receiver = ThreadingHTTPServer(('127.0.0.1', 0), Receiver)
    receiver_thread = threading.Thread(target=receiver.serve_forever, daemon=True); receiver_thread.start()
    listener = socket.socket(); listener.bind(('0.0.0.0', 0))
    port = listener.getsockname()[1]
    local = f'http://127.0.0.1:{port}'
    lan = f'http://{args.lan_host}:{port}'
    with tempfile.TemporaryDirectory(dir=work) as directory:
        initialize(Path(directory), 'isolated-lan-test-password')
        app = create_app(directory)
        server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False))
        thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True); thread.start()
        try:
            wait_for(lambda: server.started)
            with httpx.Client(base_url=local, trust_env=False, timeout=10) as http, sync_playwright() as p:
                login = http.post('/api/login', json={'password': 'isolated-lan-test-password'}, headers={'X-PostHub-Request': '1'})
                login.raise_for_status()
                http.headers.update({'X-PostHub-Request': '1', 'X-CSRF-Token': login.json()['csrf']})
                for name in ['隔离接收端 A', '隔离接收端 B']:
                    target = http.post('/api/admin/targets', json={'name': name, 'kind': 'webhook', 'config': {'url': f'http://127.0.0.1:{receiver.server_port}/inbox'}})
                    target.raise_for_status(); target_id = target.json()['id']
                    before = len(received)
                    http.post(f'/api/admin/targets/{target_id}/verify').raise_for_status()
                    wait_for(lambda: len(received) > before)
                    code = re.search(r'验证码：(\d{6})', received[-1]['body'])[1]
                    http.post(f'/api/admin/targets/{target_id}/confirm', json={'code': code}).raise_for_status()
                browser = p.chromium.launch(executable_path=args.browser, headless=True)
                cases = [('lan-http', lan, False), ('localhost-native', local, False), ('localhost-compatibility', local, True)]
                for name, origin, disable_native in cases:
                    context = browser.new_context(viewport={'width': 1440, 'height': 1000})
                    if disable_native:
                        context.add_init_script("Object.defineProperty(crypto, 'randomUUID', {value: undefined, configurable: true});")
                    page = context.new_page(); page.on('pageerror', lambda e: errors.append(str(e)))
                    page.goto(origin)
                    capabilities = page.evaluate('({secure: isSecureContext, randomUUID: typeof crypto.randomUUID, getRandomValues: typeof crypto.getRandomValues})')
                    assert capabilities['secure'] == (origin == local)
                    assert capabilities['randomUUID'] == ('function' if name == 'localhost-native' else 'undefined')
                    page.get_by_label('管理员密码').fill('isolated-lan-test-password')
                    page.get_by_role('button', name='登录工作台').click()
                    expect(page.get_by_role('heading', name='下游渠道', exact=True)).to_be_visible()
                    page.get_by_role('link', name='系统分发', exact=True).click()
                    try:
                        expect(page.get_by_role('heading', name='系统分发', exact=True)).to_be_visible()
                        request_id = page.locator('#compose-form input[name=request_id]').input_value()
                        assert uuid_pattern.fullmatch(request_id), request_id
                        page.reload()
                        expect(page.get_by_role('heading', name='系统分发', exact=True)).to_be_visible()
                        assert page.locator('#compose-form input[name=request_id]').input_value() != request_id
                        ids = page.evaluate('Array.from({length: 1000}, () => newRequestId())')
                        assert len(set(ids)) == 1000 and all(uuid_pattern.fullmatch(value) for value in ids)
                        page.get_by_label('消息标题', exact=True).fill(name + ' 实际投递')
                        page.get_by_label('消息正文', exact=True).fill('通过浏览器提交到两个隔离 HTTP 接收端。')
                        for target in page.locator('#compose-form input[name=target_ids]').all(): target.check()
                        page.screenshot(path=str(out / (name + '-compose.png')))
                        with page.expect_response(lambda r: r.url.endswith('/api/admin/messages') and r.request.method == 'POST') as sending:
                            page.get_by_role('button', name='提交分发', exact=True).click()
                        response = sending.value
                        assert response.status == 202, response.text()
                        payload = response.request.post_data_json
                        ticket = response.json()
                        wait_for(lambda: all(d['state'] == 'accepted' for d in app.state.hub.message(ticket['id'])['deliveries']))
                        delivered = [m for m in received if m['message_id'] == ticket['id']]
                        assert len(delivered) == 2 and len({m['delivery_id'] for m in delivered}) == 2
                        repeated = http.post('/api/admin/messages', json=payload)
                        repeated.raise_for_status()
                        assert repeated.json()['id'] == ticket['id'] and repeated.json()['deduplicated']
                        assert len(app.state.hub.message(ticket['id'])['deliveries']) == 2
                        expect(page.locator('#dialog')).to_be_visible()
                        page.get_by_role('button', name='关闭对话框', exact=True).click()
                        page.get_by_role('link', name='系统分发', exact=True).click()
                        expect(page.get_by_role('heading', name='系统分发', exact=True)).to_be_visible()
                        assert page.locator('#compose-form input[name=request_id]').input_value() != payload['request_id']
                        page.set_viewport_size({'width': 390, 'height': 844})
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                        page.screenshot(path=str(out / (name + '-mobile.png')))
                        checks.append({'case': name, 'browser': capabilities, 'two_deliveries': True, 'same_request_deduplicated': True, 'distinct_valid_uuid_samples': 1000, 'mobile_checked': True})
                    except Exception:
                        page.screenshot(path=str(work / (name + '-failure.png')))
                        raise
                    finally:
                        context.close()
                browser.close()
            assert not errors, errors
            notifications = [m for m in received if m['title'] != '文思邮差 收件验证']
            assert len(notifications) == 6
            result = {'passed': True, 'version': '0.1.7', 'checks': checks, 'real_http_notifications': len(notifications), 'page_errors': errors, 'user_data_modified': False, 'tested_from': 'deployment_host_using_non_loopback_ip'}
            (out / 'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(result, ensure_ascii=False))
        finally:
            server.should_exit = True; thread.join(timeout=15); listener.close()
            receiver.shutdown(); receiver.server_close(); receiver_thread.join(timeout=3)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--browser', required=True)
    parser.add_argument('--lan-host', required=True)
    parser.add_argument('--work-dir', required=True)
    parser.add_argument('--artifacts', required=True)
    run(parser.parse_args())
