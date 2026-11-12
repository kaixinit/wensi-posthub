"""Black-box restart of the real web process with a durable scheduled task."""
from datetime import datetime, timezone
import json
import os
import re
import socket
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler

import httpx

from .test_real_transports import server


def test_real_service_restart_and_process_lock(environment):
    received=[]
    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(b'{"accepted":true}')
        def log_message(self,*args):pass
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1',0));port=reserve.getsockname()[1]
    command=[sys.executable,'-m','posthub','serve','--data-dir',str(environment.directory),'--port',str(port)]
    processes=[]
    def start():
        process=subprocess.Popen(command,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        processes.append(process)
        for _ in range(60):
            try:
                if httpx.get(f'http://127.0.0.1:{port}/healthz',timeout=1,trust_env=False).status_code==200:return process
            except httpx.HTTPError:pass
            assert process.poll() is None,'server exited during startup'
            time.sleep(.1)
        raise AssertionError('server did not become healthy')
    try:
        with server(Receiver) as receiver_port, httpx.Client(base_url=f'http://127.0.0.1:{port}',trust_env=False,timeout=5) as client:
            process=start()
            headers={'X-PostHub-Request':'1'}
            result=client.post('/api/login',json={'password':'test-password-12345'},headers=headers)
            headers['X-CSRF-Token']=result.json()['csrf']
            target=client.post('/api/admin/targets',json={'name':'restart test','kind':'webhook','config':{'url':f'http://127.0.0.1:{receiver_port}'}},headers=headers).json()['id']
            client.post('/api/admin/targets/'+target+'/verify',headers=headers).raise_for_status()
            for _ in range(40):
                if received:break
                time.sleep(.1)
            assert received
            code=re.search(r'验证码：(\d{6})',received[0]['body'])[1]
            client.post('/api/admin/targets/'+target+'/confirm',json={'code':code},headers=headers).raise_for_status()
            scheduled=datetime.fromtimestamp(time.time()+5,timezone.utc).isoformat()
            result=client.post('/api/admin/messages',json={'title':'restart-proof','body':'must arrive once','target_ids':[target],'request_id':'restart-proof','scheduled_at':scheduled},headers=headers)
            assert result.status_code==202
            mid=result.json()['id']
            process.terminate();process.wait(timeout=10)
            process=start()
            duplicate=subprocess.Popen(command[:-1]+[str(port+1)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            processes.append(duplicate)
            assert duplicate.wait(timeout=10)!=0,'second process must reject same data directory'
            for _ in range(100):
                item=client.get('/api/admin/messages/'+mid).json()
                if item['deliveries'][0]['state']=='accepted':break
                time.sleep(.1)
            assert item['deliveries'][0]['state']=='accepted'
            assert len([m for m in received if m['title']=='restart-proof'])==1
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate();process.wait(timeout=10)
