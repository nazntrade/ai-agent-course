"""Actual trusted launcher preflight with TEMP state and no model/key."""
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
from urllib.request import urlopen,Request
from assistant.config import MODULE

def free_port():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]

def main():
    with tempfile.TemporaryDirectory() as d:
        port=free_port();base=f'http://127.0.0.1:{port}'
        env={k:v for k,v in os.environ.items() if not any(x in k.upper() for x in ('KEY','TOKEN','SECRET','TELEGRAM_CHAT'))}
        env.update(ASSISTANT_SKIP_ENV='1',ASSISTANT_NO_BROWSER='1',ASSISTANT_DATA=d,ASSISTANT_SUPPORT_DATA=str(Path(d)/'support'),ASSISTANT_MODE='support',ASSISTANT_PORT=str(port),PYTHONUTF8='1')
        log=open(Path(d)/'preflight.log','w')
        proc=subprocess.Popen([os.environ.get('COMSPEC','cmd.exe'),'/d','/c',str(MODULE/'run_app.bat')],cwd=MODULE,env=env,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        def get(path):
            with urlopen(base+path,timeout=2) as r:return json.loads(r.read())
        def post(path,body,cookie=None):
            headers={'Content-Type':'application/json'}
            if cookie:headers['Cookie']=cookie
            with urlopen(Request(base+path,data=json.dumps(body).encode(),headers=headers),timeout=2) as r:return json.loads(r.read()),r.headers.get('Set-Cookie')
        try:
            for _ in range(80):
                try:health=get('/api/health');break
                except Exception:
                    if proc.poll() is not None:raise RuntimeError('Trusted Support launcher exited before health')
                    time.sleep(.1)
            else:raise RuntimeError('Support launcher failed bounded startup')
            assert health['application']=='developer-assistant-support'
            assert get('/api/faq')['articles']
            _,cookie=post('/api/session',{'user':'demo-alex'})
            ticket,_=post('/api/tickets',{'title':'Trusted entrypoint request','description':'No model or key needed'},cookie.split(';')[0])
            assert ticket['state']=='open' and len(ticket['messages'])==1
            print(json.dumps({'preflight':'support-launch','health':health['application'],'faq':'available','manual_ticket':'created','model_calls':0,'data':'isolated_TEMP'}))
        finally:
            # Only this scenario's exact owned process tree is terminated.
            subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],capture_output=True)
            proc.wait(timeout=10);log.close()

if __name__=='__main__':main()
