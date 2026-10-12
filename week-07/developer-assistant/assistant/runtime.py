"""Start only an explicitly configured local runtime; borrowed services are preserved."""
import asyncio
import os
import subprocess
import httpx

class LocalRuntime:
    def __init__(self,settings):
        self.settings=settings;self.process=None;self.lock=asyncio.Lock();self.log=None

    async def ready(self):
        headers={'Authorization':'Bearer '+self.settings.local_key} if self.settings.local_key else {}
        try:
            async with httpx.AsyncClient(timeout=3) as c:
                r=await c.get(self.settings.local_url+'/models',headers=headers)
                if r.status_code==200:
                    models=r.json().get('data',[])
                    return any(x.get('id')==self.settings.local_model for x in models)
        except Exception:
            pass
        return False

    async def ensure(self):
        async with self.lock:
            if await self.ready():
                return
            cfg=self.settings.runtime
            if not cfg.get('executable') or not cfg.get('model'):
                raise ValueError('Local provider is unavailable. Configure local-data/settings.json or start its server.')
            from pathlib import Path
            if not Path(cfg['executable']).is_file() or not Path(cfg['model']).is_file():
                raise ValueError('Configured local runtime or model file is missing')
            if self.process and self.process.poll() is not None:
                self.process=None
            if self.process is None:
                from urllib.parse import urlsplit
                import socket
                port=urlsplit(self.settings.local_url).port
                with socket.socket() as s:
                    if s.connect_ex(('127.0.0.1',port))==0:
                        raise ValueError('Local port is occupied by an unavailable or different service; it will not be stopped')
                argv=[cfg['executable'],'--model',cfg['model'],'--alias',self.settings.local_model,'--host','127.0.0.1','--port',str(port),*cfg.get('args',[])]
                env=os.environ.copy()
                if self.settings.local_key:
                    env['LLAMA_ARG_API_KEY']=self.settings.local_key
                for k in tuple(env):
                    if k in {'DEEPSEEK_API_KEY','TAVILY_API_KEY','TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID','LOCAL_LLM_API_KEY'}:
                        env.pop(k,None)
                self.log=open(self.settings.data/'runtime.log','ab')
                self.process=subprocess.Popen(argv,env=env,stdout=self.log,stderr=self.log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            for _ in range(240):
                if await self.ready():
                    return
                if self.process.poll() is not None:
                    raise ValueError('Owned local runtime failed to start. Inspect the local runtime log.')
                await asyncio.sleep(1)
            raise ValueError('Local model startup exceeded the bounded wait')

    async def close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(asyncio.to_thread(self.process.wait),timeout=10)
            except asyncio.TimeoutError:
                self.process.kill();await asyncio.to_thread(self.process.wait)
        if self.log:
            self.log.close()
