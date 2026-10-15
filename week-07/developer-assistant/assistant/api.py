"""Loopback API and static UI for the independent prototype."""
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
import asyncio
import json
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict,model_validator
from .config import Settings,MODULE
from .store import Store
from .runtime import LocalRuntime
from .provider import Provider
from .service import Service
from .tools import Executor,project_call
from .scope import ProjectScope
from .scheduler import Scheduler

class Body(BaseModel):
    model_config=ConfigDict(extra='forbid')
class ProjectBody(Body):
    root: str = Field(min_length=1,max_length=512)
    name: str = Field(default='',max_length=80)
class TaskBody(Body):
    project_id: str
    question: str = Field(min_length=1,max_length=4000)
    provider: Literal['local','deepseek']='local'
    mode: Literal['help','web','files']='help'
    allow_tools: bool=False
    chat_id: str | None = Field(default=None,max_length=64)
    files_context_chars: int | None=Field(default=None,strict=True)
    @model_validator(mode='after')
    def files_budget(self):
        if self.files_context_chars is not None and (self.mode!='files' or self.files_context_chars not in (40000,100000)):raise ValueError('Only Project files may explicitly select integer40000 or integer100000')
        return self
class ChatBody(Body):
    title: str = Field(default='New chat',min_length=1,max_length=80)
class ToolBody(Body):
    name: Literal['project_tree','read_project_file','search_project_text','run_project_tests']
    arguments: dict = Field(default_factory=dict)
class UploadBody(Body):
    name: str = Field(min_length=1,max_length=100)
    content: str = Field(max_length=300000)

class ScheduleBody(TaskBody):
    due: str = Field(max_length=80)
    interval_minutes: int = Field(default=0,ge=0,le=10080)
    telegram: bool=False
class ScheduleToggle(Body):
    enabled: bool
class ReviewConnect(Body):
    repository: str=Field(min_length=3,max_length=200)
    scope: str=Field(default='week-07/developer-assistant',max_length=250)
class ReviewBody(Body):
    number: int=Field(ge=1,le=10000000)
    provider: Literal['local','deepseek']='local'
    chat_id: str | None=None
    telegram: bool=False
    repository: str | None=Field(default=None,max_length=200)

class FilesBinding(Body):
    chat_id: str = Field(min_length=1,max_length=64)
class FilesReview(FilesBinding):
    revision: int = Field(ge=0,strict=True)
    digest: str = Field(min_length=64,max_length=64)
class FilesEdit(FilesBinding):
    path: str = Field(min_length=1,max_length=240)
    content: str = Field(max_length=512000)
    expected_hash: str = Field(min_length=1,max_length=64)

def create_app(settings=None):
    s=settings or Settings.load()
    store=Store(s.data/'assistant.sqlite3')
    default=store.project(MODULE,'Developer Assistant')
    runtime=LocalRuntime(s);service=Service(store,s,Provider(s,runtime))
    scheduler=Scheduler(store,service)
    from .review_service import Reviews
    reviews=Reviews(service)
    @asynccontextmanager
    async def lifespan(app):
        scheduler.start()
        try:
            yield
        finally:
            await scheduler.close()
            await service.close();await runtime.close()
    app=FastAPI(title='Developer Assistant',version='1.0.0',lifespan=lifespan)
    app.state.store=store;app.state.service=service;app.state.settings=s;app.state.runtime=runtime
    @app.middleware('http')
    async def loopback_boundary(request: Request,call_next):
        host=request.url.hostname
        origin=request.headers.get('origin')
        if host not in {'127.0.0.1','localhost','::1','testserver'}:
            return JSONResponse({'error':'Loopback host required'},status_code=403)
        if origin and (urlsplit(origin).hostname not in {'127.0.0.1','localhost','::1'} or urlsplit(origin).port!=request.url.port):
            return JSONResponse({'error':'Cross-origin request rejected'},status_code=403)
        return await call_next(request)
    from .maintenance import Conflict,WorkspaceLease,run_guard,new_files_budget
    @app.exception_handler(Conflict)
    async def files_conflict(request,e):return JSONResponse({'error':str(e)},status_code=409)
    @app.exception_handler(ValueError)
    async def value_error(request,e):
        return JSONResponse({'error':s.scrub(str(e))},status_code=400)
    @app.exception_handler(OSError)
    async def os_error(request,e):
        return JSONResponse({'error':'Project file operation unavailable'},status_code=400)
    def idle():
        if service.busy():
            raise HTTPException(409,'Wait for active task or Stop before changing project documents')
    @app.get('/api/projects/{pid}/reviews/connection')
    def review_connection(pid:str):return reviews.connection(pid)
    @app.post('/api/projects/{pid}/reviews/connect')
    async def review_connect(pid:str,b:ReviewConnect):
        idle();return await reviews.connect(pid,b.repository,b.scope)
    @app.post('/api/projects/{pid}/reviews/authenticate')
    async def review_authenticate(pid:str):
        idle();return await reviews.authenticate(pid)
    @app.get('/api/projects/{pid}/reviews/pulls')
    async def review_pulls(pid:str):return await reviews.list(pid)
    @app.post('/api/projects/{pid}/reviews')
    async def review_create(pid:str,b:ReviewBody):
        if b.repository is not None and b.repository!=reviews.connection(pid)['repository']:raise ValueError('Review repository changed')
        return reviews.create(pid,b.number,b.provider,b.chat_id,b.telegram)
    @app.post('/api/projects/{pid}/reviews/sync')
    async def review_sync(pid:str,b:ReviewBody):return await reviews.sync(pid,b.number,b.chat_id,b.telegram,b.repository)
    @app.get('/api/projects/{pid}/schedules')
    def schedules(pid:str):
        store.get_project(pid);return {'schedules':scheduler.list(pid)}
    @app.post('/api/schedules')
    def schedule(b:ScheduleBody):
        if b.allow_tools or b.mode=='files': raise ValueError('Scheduled runs use the bounded default workflow')
        return scheduler.create(b.project_id,b.question,b.provider,b.due,b.interval_minutes,b.telegram,b.mode,b.chat_id)
    @app.post('/api/schedules/{sid}')
    def toggle_schedule(sid:str,b:ScheduleToggle):
        return scheduler.toggle(sid,b.enabled)
    @app.get('/api/projects/{pid}/chats')
    def chats(pid:str,include_archived:bool=False):
        return {'chats':store.chats(pid,include_archived)}
    @app.post('/api/projects/{pid}/chats')
    async def new_chat(pid:str,b:ChatBody):
        return store.create_chat(pid,s.scrub(b.title))
    @app.get('/api/chats/{cid}')
    def chat(cid:str): return store.chat(cid)
    @app.post('/api/chats/{cid}/rename')
    async def rename_chat(cid:str,b:ChatBody):
        return store.rename_chat(cid,s.scrub(b.title))
    @app.post('/api/chats/{cid}/archive')
    async def archive_chat(cid:str): return store.archive_chat(cid)
    @app.post('/api/chats/{cid}/restore')
    async def restore_chat(cid:str): return store.archive_chat(cid,False)
    @app.get('/api/chats/{cid}/tasks')
    def chat_tasks(cid:str,before:int | None=Query(default=None,ge=1),limit:int=Query(default=50,ge=1,le=200)):
        return store.chat_tasks(cid,before,limit)
    @app.get('/api/support')
    async def support():
        from .launch import support_status
        return await asyncio.to_thread(support_status)
    @app.get('/api/health')
    def health():
        return {'application':'developer-assistant','version':'1.0.0','status':'ok'}
    @app.get('/api/config')
    def config():
        value=s.public()
        try:value['files_context_default_chars']=new_files_budget(s.context_chars)['profile_default_chars']
        except ValueError:value['files_context_default_chars']=None
        return value
    @app.get('/api/projects')
    def projects():
        return {'projects':store.projects(),'default_id':default['id']}
    @app.post('/api/projects')
    def add_project(b:ProjectBody):
        idle();return store.project(b.root,b.name or None)
    @app.post('/api/projects/{pid}/index')
    def index(pid:str):
        idle();return store.index(pid)
    @app.get('/api/projects/{pid}/index')
    def manifest(pid:str):
        store.get_project(pid);return store.manifest(pid)
    @app.post('/api/projects/{pid}/documents')
    def upload(pid:str,b:UploadBody):
        idle();scope=ProjectScope(store.get_project(pid)['root'])
        if Path(b.name).name!=b.name or not scope.allowed('docs/imported/'+b.name) or Path(b.name).suffix.lower() not in {'.md','.txt','.json','.yaml','.yml'}:
            raise ValueError('Only named nonsecret documentation text files are allowed')
        folder=scope.mkdir('docs/imported')
        target=folder/b.name
        with target.open('x',encoding='utf-8') as f:
            f.write(s.scrub(b.content))
        return {'saved':'docs/imported/'+b.name,'needs_reindex':True}
    @app.get('/api/projects/{pid}/sources/{cid}')
    def source(pid:str,cid:str):
        with store.connect() as c:
            r=c.execute('SELECT * FROM chunks WHERE project_id=? AND id=?',(pid,cid)).fetchone()
        if not r:
            raise ValueError('Unknown indexed source')
        return dict(r)
    @app.get('/api/projects/{pid}/mcp')
    async def mcp(pid:str):
        return await project_call(store.get_project(pid)['root'],'git_branch')
    @app.post('/api/projects/{pid}/tools')
    async def tools(pid:str,b:ToolBody):
        idle();project=store.get_project(pid)
        executor=Executor(project['root'],store,pid,s,max_calls=1)
        return await executor.execute(b.name,b.arguments)
    @app.get('/api/projects/{pid}/tasks')
    def tasks(pid:str):
        store.get_project(pid);return {'tasks':store.tasks(pid)}
    @app.post('/api/tasks')
    async def task(b:TaskBody):
        return service.create(b.project_id,b.question,b.provider,b.mode,b.allow_tools,b.chat_id,b.files_context_chars)
    @app.get('/api/tasks/{tid}')
    def get_task(tid:str):
        return store.task(tid)
    @app.post('/api/tasks/{tid}/stop')
    async def stop(tid:str):
        return service.cancel(tid)
    @app.post('/api/tasks/{tid}/retry')
    async def retry(tid:str,request:Request):
        raw=await request.body()
        if raw:
            try:body=json.loads(raw)
            except ValueError:raise ValueError('Retry accepts no override body') from None
            if body!={}:raise ValueError('Retry preserves its recorded budget; submit a new goal to change it')
        old=store.task(tid)
        if old['status'] in {'queued','running'}:
            raise ValueError('Stop active task before retry')
        if old['mode']=='files':return service.maintenance.retry(old)
        if old['mode']=='review':
            req=old.get('review_request')
            if not req:raise ValueError('Imported CI report cannot be retried; select PR for a manual review')
            return reviews.create(old['project_id'],req['number'],old['provider'],old.get('chat_id'),req.get('telegram',False))
        return service.create(old['project_id'],old['question'],old['provider'],old['mode'],old.get('allow_tools',False),old.get('chat_id'))
    @app.post('/api/tasks/{tid}/telegram')
    async def telegram(tid:str):
        t=store.task(tid)
        if t.get('mode')=='review':
            if t.get('review',{}).get('origin')=='ci':return dict(t['review'].get('notification',{'status':'not_recorded'}),duplicate_prevented=True)
            return await reviews.notify(t)
        return await service.report(tid)
    def file_run(pid,tid,cid):
        t=store.task(tid)
        if t['project_id']!=pid or t.get('chat_id')!=cid:raise Conflict('Stale project/chat/task binding')
        return service.maintenance.run_for(t)
    def refresh_files(tid,run):
        t=store.task(tid);t['maintenance']=run.public();store.save_task(t);return t['maintenance']
    @app.get('/api/projects/{pid}/files/{tid}')
    def files_manifest(pid:str,tid:str,chat_id:str):return file_run(pid,tid,chat_id).public()
    @app.get('/api/projects/{pid}/files/{tid}/read')
    def files_read(pid:str,tid:str,chat_id:str,path:str,start_line:int=Query(1,ge=1),count:int=Query(160,ge=1,le=200),continuation:str|None=None):
        return file_run(pid,tid,chat_id).call('read_file',dict(path=path,start_line=start_line,count=count,continuation=continuation))
    @app.get('/api/projects/{pid}/files/{tid}/diff')
    def files_diff(pid:str,tid:str,chat_id:str,cursor:str|None=None):return file_run(pid,tid,chat_id).call('get_diff',dict(cursor=cursor))
    @app.post('/api/projects/{pid}/files/{tid}/edit')
    def files_edit(pid:str,tid:str,b:FilesEdit):
        idle();run=file_run(pid,tid,b.chat_id)
        with WorkspaceLease(run.m['root']),run_guard(run.folder):run.write(b.path,b.content,b.expected_hash)
        return refresh_files(tid,run)
    @app.post('/api/projects/{pid}/files/{tid}/checks')
    def files_checks(pid:str,tid:str,b:FilesBinding):
        idle();run=file_run(pid,tid,b.chat_id)
        run.call('run_fixed_checks',{'suite':'static'});return refresh_files(tid,run)
    @app.post('/api/projects/{pid}/files/{tid}/review')
    def files_review(pid:str,tid:str,b:FilesReview):
        idle();run=file_run(pid,tid,b.chat_id)
        with WorkspaceLease(run.m['root']),run_guard(run.folder):run.review(b.revision,b.digest)
        return refresh_files(tid,run)
    @app.post('/api/projects/{pid}/files/{tid}/apply')
    def files_apply(pid:str,tid:str,b:FilesReview):
        idle();run=file_run(pid,tid,b.chat_id);run.transact(b.revision,b.digest);return refresh_files(tid,run)
    @app.post('/api/projects/{pid}/files/{tid}/undo')
    def files_undo(pid:str,tid:str,b:FilesBinding):
        idle();run=file_run(pid,tid,b.chat_id);run.undo();return refresh_files(tid,run)
    @app.post('/api/projects/{pid}/files/{tid}/recover')
    def files_recover(pid:str,tid:str,b:FilesBinding):
        idle();run=file_run(pid,tid,b.chat_id);run.recover();return refresh_files(tid,run)
    @app.get('/')
    def home():
        return FileResponse(MODULE/'static/index.html')
    app.mount('/static',StaticFiles(directory=MODULE/'static'),name='static')
    return app

