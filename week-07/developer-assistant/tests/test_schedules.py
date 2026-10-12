import asyncio
import tempfile
import unittest
from pathlib import Path
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace
from assistant.store import Store,ident,now
from assistant.config import Settings
from assistant.scheduler import Scheduler

class Schedules(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name);(root/'README.md').write_text('public docs')
        self.store=Store(root/'state/db');self.pid=self.store.project(root)['id'];self.calls=[];self.sent=[];self.busy=False
        def create(pid,q,provider,mode):
            t=dict(id=ident(),project_id=pid,question=q,provider=provider,mode=mode,status='completed',created=now())
            self.calls.append(t);self.store.save_task(t);return t
        async def report(tid): self.sent.append(tid);return {'status':'sent'}
        self.service=SimpleNamespace(settings=Settings(root,telegram_token='fake',telegram_chat='fake'),busy=lambda:self.busy,create=create,report=report)
        self.scheduler=Scheduler(self.store,self.service)
    async def asyncTearDown(self): self.tmp.cleanup()
    def create_due(self,interval=0):
        s=self.scheduler.create(self.pid,'structure','local',(datetime.now(timezone.utc)+timedelta(seconds=10)).isoformat(),interval,True)
        s['due']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat();return self.scheduler.save(s)
    async def test_once_delivers_completed_report_once(self):
        s=self.create_due();await self.scheduler.tick();await self.scheduler.tick();await self.scheduler.tick()
        self.assertEqual(len(self.calls),1);self.assertEqual(len(self.sent),1);self.assertFalse(self.scheduler.list()[0]['enabled'])
    async def test_busy_waits_and_repeat_does_not_catch_up(self):
        self.create_due(10);self.busy=True;await self.scheduler.tick();self.assertEqual(self.calls,[])
        self.busy=False;await self.scheduler.tick();await self.scheduler.tick();await self.scheduler.tick()
        self.assertEqual(len(self.calls),1);self.assertGreater(datetime.fromisoformat(self.scheduler.list()[0]['due']),datetime.now(timezone.utc))
    async def test_pause_during_awaited_delivery_survives(self):
        s=self.create_due(10);await self.scheduler.tick();started=asyncio.Event();release=asyncio.Event()
        async def report(tid): started.set();await release.wait();return {'status':'sent'}
        self.service.report=report;job=asyncio.create_task(self.scheduler.tick());await started.wait()
        self.scheduler.toggle(s['id'],False);release.set();await job
        self.assertFalse(self.scheduler.list()[0]['enabled'])
    async def test_interrupted_claim_pauses_on_restart(self):
        s=self.create_due(10);s['inflight']='trigger_claimed';self.scheduler.save(s)
        restarted=Scheduler(self.store,self.service);await restarted.tick()
        self.assertFalse(restarted.list()[0]['enabled']);self.assertEqual(self.calls,[])
    async def test_cancelled_run_not_sent(self):
        self.create_due();await self.scheduler.tick();t=self.calls[0];t['status']='cancelled';self.store.save_task(t)
        await self.scheduler.tick();self.assertEqual(self.sent,[])
    async def test_timezone_required(self):
        with self.assertRaises(ValueError): self.scheduler.create(self.pid,'question','local','2030-01-01T10:00:00')
