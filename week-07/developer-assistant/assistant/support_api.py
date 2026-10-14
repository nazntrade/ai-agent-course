"""Independent manual-first Support Desk API and static chat UI."""
from contextlib import asynccontextmanager
import hashlib
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from fastapi import FastAPI,Request,HTTPException
from fastapi.responses import JSONResponse,FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,ConfigDict,Field
from .config import Settings,MODULE
from .provider import Provider
from .runtime import LocalRuntime
from .support_store import SupportStore,Conflict,safe_text
from .support_rag import SupportRag
from .support_service import SupportService

def data_path(settings=None):
    base=settings.data if settings else Path(os.getenv('ASSISTANT_DATA',str(MODULE/'local-data')))
    return Path(os.getenv('ASSISTANT_SUPPORT_DATA',str(base/'support'))).resolve()

def data_identity(path):return hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest()[:24]

class DataLease:
    """A process-scoped OS file lock prevents two Support writers/recovery races."""
    def __init__(self,path):
        path.mkdir(parents=True,exist_ok=True);self.file=(path/'service.lock').open('a+b')
        self.file.seek(0);self.file.write(b'1');self.file.flush();self.file.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.file.close();raise ValueError('Another Support process owns this data; it was not stopped') from None
    def close(self):self.file.close()

class Body(BaseModel):model_config=ConfigDict(extra='forbid')
class Persona(Body):user: str=Field(min_length=1,max_length=64)
class Diagnostics(Body):
    component: Literal['unknown','deepseek','local','github','rag','chat','telegram']='unknown'
    status_code: int|None=Field(default=None,ge=100,le=599)
    error_code: str=Field(default='',max_length=160)
    connection_state: str=Field(default='',max_length=160)
    observed_at: str=Field(default='',max_length=160)
    attempted_steps: list[str]=Field(default_factory=list,max_length=8)
class NewTicket(Body):
    title: str=Field(min_length=1,max_length=120)
    description: str=Field(min_length=1,max_length=4000)
    component: Literal['unknown','deepseek','local','github','rag','chat','telegram']='unknown'
    diagnostics: Diagnostics=Field(default_factory=Diagnostics)
class Version(Body):context_version:int=Field(ge=1)
class Update(Version):diagnostics:Diagnostics
class Ask(Version):
    question:str=Field(min_length=1,max_length=4000)
    provider:Literal['local','deepseek']='local'
class ChangeState(Version):state:Literal['open','resolved','escalated']
class Notify(Version):
    action:Literal['answer','escalation']
    run_id:str|None=Field(default=None,min_length=1,max_length=64)

def create_app(settings=None,provider=None,root=MODULE):
    configuration_error=None
    if settings is None:
        try:settings=Settings.load()
        except Exception:configuration_error='AI configuration is invalid; manual FAQ and tickets are available.'
    path=data_path(settings);lease=DataLease(path)
    def scrub(text):
        if settings:return settings.scrub(text)
        for name in ('DEEPSEEK_API_KEY','LOCAL_LLM_API_KEY','TAVILY_API_KEY','TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID','GITHUB_TOKEN'):
            v=os.getenv(name)
            if v:text=str(text).replace(v,'[redacted]')
        return text
    try:
        store=SupportStore(path,Path(root)/'examples/support/users-tickets.json',scrub)
        rag=SupportRag(store,root)
        runtime=LocalRuntime(settings) if settings else None
        service=SupportService(store,rag,settings,provider or (Provider(settings,runtime) if settings else None))
    except BaseException:
        lease.close();raise
    @asynccontextmanager
    async def lifespan(app):
        try:yield
        finally:
            await service.close()
            if runtime:await runtime.close()
            lease.close()
    app=FastAPI(title='Developer Assistant Support Desk',version='1.0.0',lifespan=lifespan)
    app.state.store=store;app.state.rag=rag;app.state.service=service;app.state.runtime=runtime;app.state.lease=lease
    @app.middleware('http')
    async def boundary(req:Request,next):
        origin=req.headers.get('origin');host=req.url.hostname
        if host not in {'localhost','127.0.0.1','::1','testserver'}:return JSONResponse({'error':'Loopback host required'},403)
        if origin:
            u=urlsplit(origin)
            if u.scheme!=req.url.scheme or u.hostname!=host or u.port!=req.url.port:return JSONResponse({'error':'Cross-origin request rejected'},403)
        return await next(req)
    @app.exception_handler(ValueError)
    async def failed(req,e):return JSONResponse({'error':scrub(str(e)),**({'session_changed':True} if isinstance(e,Conflict) and str(e).startswith('Demo session changed') else {})},409 if isinstance(e,Conflict) else 400)
    @app.exception_handler(OSError)
    async def io_error(req,e):return JSONResponse({'error':'Support storage operation unavailable'},400)
    def owner(req,verify_expected=True):
        try:actual=store.owner(req.cookies.get('support_session',''))
        except ValueError:raise HTTPException(401,'Choose a synthetic demo persona first') from None
        expected=req.headers.get('x-support-persona')
        if verify_expected and expected and expected!=actual:
            raise Conflict('Demo session changed in another tab; select the persona again')
        return actual
    @app.get('/api/health')
    def health():return {'application':'developer-assistant-support','version':'1.0.0','status':'ok','data_identity':data_identity(path)}
    @app.get('/api/config')
    def config():
        return {'synthetic_demo':True,'ai_available':service.provider is not None,'configuration_error':configuration_error,
                'providers':settings.public() if settings else None,'faq':rag.manifest()}
    @app.get('/api/personas')
    def personas():return {'users':store.users(),'notice':'Synthetic local demonstration; persona selection is not production authentication.'}
    @app.post('/api/session')
    def session(b:Persona):
        sid=store.session(b.user);response=JSONResponse({'user':b.user,'synthetic':True})
        response.set_cookie('support_session',sid,httponly=True,samesite='strict',max_age=86400,path='/')
        return response
    @app.get('/api/session')
    def current_session(req:Request):return {'user':owner(req,False),'synthetic':True}
    @app.get('/api/tickets')
    def tickets(req:Request):return {'tickets':store.tickets(owner(req))}
    @app.post('/api/tickets')
    def create(req:Request,b:NewTicket):return store.create(owner(req),b.title,b.description,b.component,b.diagnostics.model_dump())
    @app.get('/api/tickets/{tid}')
    def ticket(req:Request,tid:str):return store.ticket(owner(req),tid)
    @app.post('/api/tickets/{tid}/diagnostics')
    def update(req:Request,tid:str,b:Update):return store.update(owner(req),tid,b.context_version,b.diagnostics.model_dump())
    @app.post('/api/tickets/{tid}/state')
    def state(req:Request,tid:str,b:ChangeState):return store.state(owner(req),tid,b.context_version,b.state)
    @app.post('/api/tickets/{tid}/ask')
    async def ask(req:Request,tid:str,b:Ask):return service.create(owner(req),tid,b.context_version,b.question,b.provider)
    @app.get('/api/runs/{rid}')
    def run(req:Request,rid:str):return store.run(owner(req),rid)
    @app.get('/api/runs/{rid}/sources/{cid}')
    def source(req:Request,rid:str,cid:str):
        r=store.run(owner(req),rid)
        found=next((x for x in r['sources'] if x['id']==cid),None)
        if not found:raise ValueError('Source unavailable for this answer')
        return found
    @app.post('/api/runs/{rid}/stop')
    async def stop(req:Request,rid:str):return service.cancel(owner(req),rid)
    @app.post('/api/runs/{rid}/retry')
    async def retry(req:Request,rid:str,b:Version):
        user=owner(req);r=store.run(user,rid)
        if r['status'] in {'queued','running'}:raise Conflict('Stop the active answer before retrying')
        return service.create(user,r['ticket'],b.context_version,r['question'],r['provider'])
    @app.post('/api/tickets/{tid}/telegram')
    async def telegram(req:Request,tid:str,b:Notify):return await service.notify(owner(req),tid,b.context_version,b.action,b.run_id)
    @app.get('/api/faq')
    def faq(q:str=''):
        if len(q)>4000:raise ValueError('FAQ query is too long')
        return {'manifest':rag.manifest(),'articles':rag.retrieve(q) if q.strip() else rag.articles()}
    @app.post('/api/faq/reindex')
    def reindex(req:Request):
        owner(req)
        if any(not j.done() for j in service.jobs.values()):raise Conflict('Wait or Stop before reindexing Support FAQ')
        return rag.index()
    @app.get('/')
    def home():return FileResponse(MODULE/'static/support/index.html')
    app.mount('/static',StaticFiles(directory=MODULE/'static/support'),name='support-static')
    return app
