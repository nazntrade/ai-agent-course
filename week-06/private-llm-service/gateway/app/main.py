"""Private HTTP API; one durable worker and no HTTP administration."""
from contextlib import asynccontextmanager
import asyncio
import json
from pathlib import Path
import re
import secrets
import time

from fastapi import Depends,FastAPI,Header,Query,Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse,Response,StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials,HTTPBearer

from gateway.app.config import Settings
from gateway.app.database import DomainError,Store,TERMINAL
from gateway.app.models import (Conversation,ConversationPage,CreateConversation,ErrorResponse,Identity,
    MessagePage,RenameConversation,ServiceStatus,Snapshot,SubmitRequest)
from gateway.app.worker import Worker


class ProcessLock:
    def __init__(self,path):
        self.path,self.file=path,None

    def acquire(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.file=self.path.open("a+b")
        self.file.seek(0)
        self.file.write(b"0")
        self.file.flush()
        self.file.seek(0)
        try:
            import msvcrt
            msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
        except ImportError:
            import fcntl
            fcntl.flock(self.file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file=None
            raise ValueError("service_already_running") from None

    def release(self):
        if self.file:
            self.file.close()
            self.file=None


class ByteLimit:
    def __init__(self,app,limit):
        self.app,self.limit=app,limit

    async def __call__(self,scope,receive,send):
        if scope["type"]!="http":
            return await self.app(scope,receive,send)
        chunks=[]
        size=0
        while True:
            event=await receive()
            if event["type"]=="http.disconnect":
                return
            data=event.get("body",b"")
            size+=len(data)
            if size>self.limit:
                response=JSONResponse({"error":{"code":"body_too_large"}},status_code=413)
                return await response(scope,receive,send)
            chunks.append(data)
            if not event.get("more_body",False):
                break
        sent=False
        async def buffered():
            nonlocal sent
            if not sent:
                sent=True
                return {"type":"http.request","body":b"".join(chunks),"more_body":False}
            return await receive()
        await self.app(scope,buffered,send)


def create_app(settings:Settings,*,store=None,pool=None):
    database=store or Store(settings.data_dir/"chat.sqlite3",settings)
    worker=Worker(database,settings,pool)
    lock=ProcessLock(settings.data_dir/"service.lock")
    streams={}
    shutdown_lock=asyncio.Lock()

    async def shutdown():
        async with shutdown_lock:
            await worker.stop()

    @asynccontextmanager
    async def lifespan(app):
        lock.acquire()
        try:
            await worker.start()
            yield
        finally:
            await shutdown()
            database.close()
            lock.release()

    app=FastAPI(title="Private LLM Chat",version="1.0.0",openapi_version="3.1.0",lifespan=lifespan,
                docs_url=None,redoc_url=None,openapi_url=None,
                responses={code:{"model":ErrorResponse} for code in (401,404,409,413,422,429,503)})
    app.add_middleware(ByteLimit,limit=settings.body_limit)
    app.state.store,app.state.worker=database,worker
    app.state.server=None

    @app.exception_handler(DomainError)
    async def domain_error(_request,error):
        headers={"Retry-After":str(error.retry_after)} if error.retry_after else {}
        return JSONResponse({"error":{"code":error.code}},status_code=error.status,headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request,_error):
        return JSONResponse({"error":{"code":"invalid_request"}},status_code=422)

    @app.exception_handler(Exception)
    async def unexpected_error(_request,_error):
        return JSONResponse({"error":{"code":"service_error"}},status_code=500)

    bearer=HTTPBearer(auto_error=False)
    def authenticate(credentials:HTTPAuthorizationCredentials|None=Depends(bearer)):
        if not credentials or credentials.scheme.lower()!="bearer" or not re.fullmatch(r"[A-Za-z0-9_-]{43}",credentials.credentials):
            raise DomainError("unauthorized",401)
        return database.authenticate(credentials.credentials)

    @app.get("/healthz")
    async def health():
        return {"status":"ok"}

    @app.get("/v1/me",response_model=Identity)
    async def me(identity=Depends(authenticate)):
        return identity

    @app.get("/v1/status",response_model=ServiceStatus)
    async def status(_identity=Depends(authenticate)):
        return dict(database.counts(),accepting=worker.accepting,mode="demo" if settings.mode=="stub" else "local",
                    queue_capacity=settings.queue_capacity,rate_burst=settings.bucket_capacity,
                    rate_refill_per_minute=settings.refill_per_minute,output_cap=settings.output_cap,prompt_cap=settings.prompt_cap,
                    pairing_cleanup_required=worker.pairing_cleanup_required)

    @app.get("/v1/conversations",response_model=ConversationPage)
    async def conversations(limit:int=Query(50,ge=1,le=100),offset:int=Query(0,ge=0),identity=Depends(authenticate)):
        return database.conversations(identity["owner_id"],limit,offset)

    @app.post("/v1/conversations",response_model=Conversation,status_code=201)
    async def create(body:CreateConversation,identity=Depends(authenticate)):
        if not body.title.strip():
            raise DomainError("invalid_title",422)
        return database.create_conversation(identity["owner_id"],body.title.strip())

    @app.get("/v1/conversations/{identifier}",response_model=Conversation)
    async def conversation(identifier:str,identity=Depends(authenticate)):
        return database.public_conversation(database.conversation(identity["owner_id"],identifier))

    @app.patch("/v1/conversations/{identifier}",response_model=Conversation)
    async def rename(identifier:str,body:RenameConversation,identity=Depends(authenticate)):
        if not body.title.strip():
            raise DomainError("invalid_title",422)
        return database.rename(identity["owner_id"],identifier,body.title.strip(),body.expected_revision)

    @app.delete("/v1/conversations/{identifier}",status_code=204,response_class=Response)
    async def delete(identifier:str,expected_revision:int=Query(ge=1),identity=Depends(authenticate)):
        active=database.delete(identity["owner_id"],identifier,expected_revision)
        worker.cancel(active)
        return Response(status_code=204)

    @app.get("/v1/conversations/{identifier}/messages",response_model=MessagePage)
    async def messages(identifier:str,limit:int=Query(50,ge=1,le=100),offset:int=Query(0,ge=0),identity=Depends(authenticate)):
        return database.messages(identity["owner_id"],identifier,limit,offset)

    @app.post("/v1/conversations/{identifier}/requests",response_model=Snapshot,status_code=202,
              responses={200:{"model":Snapshot,"description":"Idempotent replay of the original job"}})
    async def submit(identifier:str,body:SubmitRequest,response:Response,
                     idempotency_key:str=Header(alias="Idempotency-Key"),identity=Depends(authenticate)):
        if not worker.accepting:
            raise DomainError("service_stopping",503)
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}",idempotency_key) or not body.text.strip():
            raise DomainError("invalid_request",422)
        result,created=database.admit(identity,identifier,idempotency_key,body.text,body.expected_revision)
        response.status_code=202 if created else 200
        worker.notify()
        return result

    @app.get("/v1/requests/by-key/{key}",response_model=Snapshot)
    async def lookup(key:str,identity=Depends(authenticate)):
        return database.by_key(identity["owner_id"],key)

    @app.get("/v1/requests/{identifier}",response_model=Snapshot)
    @app.get("/v1/requests/{identifier}/snapshot",response_model=Snapshot)
    async def request_snapshot(identifier:str,identity=Depends(authenticate)):
        return database.job(identity["owner_id"],identifier)

    @app.post("/v1/requests/{identifier}/cancel",response_model=Snapshot)
    async def cancel(identifier:str,identity=Depends(authenticate)):
        result=database.cancel(identity["owner_id"],identifier,worker.pending_partial(identifier))
        worker.cancel(identifier)
        return result

    @app.get("/v1/requests/{identifier}/events",response_class=StreamingResponse,
             responses={200:{"content":{"text/event-stream":{"schema":{"type":"string"}}}}},
             description="Authoritative Snapshot JSON events: snapshot, reset, terminal; monotonic seq ID and state_version.")
    async def events(identifier:str,request:Request,last_event_id:str|None=Header(default=None),
                     identity=Depends(authenticate),authorization:str=Header()):
        database.job(identity["owner_id"],identifier)
        device=identity["device_id"]
        if streams.get(device,0)>=3:
            raise DomainError("stream_limit",429,1)
        if last_event_id is not None and not re.fullmatch(r"[0-9]{1,20}",last_event_id):
            raise DomainError("invalid_event_cursor",422)
        cursor=int(last_event_id) if last_event_id is not None else None
        streams[device]=streams.get(device,0)+1
        async def stream():
            nonlocal cursor
            heartbeat=time.monotonic()
            try:
                while worker.accepting:
                    if await request.is_disconnected():
                        return
                    try:
                        database.authenticate(authorization[7:])
                        snapshot=database.job(identity["owner_id"],identifier)
                    except DomainError:
                        return
                    for seq,event,payload in database.events(identifier,cursor):
                        yield f"id: {seq}\nevent: {event}\ndata: {json.dumps(payload,separators=(',',':'))}\n\n"
                        cursor=seq
                    if snapshot["state"] in TERMINAL:
                        return
                    if time.monotonic()-heartbeat>=10:
                        yield ": heartbeat\n\n"
                        heartbeat=time.monotonic()
                    await asyncio.sleep(0.1)
            finally:
                streams[device]-=1
        return StreamingResponse(stream(),media_type="text/event-stream",headers={"Cache-Control":"no-store","X-Accel-Buffering":"no"})

    @app.post("/internal/shutdown",include_in_schema=False)
    async def internal_shutdown(request:Request,authorization:str|None=Header(default=None)):
        client=request.client.host if request.client else ""
        if (client not in {"127.0.0.1","::1"} or not settings.shutdown_token
                or not secrets.compare_digest(authorization or "","Bearer "+settings.shutdown_token)):
            raise DomainError("not_found",404)
        await shutdown()
        if app.state.server:
            app.state.server.should_exit=True
        return {"stopped":True}

    return app


def main(mode="local"):
    import uvicorn
    try:
        settings=Settings.from_environment(mode)
        app=create_app(settings)
        logging={"version":1,"disable_existing_loggers":False,"handlers":{"null":{"class":"logging.NullHandler"}},
                 "loggers":{name:{"handlers":["null"],"propagate":False} for name in ("uvicorn","uvicorn.error","uvicorn.access","httpx")}}
        server=uvicorn.Server(uvicorn.Config(app,host=settings.host,port=settings.port,workers=1,access_log=False,
                                            log_config=logging,timeout_graceful_shutdown=330))
        app.state.server=server
        server.run()
        return 0 if server.started else 1
    except Exception:
        print("SERVICE_STATUS: CONFIGURATION_OR_STARTUP_ERROR")
        return 2
