"""One generation worker, durable job transitions and shielded lifecycle."""
import asyncio
import time
import httpx
from gateway.app.context import prepare_context
from gateway.app.database import TERMINAL
from gateway.app.manage import expire_payload
from gateway.app.upstream import LeaseManager,LocalProvider,ProviderConfig,StreamResult,UpstreamError


class StubProvider:
    async def count(self,body):
        return {"input_tokens":sum(len(message["content"].split())+4 for message in body["messages"])+3,
                "context_capacity":8192,"method":"isolated-fake-tokenizer"}

    async def stream(self,body,on_content=None):
        text = "Demo reply: " + body["messages"][-1]["content"][:100]
        result = StreamResult(finish_reason="stop",done=True,prompt_tokens=(await self.count(body))["input_tokens"])
        for start in range(0,len(text),6):
            delta = text[start:start+6]
            await asyncio.sleep(0.025)
            result.content+=delta
            result.content_chunks+=1
            if on_content:
                await on_content(delta)
        return result


class ProviderPool:
    def __init__(self,settings):
        self.settings=settings
        self.client=None
        self.manager=None
        self.provider=None
        self.config=ProviderConfig.from_environment() if settings.mode!="stub" else None

    async def get(self):
        if self.provider is None:
            if self.config is None:
                self.provider=StubProvider()
            else:
                self.client=httpx.AsyncClient(follow_redirects=False,trust_env=False)
                self.manager=LeaseManager(self.config,self.client)
                self.provider=LocalProvider(self.config,self.client,self.manager)
        if self.manager:
            await self.manager.ensure()
        return self.provider

    async def close(self):
        try:
            if self.manager:
                await self.manager.close()
        finally:
            if self.client:
                await self.client.aclose()
            self.manager,self.client,self.provider=None,None,None


class Worker:
    def __init__(self,store,settings,pool=None):
        self.store,self.settings=store,settings
        self.pool=pool or ProviderPool(settings)
        self.wake=asyncio.Event()
        self.task=None
        self.generation=None
        self.active_id=None
        self.partials={}
        self.accepting=True
        self.stopped=False
        self.pairing_cleanup_required=False
        self.last_work=time.monotonic()

    async def start(self):
        self.store.recover()
        self.task=asyncio.create_task(self.run())

    def notify(self):
        self.wake.set()

    def cancel(self,identifier):
        if identifier and identifier==self.active_id and self.generation:
            self.generation.cancel()
        self.notify()

    def pending_partial(self,identifier):
        # API handlers and generation callbacks run on this same event loop.
        # Read the coalesced buffer synchronously, then commit it together with
        # the terminal transition without an await between those operations.
        return self.partials.get(identifier)

    async def run(self):
        while self.accepting:
            self.store.expire_pairings()
            try:
                expire_payload(self.store,self.settings.pairing_dir or self.settings.data_dir/"pairing")
                self.pairing_cleanup_required=False
            except Exception:
                # Expired credentials are already revoked in SQLite. A corrupt
                # local envelope must be repaired locally, never reactivated.
                self.pairing_cleanup_required=True
            job=self.store.claim()
            if job and not job.get("expired"):
                self.active_id=job["id"]
                self.generation=asyncio.create_task(self.execute(job))
                try:
                    await self.generation
                except asyncio.CancelledError:
                    if self.accepting:
                        self.store.finish(job["id"],"cancelled","cancelled")
                except Exception:
                    self.store.finish(job["id"],"failed","worker_operation_failed")
                finally:
                    self.active_id,self.generation=None,None
                    self.last_work=time.monotonic()
                continue
            if job:
                continue
            if time.monotonic()-self.last_work>=self.settings.idle_seconds:
                try:
                    await self.pool.close()
                except Exception:
                    # Transport failure stays an explicit failed lease cleanup;
                    # expiry is the emergency guard, not a successful release.
                    self.accepting=False
                    self.store.stop_jobs()
                self.last_work=time.monotonic()
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(),timeout=1)
            except TimeoutError:
                pass

    async def execute(self,job):
        identifier=job["id"]
        partial=""
        self.partials[identifier]=partial
        published=0.0
        try:
            provider=await self.pool.get()
            if not self.store.update(identifier,state="preparing_context"):
                return
            body,meta=await prepare_context(provider,self.settings,job["text"],self.store.history(job["conversation_id"]))
            if not self.store.update(identifier,state="running",**meta):
                return
            async def content(delta):
                nonlocal partial,published
                partial+=delta
                self.partials[identifier]=partial
                if time.monotonic()-published>=0.1:
                    if not self.store.update(identifier,partial_content=partial):
                        raise asyncio.CancelledError
                    published=time.monotonic()
            result=await provider.stream(body,content)
            if result.prompt_tokens!=meta["prompt_tokens"]:
                raise UpstreamError("native_count_inference_mismatch")
            self.store.finish(identifier,"completed",content=result.content,finish_reason=result.finish_reason)
        except asyncio.CancelledError:
            self.store.finish(identifier,"cancelled","cancelled",content=partial)
            raise
        except UpstreamError as error:
            self.store.update(identifier,partial_content=partial)
            self.store.finish(identifier,"failed",error.code)
            # Broken renewal/acquire state is never silently reused.
            if self.pool.manager and error.code!="context_too_long":
                await self.pool.close()
        except Exception:
            self.store.update(identifier,partial_content=partial)
            self.store.finish(identifier,"failed","generation_failed")
        finally:
            self.partials.pop(identifier,None)

    async def stop(self):
        if self.stopped:
            return
        self.accepting=False
        self.store.stop_jobs(dict(self.partials))
        if self.generation:
            self.generation.cancel()
        self.notify()
        if self.task:
            await asyncio.gather(self.task,return_exceptions=True)
        await self.pool.close()
        self.stopped=True
