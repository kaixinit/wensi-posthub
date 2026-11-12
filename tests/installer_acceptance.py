"""Black-box package install/upgrade; only local HTTP recipients and private fixtures."""
import argparse
from collections import Counter
from contextlib import contextmanager, closing
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from zipfile import ZipFile

import httpx

FLAGS = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
ENV = os.environ | {'PYTHONIOENCODING':'utf-8', 'PIP_DISABLE_PIP_VERSION_CHECK':'1'}
ENV.pop('PYTHONPATH', None)
ENV.pop('POSTHUB_DATA_DIR', None)


def command(args, cwd, expected=0):
    if Path(args[0]).name.lower() in {'python', 'python.exe'}:
        args=[args[0],'-X','utf8',*args[1:]]
    result = subprocess.run([str(a) for a in args],cwd=cwd,env=ENV,text=True,encoding='utf-8',errors='replace',capture_output=True,creationflags=FLAGS,timeout=300)
    if (result.returncode == 0) != (expected == 0):
        safe = re.sub(r'初始密码：[^\r\n]*','初始密码：[redacted]',result.stdout+result.stderr)
        raise AssertionError('Command failed: '+safe[-3500:])
    return result.stdout + result.stderr


def snapshot(data):
    with closing(sqlite3.connect(data/'posthub.sqlite3')) as conn:
        names=[r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        result={name:conn.execute('SELECT * FROM "'+name+'" ORDER BY rowid').fetchall() for name in names}
        result['_schema']=conn.execute('PRAGMA user_version').fetchone()[0]
    result['_config']=hashlib.sha256((data/'config.json').read_bytes()).hexdigest()
    return result


@contextmanager
def service(python, data, package, http_timeout=5):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    if os.name=='nt':
        argv=['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(package/'start.ps1'),'-Port',str(port)]
    else:
        argv=['bash',str(package/'start.sh'),'--port',str(port)]
    state=json.loads((package/'installation.json').read_text(encoding='utf-8'))
    assert Path(state['data_dir'])==data,'Launcher must use saved data directory'
    process=subprocess.Popen(argv,cwd=package,env=ENV,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=FLAGS,start_new_session=os.name!='nt')
    try:
        with httpx.Client(base_url=f'http://127.0.0.1:{port}',timeout=http_timeout,trust_env=False) as client:
            for _ in range(100):
                assert process.poll() is None,'Installed service exited'
                try:
                    if client.get('/healthz').status_code==200:break
                except httpx.HTTPError:pass
                time.sleep(.1)
            else:raise AssertionError('Installed service failed to start')
            yield client
    finally:
        if os.name=='nt':
            subprocess.run(['taskkill.exe','/PID',str(process.pid),'/T','/F'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=FLAGS,check=False)
        else:
            os.killpg(process.pid,signal.SIGTERM)
        process.wait(timeout=15)


def login(client,password):
    response=client.post('/api/login',json={'username':'admin','password':password},headers={'X-PostHub-Request':'1'})
    assert response.status_code==200
    return {'X-PostHub-Request':'1','X-CSRF-Token':response.json()['csrf']}


def run(args):
    work=args.work_dir.resolve();work.mkdir(parents=True,exist_ok=False)
    out=args.artifacts.resolve();out.mkdir(parents=True,exist_ok=True)
    with ZipFile(args.package) as archive:
        assert len([n for n in archive.namelist() if n.endswith('.whl')])==1
        assert not any('/tests/' in n or '/artifacts/' in n or '/.venv/' in n for n in archive.namelist())
        archive.extractall(work)
    package=work/'posthub';python=package/'.venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    def installer(action,data=None,expected=0):
        if os.name=='nt':
            argv=['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',package/(action+'.ps1')]
            if data:argv+=['-DataDir',data]
        else:
            argv=['bash',package/(action+'.sh')]
            if data:argv+=['--data-dir',data]
        return command(argv,package,expected)
    installed=installer('install')
    initial=re.search(r'初始密码：([^\r\n]+)',installed)[1]
    data=package/'data'
    with service(python,data,package,args.http_timeout) as client:
        assert client.get('/api/admin/state').status_code==401
        headers=login(client,initial)
        response=client.post('/api/admin/account/password',headers=headers,json={'current_password':initial,'new_password':'installer-acceptance-password','confirm_password':'installer-acceptance-password'})
        assert response.status_code==200
        headers=login(client,'installer-acceptance-password')
        for asset in ['/','/setup.js','/recovery.js','/help-data.js','/fonts/NotoSansSC-ui.woff2','/fonts/NotoSansSC-variable.woff2']:
            assert client.get(asset).status_code==200,asset
        before=(data/'config.json').read_bytes()
        refused=installer('upgrade',data,expected=1)
        assert '正在运行' in refused and (data/'config.json').read_bytes()==before
        assert not (data/'backups').exists()
    dependency_report=command([python,'-I','-m','pip','list','--format=json'],package)
    names={p['name'].lower() for p in json.loads(dependency_report)}
    assert not names & {'pytest','aiosmtpd','playwright','iniconfig','pluggy'}
    command([python,'-I','-m','pip','check'],package)
    received=[];fail_partial=[True]
    class Receiver(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200);self.end_headers();self.wfile.write(json.dumps(received).encode())
        def do_POST(self):
            item=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path=='/b' and item['title'].endswith('-partial') and fail_partial[0]:
                self.send_response(429);self.end_headers();return
            received.append(item|{'endpoint':self.path})
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(b'{"accepted":true}')
        def log_message(self,*args):pass
    receiver=ThreadingHTTPServer(('127.0.0.1',0),Receiver)
    thread=threading.Thread(target=receiver.serve_forever,daemon=True);thread.start()
    results={'fresh_install':'passed','first_login_change_password':'passed','runtime_dependencies_only':True,'running_upgrade_refused':'passed','cases':[],'external_notifications':0,'http_timeout_seconds':args.http_timeout}
    try:
        for index,wheel in enumerate([None,*args.old_wheel]):
            case='same-version' if wheel is None else 'upgrade-'+wheel.name.split('-')[1]
            if wheel:
                command([python,'-I','-m','pip','install','--no-deps','--force-reinstall',wheel.resolve()],package)
                data=work/(case+'-data')
            fail_partial[0]=True
            fixture_file=work/(case+'-fixture.json')
            command([python,'-I',Path(__file__).with_name('installer_seed.py').resolve(),data,f'http://127.0.0.1:{receiver.server_port}',case,fixture_file],package)
            fixture=json.loads(fixture_file.read_text(encoding='utf-8'))
            before=snapshot(data)
            installer('upgrade',data)
            after=snapshot(data)
            assert after['_schema']==10
            for table,rows in before.items():
                if table!='_schema':assert after[table]==rows,table
            state=json.loads((package/'installation.json').read_text(encoding='utf-8'))
            assert Path(state['data_dir'])==data
            assert snapshot(Path(state['backup']))==before,'Paired backup differs from old database/config'
            fail_partial[0]=False
            with service(python,data,package,args.http_timeout) as client:
                assert client.get('/healthz').json()['version']=='0.1.25'
                headers=login(client,'installer-acceptance-password')
                for _ in range(700):
                    rows=[client.get('/api/admin/messages/'+fixture[mid],headers=headers).json() for mid in ['partial','queued']]
                    if all(all(d['state']=='accepted' for d in row['deliveries']) for row in rows):break
                    time.sleep(.1)
                else:raise AssertionError('Pending/retry messages did not resume')
                payload={'title':case+'-partial','body':'Isolated installation acceptance only','request_id':case+'-partial','target_ids':fixture['targets']}
                reply=client.post('/api/v1/messages',headers={'Authorization':'Bearer '+fixture['api_key']},json=payload)
                assert reply.status_code==202 and reply.json()['deduplicated'] and reply.json()['id']==fixture['partial']
                forbidden=client.post('/api/v1/messages',headers={'Authorization':'Bearer '+fixture['other']['api_key']},json={'title':'scope','body':'fixture','request_id':'scope-check','target_ids':[fixture['targets'][1]]})
                assert forbidden.status_code==403
                assert client.get('/api/admin/messages/'+fixture['waiting'],headers=headers).json()['routing_state']=='awaiting_routing'
            with service(python,data,package,args.http_timeout) as client:
                login(client,'installer-acceptance-password');time.sleep(2)
            counts=Counter((m['title'],m['endpoint']) for m in received if m['title'].startswith(case+'-'))
            assert counts==Counter({(case+'-history','/a'):1,(case+'-partial','/a'):1,(case+'-partial','/b'):1,(case+'-queued','/a'):1}),dict(counts)
            results['cases'].append({'from':fixture['version'],'to':'0.1.25','schema_before':before['_schema'],'schema_after':after['_schema'],'config_and_existing_rows_preserved':True,'paired_backup_verified':True,'old_api_key_and_independent_scope':'passed','real_local_receipts':sum(counts.values()),'duplicates':0,'pending_retry_resumed':True,'cancelled_not_sent':True,'waiting_route_preserved':True})
            print('Passed: '+case,flush=True)
    finally:
        receiver.shutdown();receiver.server_close();thread.join(5)
    results['status']='passed'
    (out/'results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
    print(json.dumps(results,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--package',type=Path,required=True)
    parser.add_argument('--work-dir',type=Path,required=True)
    parser.add_argument('--artifacts',type=Path,required=True)
    parser.add_argument('--old-wheel',type=Path,action='append',default=[])
    parser.add_argument('--http-timeout',type=float,default=5,help='Per-request timeout; use 60 for slow software-emulated VMs. Product settings are unchanged.')
    run(parser.parse_args())
