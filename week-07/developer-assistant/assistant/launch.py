"""Trusted human entrypoint; independent Support survives the Main process."""
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
import json
from pathlib import Path
from urllib.request import urlopen
import uvicorn

def support_port():
    p=int(os.getenv('ASSISTANT_SUPPORT_PORT','8793'))
    if not 1024<=p<=65535 or p==8792:raise ValueError('Invalid Support port; Local default port is reserved')
    return p

def occupied(port):
    with socket.socket() as s:return s.connect_ex(('127.0.0.1',port))==0

def support_status():
    from .support_api import data_path,data_identity
    url=f'http://127.0.0.1:{support_port()}'
    try:
        with urlopen(url+'/api/health',timeout=1) as r:health=json.loads(r.read(2048))
        good=health.get('application')=='developer-assistant-support' and health.get('data_identity')==data_identity(data_path())
        return {'available':good,'url':url if good else None,'reason':None if good else 'Port is occupied by a different service or Support data identity'}
    except Exception:return {'available':False,'url':None,'reason':'Support is not available; no unknown process was stopped'}

def ensure_support():
    from .support_api import data_path
    from .config import MODULE
    port=support_port()
    if occupied(port):return support_status()
    folder=data_path();folder.mkdir(parents=True,exist_ok=True)
    env=dict(os.environ,ASSISTANT_MODE='support',ASSISTANT_PORT=str(port),ASSISTANT_NO_BROWSER='1')
    with (folder/'service.log').open('ab') as log:
        subprocess.Popen([sys.executable,'-m','assistant.launch'],cwd=MODULE,env=env,stdout=log,stderr=log,
                         creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0)|getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0),start_new_session=os.name!='nt')
    for _ in range(40):
        state=support_status()
        if state['available']:return state
        time.sleep(.1)
    return support_status()

def main():
    mode=os.getenv('ASSISTANT_MODE','main')
    try:
        if mode not in {'main','support'}:raise ValueError('ASSISTANT_MODE must be main or support')
        port=int(os.getenv('ASSISTANT_PORT',str(support_port()) if mode=='support' else '8791'))
        if not 1024<=port<=65535:raise ValueError('Invalid application port')
        if occupied(port):
            print('Application port is already in use; no existing process was stopped.');return 2
        if mode=='main':
            state=ensure_support()
            if not state['available']:print('Support unavailable: '+state['reason'])
    except (ValueError,OSError):
        print('Application launch configuration is invalid or Support could not start; no existing process was stopped.');return 2
    if os.getenv('ASSISTANT_NO_BROWSER')!='1':threading.Timer(1.5,lambda:webbrowser.open(f'http://127.0.0.1:{port}')).start()
    factory='assistant.support_api:create_app' if mode=='support' else 'assistant.api:create_app'
    uvicorn.run(factory,factory=True,host='127.0.0.1',port=port,log_level='warning',access_log=False)
    return 0

if __name__=='__main__':raise SystemExit(main())
