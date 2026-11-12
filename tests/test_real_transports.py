"""Real loopback HTTP/SMTP and proxy tests. Never contact third-party accounts."""
from contextlib import contextmanager
import json
import re
import socket
import socketserver
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from aiosmtpd.controller import Controller
import httpx

from posthub import adapters
from posthub.core import Hub
from posthub.models import MessageInput, TargetInput


@contextmanager
def server(handler, server_class=ThreadingHTTPServer):
    instance = server_class(('127.0.0.1',0),handler)
    thread=threading.Thread(target=instance.serve_forever,daemon=True)
    thread.start()
    try: yield instance.server_address[1]
    finally:
        instance.shutdown();instance.server_close();thread.join(timeout=3)


def test_real_http_validation_send_and_idempotency(environment):
    received=[]
    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append((payload,self.headers.get('Idempotency-Key')))
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
            self.wfile.write(b'{"accepted":true}')
        def log_message(self,*args):pass
    with server(Receiver) as port:
        hub=Hub(environment)
        target=hub.save_target(TargetInput(name='本机 HTTP 验收',kind='webhook',config={'url':f'http://127.0.0.1:{port}/inbox'}))['id']
        hub.verify_start(target);hub.tick()
        code=re.search(r'验证码：(\d{6})',received[-1][0]['body'])[1]
        hub.verify_finish(target,code)
        data=MessageInput(title='真实 HTTP 验收',body='这是经过 TCP 连接的通知',target_ids=[target],request_id='real-http')
        result=hub.submit(data);hub.submit(data);hub.tick();hub.tick()
        assert len(received)==2  # one verification + one notification
        assert received[-1][0]['delivery_id']==received[-1][1]
        assert hub.message(result['id'])['deliveries'][0]['state']=='accepted'


def test_real_apprise_smtp_message():
    received=[]
    class Mailbox:
        async def handle_DATA(self,server,session,envelope):
            received.append(envelope)
            return '250 Message accepted'
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1',0));port=reserve.getsockname()[1]
    controller=Controller(Mailbox(),hostname='127.0.0.1',port=port)
    controller.start()
    try:
        config={'host':'127.0.0.1','port':port,'security':'insecure','sender':'posthub@example.test','recipient':'operator@example.test'}
        adapters.validate_config('email',config)
        adapters.send('email',config,{'id':'smtp','title':'PostHub SMTP test','body':'Real mail transport.'},'delivery-smtp')
        assert len(received)==1
        assert received[0].rcpt_tos==['operator@example.test']
        assert b'Real mail transport.' in received[0].content
    finally:controller.stop()


def test_real_http_proxy_request():
    paths=[]
    class Proxy(BaseHTTPRequestHandler):
        def do_GET(self):
            paths.append(self.path)
            self.send_response(200);self.end_headers();self.wfile.write(b'proxy-reached')
        def log_message(self,*args):pass
    with server(Proxy) as port:
        with adapters.client({'proxy':f'http://127.0.0.1:{port}'}) as client:
            response=client.get('http://example.test/through-proxy')
        assert response.text=='proxy-reached'
    assert paths==['http://example.test/through-proxy']


def test_real_socks5_proxy_handshake():
    destinations=[]
    class Socks(socketserver.StreamRequestHandler):
        def handle(self):
            version,methods=self.rfile.read(2)
            assert version==5
            self.rfile.read(methods)
            self.wfile.write(b'\x05\x00');self.wfile.flush()
            version,cmd,reserved,kind=self.rfile.read(4)
            if kind==3:
                size=self.rfile.read(1)[0];host=self.rfile.read(size).decode()
            elif kind==1:host=socket.inet_ntoa(self.rfile.read(4))
            else:raise AssertionError('unexpected address type')
            port=struct.unpack('!H',self.rfile.read(2))[0]
            destinations.append((host,port))
            self.wfile.write(b'\x05\x00\x00\x01\x7f\x00\x00\x01\x1f\x90');self.wfile.flush()
            request=self.rfile.readline()
            while self.rfile.readline() not in (b'\r\n',b''):pass
            self.wfile.write(b'HTTP/1.1 200 OK\r\nContent-Length: 8\r\nConnection: close\r\n\r\nsocks-ok');self.wfile.flush()
    with server(Socks,socketserver.ThreadingTCPServer) as port:
        with adapters.client({'proxy':f'socks5://127.0.0.1:{port}'}) as client:
            response=client.get('http://example.test/path')
        assert response.text=='socks-ok'
    assert destinations==[('example.test',80)]
