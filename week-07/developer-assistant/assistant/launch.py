"""Human entrypoint; UI starts even when credentials/provider are unavailable."""
import os
import socket
import threading
import webbrowser
import uvicorn

def main():
    port=int(os.getenv('ASSISTANT_PORT','8791'))
    with socket.socket() as s:
        if s.connect_ex(('127.0.0.1',port))==0:
            print('Application port is already in use; no existing process was stopped.')
            return 2
    if os.getenv('ASSISTANT_NO_BROWSER')!='1':
        threading.Timer(1.5,lambda:webbrowser.open(f'http://127.0.0.1:{port}')).start()
    uvicorn.run('assistant.api:create_app',factory=True,host='127.0.0.1',port=port,log_level='warning',access_log=False)
    return 0

if __name__=='__main__':
    raise SystemExit(main())
