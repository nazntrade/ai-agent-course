"""Persistent schedules; single worker; missed runs are never replayed in a burst."""
import asyncio
from datetime import datetime, timezone, timedelta
from .store import ident,now

def timestamp(value):
    d=datetime.fromisoformat(value.replace('Z','+00:00'))
    if d.tzinfo is None:
        raise ValueError('Scheduled time must include a timezone')
    return d.astimezone(timezone.utc)

class Scheduler:
    def __init__(self,store,service):
        self.store=store;self.service=service;self.loop=None
        with store.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS schedules(id TEXT PRIMARY KEY,project_id TEXT,payload TEXT)')
        # An interrupted trigger cannot be safely assumed not to have executed.
        for s in self.list():
            if not s.get('chat_id'):
                self.map_legacy_chat(s)
            if s.get('inflight'):
                s.update(enabled=False,inflight=None,error='Interrupted scheduled run. Inspect history before enabling again.')
                self.save(s)

    def map_legacy_chat(self,s):
        previous=None
        if s.get('last_task_id'):
            try:
                task=self.store.task(s['last_task_id'])
                if task['project_id']==s['project_id'] and task.get('chat_id'):
                    chat=self.store.chat(task['chat_id'])
                    if chat['project_id']==s['project_id'] and not chat['archived']:
                        previous=chat['id']
            except ValueError:
                pass
        s['chat_id']=self.store.resolve_chat(s['project_id'],previous)
        self.save(s)

    def save(self,s):
        import json
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if s.get('chat_id') and s['enabled']:
                self.store.check_chat(c,s['chat_id'],s['project_id'])
            c.execute('INSERT OR REPLACE INTO schedules VALUES (?,?,?)',(s['id'],s['project_id'],json.dumps(s)))
        return s

    def list(self,pid=None):
        import json
        with self.store.connect() as c:
            rows=c.execute('SELECT payload FROM schedules'+(' WHERE project_id=?' if pid else ''),(pid,) if pid else ()).fetchall()
        return [json.loads(r['payload']) for r in rows]

    def create(self,pid,question,provider,due,interval=0,telegram=False,mode='help',chat_id=None):
        self.store.get_project(pid)
        chat_id=self.store.resolve_chat(pid,chat_id)
        when=timestamp(due)
        if when<=datetime.now(timezone.utc):
            raise ValueError('Choose a future scheduled time')
        if telegram and not self.service.settings.public()['telegram_configured']:
            raise ValueError('Owner Telegram is not configured')
        q=self.service.settings.scrub(question.strip())
        if not q or len(q)>4000: raise ValueError('Invalid scheduled question')
        return self.save(dict(id=ident(),project_id=pid,question=q,provider=provider,mode=mode,chat_id=chat_id,
            due=when.isoformat(),interval_minutes=interval,telegram=telegram,enabled=True,
            inflight=None,last_task_id=None,last_status=None,delivery=None,error=None,created=now()))

    def toggle(self,sid,enabled):
        s=next((s for s in self.list() if s['id']==sid),None)
        if not s: raise ValueError('Unknown schedule')
        if enabled: self.store.resolve_chat(s['project_id'],s.get('chat_id'))
        if enabled and s.get('inflight'): raise ValueError('Wait for the current run')
        if enabled and timestamp(s['due'])<=datetime.now(timezone.utc):
            s['due']=(datetime.now(timezone.utc)+timedelta(seconds=2)).isoformat()
        s['enabled']=enabled
        return self.save(s)

    async def tick(self):
        current=datetime.now(timezone.utc)
        for s in self.list():
            if not s.get('chat_id'):
                self.map_legacy_chat(s)
            if s.get('inflight'):
                t=self.store.task(s['inflight'])
                if t['status'] in {'queued','running'}: continue
                s['last_status']=t['status'];s['inflight']=None
                if s['telegram'] and t['status']=='completed':
                    try:
                        s['delivery']=await self.service.report(t['id'])
                    except Exception:
                        s['delivery']={'status':'failed'}
                        s['error']='Telegram failed; no automatic delivery retry'
                # Pause may have been requested while report delivery awaited IO.
                fresh=next(x for x in self.list() if x['id']==s['id'])
                s['enabled']=fresh['enabled']
                if s['interval_minutes'] and s['enabled']:
                    s['due']=(current+timedelta(minutes=s['interval_minutes'])).isoformat()
                else: s['enabled']=False
                self.save(s)
            elif s['enabled'] and timestamp(s['due'])<=current and not self.service.busy():
                # Persist the claim before launching. Restart requires explicit review.
                s['inflight']='trigger_claimed';self.save(s)
                try:
                    t=self.service.create(s['project_id'],s['question'],s['provider'],s['mode'],chat_id=s['chat_id'])
                    s.update(inflight=t['id'],last_task_id=t['id'],last_status='queued',error=None,delivery=None)
                except Exception:
                    s.update(enabled=False,inflight=None,error='Scheduled task could not be created')
                self.save(s)

    async def run(self):
        while True:
            await self.tick();await asyncio.sleep(1)

    def start(self):
        self.loop=asyncio.create_task(self.run())

    async def close(self):
        if self.loop:
            self.loop.cancel();await asyncio.gather(self.loop,return_exceptions=True)
