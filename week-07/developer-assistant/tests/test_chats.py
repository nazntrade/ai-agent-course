import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace
from fastapi.testclient import TestClient
from assistant.store import Store,ident,now
from assistant.config import Settings
from assistant.api import create_app
from assistant.scheduler import Scheduler

class ChatFixture:
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);(self.root/'README.md').write_text('public')
        self.store=Store(self.root/'state/db');self.pid=self.store.project(self.root)['id']
    def tearDown(self):self.tmp.cleanup()
    def task(self,cid=None,status='completed'):
        t=dict(id=ident(),project_id=self.pid,question='structure',provider='local',mode='help',status=status,created=now(),answer='Saved evidence',sources=[{'label':'S1'}],quality='NOT_ASSESSED',plan=[])
        if cid:t['chat_id']=cid
        self.store.save_task(t);return t

class Chats(ChatFixture,unittest.TestCase):
    def test_legacy_migration_is_idempotent_and_payload_report_ids_unchanged(self):
        t=self.task()
        with self.store.connect() as c:
            before=c.execute('SELECT payload FROM tasks WHERE id=?',(t['id'],)).fetchone()[0]
            c.execute('INSERT INTO deliveries VALUES (?,?,?)',(t['id'],'sent',42))
        Store(self.store.path);Store(self.store.path)
        chats=self.store.chats(self.pid);self.assertEqual(len(chats),1)
        self.assertEqual(self.store.chat_tasks(chats[0]['id'])['tasks'][0]['id'],t['id'])
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT payload FROM tasks WHERE id=?',(t['id'],)).fetchone()[0],before)
            self.assertEqual(c.execute('SELECT message_id FROM deliveries WHERE task_id=?',(t['id'],)).fetchone()[0],42)
    def test_create_rename_archive_restore_and_restart_preserve_runs(self):
        chat=self.store.create_chat(self.pid,'  Project notes  ');cid=chat['id'];t=self.task(cid)
        self.store.rename_chat(cid,'Design review');self.store.archive_chat(cid)
        self.assertEqual(self.store.chats(self.pid),[]);self.assertEqual(self.store.task(t['id'])['answer'],'Saved evidence')
        self.assertEqual(self.store.chat_tasks(cid)['tasks'][0]['id'],t['id'])
        restored=Store(self.store.path);restored.archive_chat(cid,False)
        self.assertEqual(restored.chats(self.pid)[0]['title'],'Design review')
    def test_wrong_project_and_archived_targets_rejected_before_task_insert(self):
        cid=self.store.create_chat(self.pid)['id'];other=self.root/'other';other.mkdir();pid=self.store.project(other)['id']
        t=self.task();t.update(id=ident(),project_id=pid,chat_id=cid)
        with self.assertRaises(ValueError):self.store.save_task(t)
        self.store.archive_chat(cid);t.update(id=ident(),project_id=self.pid)
        with self.assertRaises(ValueError):self.store.save_task(t)
        with self.assertRaises(ValueError):self.store.task(t['id'])
    def test_active_chat_cannot_be_deleted_and_no_hidden_stop(self):
        cid=self.store.create_chat(self.pid)['id'];t=self.task(cid,'queued')
        with self.assertRaises(ValueError):self.store.archive_chat(cid)
        self.assertEqual(self.store.task(t['id'])['status'],'queued');self.assertFalse(self.store.chat(cid)['archived'])
    def test_concurrent_archive_and_start_have_one_consistent_outcome(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        cid=self.store.create_chat(self.pid)['id'];t=dict(id=ident(),project_id=self.pid,chat_id=cid,question='structure',status='queued',created=now())
        barrier=Barrier(2)
        def start():
            barrier.wait()
            try:self.store.save_task(t);return 'started'
            except ValueError:return 'denied'
        def archive():
            barrier.wait()
            try:self.store.archive_chat(cid);return 'archived'
            except ValueError:return 'busy'
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(start);b=pool.submit(archive);result=(a.result(),b.result())
        self.assertIn(result,[('started','busy'),('denied','archived')])
        self.assertEqual(bool(self.store.chat(cid)['archived']),result[0]=='denied')
    def test_latest_page_and_older_cursor_are_chronological_without_loss(self):
        cid=self.store.create_chat(self.pid)['id'];original=[self.task(cid)['id'] for _ in range(7)]
        latest=self.store.chat_tasks(cid,limit=3);older=self.store.chat_tasks(cid,before=latest['next_before'],limit=3)
        first=self.store.chat_tasks(cid,before=older['next_before'],limit=3)
        self.assertEqual([t['id'] for t in first['tasks']+older['tasks']+latest['tasks']],original)
        self.assertIsNone(first['next_before'])
    def test_names_are_bounded_and_api_keeps_html_as_text(self):
        with self.assertRaises(ValueError):self.store.create_chat(self.pid,'   ')
        with self.assertRaises(ValueError):self.store.rename_chat(self.store.create_chat(self.pid)['id'],'x'*81)
        with TestClient(create_app(Settings(self.root/'api-state'))) as client:
            pid=client.get('/api/projects').json()['default_id']
            c=client.post(f'/api/projects/{pid}/chats',json={'title':'<script>bad()</script>'}).json()
            self.assertEqual(c['title'],'<script>bad()</script>')
            self.assertEqual(client.post(f"/api/chats/{c['id']}/rename",json={'title':'  '}).status_code,400)
            self.assertEqual(client.get(f"/api/chats/{c['id']}/tasks?limit=201").status_code,422)

class ChatSchedules(ChatFixture,unittest.TestCase):
    def scheduler(self):
        return Scheduler(self.store,SimpleNamespace(settings=Settings(self.root,telegram_token='fake',telegram_chat='fake')))
    def test_schedule_blocks_delete_and_archived_schedule_cannot_reenable(self):
        scheduler=self.scheduler();cid=self.store.create_chat(self.pid)['id']
        s=scheduler.create(self.pid,'structure','local',(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat(),chat_id=cid)
        with self.assertRaises(ValueError):self.store.archive_chat(cid)
        scheduler.toggle(s['id'],False);self.store.archive_chat(cid);before=scheduler.list()[0].copy()
        with self.assertRaises(ValueError):scheduler.toggle(s['id'],True)
        self.assertEqual(scheduler.list()[0],before)
        self.store.archive_chat(cid,False);self.assertTrue(scheduler.toggle(s['id'],True)['enabled'])
    def test_schedule_save_validates_in_transaction_after_archive(self):
        scheduler=self.scheduler();cid=self.store.create_chat(self.pid)['id']
        s=scheduler.create(self.pid,'structure','local',(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat(),chat_id=cid)
        scheduler.toggle(s['id'],False);self.store.archive_chat(cid)
        s['enabled']=True
        with self.assertRaises(ValueError):scheduler.save(s)
        self.assertFalse(scheduler.list()[0]['enabled'])
    def test_legacy_schedule_maps_previous_report_chat_after_table_init(self):
        t=self.task();Store(self.store.path)
        with self.store.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS schedules(id TEXT PRIMARY KEY,project_id TEXT,payload TEXT)')
            s=dict(id=ident(),project_id=self.pid,question='structure',provider='local',mode='help',due=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat(),interval_minutes=0,telegram=False,enabled=False,inflight=None,last_task_id=t['id'],last_status='completed')
            c.execute('INSERT INTO schedules VALUES (?,?,?)',(s['id'],self.pid,json.dumps(s)))
        scheduler=self.scheduler();self.assertEqual(scheduler.list()[0]['chat_id'],self.store.task(t['id'])['chat_id'])

    def legacy_case(self,case):
        cid=self.store.create_chat(self.pid,'Legacy')['id'];t=self.task(cid)
        if case=='missing':last=ident()
        elif case=='wrong_project':
            other=self.root/'another';other.mkdir();op=self.store.project(other)['id'];oc=self.store.create_chat(op)['id']
            t=dict(id=ident(),project_id=op,chat_id=oc,question='other',status='completed',created=now());self.store.save_task(t);last=t['id']
        else:
            self.store.archive_chat(cid);last=t['id']
        with self.store.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS schedules(id TEXT PRIMARY KEY,project_id TEXT,payload TEXT)')
            s=dict(id=ident(),project_id=self.pid,question='structure',provider='local',mode='help',due=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat(),interval_minutes=0,telegram=False,enabled=False,inflight=None,last_task_id=last,last_status='completed')
            c.execute('INSERT INTO schedules VALUES (?,?,?)',(s['id'],self.pid,json.dumps(s)))
        scheduler=self.scheduler();mapped=scheduler.list()[0];chat=self.store.chat(mapped['chat_id'])
        self.assertEqual(chat['project_id'],self.pid);self.assertFalse(chat['archived']);self.assertEqual(mapped['last_task_id'],last)
    def test_missing_legacy_report_maps_active_project_default(self):self.legacy_case('missing')
    def test_cross_project_legacy_report_cannot_redirect_schedule(self):self.legacy_case('wrong_project')
    def test_archived_legacy_report_does_not_revive_deleted_chat(self):self.legacy_case('archived')
