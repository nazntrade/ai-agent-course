import asyncio
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from assistant.api import create_app
from assistant.config import Settings, MODULE
from assistant.scope import ProjectScope,ScopeError
from assistant.store import Store,ident,now
from assistant.service import Service
from assistant.tools import Executor
from assistant.project_mcp import git_context

class CorpusAndScope(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'project';self.root.mkdir()
        (self.root/'docs').mkdir();(self.root/'README.md').write_text('# Structure\nThe engine uses SQLite storage and a sequential queue.\n',encoding='utf-8')
        (self.root/'docs/api.md').write_text('# API\nPOST /api/tasks creates a task with provider and question.\n',encoding='utf-8')
        self.store=Store(Path(self.tmp.name)/'state/db.sqlite3');self.pid=self.store.project(self.root)['id']
    def tearDown(self):
        self.tmp.cleanup()
    def test_readme_docs_and_russian_alias_retrieval(self):
        m=self.store.index(self.pid);self.assertEqual(m['document_count'],2)
        self.assertTrue(any(x['path']=='docs/api.md' for x in self.store.retrieve(self.pid,'What API creates tasks?')))
        self.assertTrue(self.store.retrieve(self.pid,'/help Где хранится информация?'))
        self.assertEqual(self.store.retrieve(self.pid,'astronomical unicorns'),[])
    def test_reindex_versions_stable_and_deleted_sources_removed(self):
        self.store.index(self.pid);first=self.store.retrieve(self.pid,'storage')
        self.store.index(self.pid);second=self.store.retrieve(self.pid,'storage')
        self.assertEqual(first,second)
        (self.root/'README.md').write_text('# Structure\nStorage now uses a revised SQLite table.\n',encoding='utf-8')
        (self.root/'docs/api.md').unlink()
        m=self.store.index(self.pid);self.assertEqual(m['document_count'],1)
        third=self.store.retrieve(self.pid,'storage');self.assertNotEqual(first[0]['hash'],third[0]['hash'])
        self.assertEqual(self.store.retrieve(self.pid,'/api/tasks'),[])
    def test_secret_paths_and_runtime_are_excluded(self):
        (self.root/'.env').write_text('KEY=fake-credential-do-not-index',encoding='utf-8')
        (self.root/'local-data').mkdir();(self.root/'local-data/private.md').write_text('fake-private',encoding='utf-8')
        scope=ProjectScope(self.root)
        for p in ['.env','../outside.txt',str(Path(self.tmp.name)/'outside.txt'),'local-data/private.md']:
            with self.assertRaises((ScopeError,FileNotFoundError)):
                scope.read(p)
        self.store.index(self.pid)
        self.assertEqual(self.store.retrieve(self.pid,'fake credential private'),[])
    def test_long_line_is_bounded_and_chunk_ids_unique(self):
        (self.root/'docs/long.txt').write_text('longline '*1500,encoding='utf-8')
        m=self.store.index(self.pid);self.assertGreater(m['chunk_count'],10)
        with self.store.connect() as c:
            rows=c.execute('SELECT text FROM chunks').fetchall()
        self.assertLessEqual(max(len(x[0]) for x in rows),1100)
    def test_escaping_link_is_rejected(self):
        outside=Path(self.tmp.name)/'outside.md';outside.write_text('do not read',encoding='utf-8')
        link=self.root/'docs/link.md'
        try:
            link.symlink_to(outside)
        except OSError:
            folder=Path(self.tmp.name)/'outside-dir';folder.mkdir();(folder/'outside.md').write_text('do not read')
            junction=self.root/'docs/junction'
            r=subprocess.run(['cmd','/c','mklink','/J',str(junction),str(folder)],capture_output=True)
            if r.returncode:
                self.skipTest('OS does not permit a linked fixture')
            with self.assertRaises(ScopeError):
                ProjectScope(self.root).read('docs/junction/outside.md')
            return
        with self.assertRaises(ScopeError):
            ProjectScope(self.root).read('docs/link.md')
    def test_recovery_never_turns_unfinished_into_completed(self):
        task={'id':ident(),'project_id':self.pid,'status':'running','created':now(),'answer':'','partial':'unfinished','plan':[{'status':'running'}]}
        self.store.save_task(task);again=Store(self.store.path)
        restored=again.task(task['id']);self.assertEqual(restored['status'],'failed');self.assertIn('Interrupted',restored['error'])
        self.assertEqual(restored['partial'],'');self.assertEqual(restored['plan'][0]['status'],'failed')

class ApiBoundaries(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.app=create_app(Settings(data=Path(self.tmp.name),local_key='fake-local-secret',remote_key='fake-remote-secret'))
        self.client=TestClient(self.app);self.client.__enter__()
        self.pid=self.client.get('/api/projects').json()['default_id']
    def tearDown(self):
        self.client.__exit__(None,None,None);self.tmp.cleanup()
    def test_config_does_not_return_credentials(self):
        text=self.client.get('/api/config').text
        self.assertNotIn('fake-local-secret',text);self.assertNotIn('fake-remote-secret',text)
    def test_cross_origin_and_wrong_host_rejected(self):
        self.assertEqual(self.client.post('/api/projects',json={'root':str(MODULE)},headers={'Origin':'https://attacker.example'}).status_code,403)
        self.assertEqual(self.client.get('/api/health',headers={'Host':'attacker.example'}).status_code,403)
    def test_unknown_schema_fields_and_arbitrary_shell_rejected(self):
        self.assertEqual(self.client.post('/api/tasks',json={'project_id':self.pid,'question':'hello','command':'rm'}).status_code,422)
        self.assertEqual(self.client.post(f'/api/projects/{self.pid}/tools',json={'name':'shell','arguments':{}}).status_code,422)
        self.assertEqual(self.client.post(f'/api/projects/{self.pid}/tools',json={'name':'read_project_file','arguments':{'path':'.env'}}).status_code,400)
    def test_import_rejects_traversal_secret_and_executable(self):
        for name in ['../escape.md','.env','script.py','credentials.md']:
            self.assertEqual(self.client.post(f'/api/projects/{self.pid}/documents',json={'name':name,'content':'fake'}).status_code,400)
    def test_model_failure_is_failed_not_completed(self):
        async def broken(*args):
            raise ValueError('Provider unavailable')
        with patch('assistant.service.project_call',broken):
            r=self.client.post('/api/tasks',json={'project_id':self.pid,'question':'/help structure','provider':'local'})
            self.assertEqual(r.status_code,200);tid=r.json()['id']
            import time
            for _ in range(20):
                t=self.client.get('/api/tasks/'+tid).json()
                if t['status']=='failed':break
                time.sleep(.02)
            self.assertEqual(t['status'],'failed');self.assertEqual(t['answer'],'');self.assertEqual(t['quality'],'NOT_ASSESSED')
    def test_import_checks_linked_parent_before_any_write(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)/'project';root.mkdir();outside=Path(d)/'outside';outside.mkdir()
            r=subprocess.run(['cmd','/c','mklink','/J',str(root/'docs'),str(outside)],capture_output=True)
            if r.returncode:self.skipTest('No junction fixture available')
            pid=self.client.post('/api/projects',json={'root':str(root)}).json()['id']
            r=self.client.post(f'/api/projects/{pid}/documents',json={'name':'new.md','content':'demo'})
            self.assertEqual(r.status_code,400);self.assertFalse((outside/'imported').exists())

class GitStates(unittest.TestCase):
    def test_nonrepo_unborn_normal_and_detached(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            self.assertEqual(git_context(root)['state'],'not_repository')
            subprocess.run(['git','init','-b','demo',d],check=True,capture_output=True)
            self.assertEqual(git_context(root),{'state':'unborn','branch':'demo'})
            (root/'README.md').write_text('demo')
            subprocess.run(['git','-C',d,'add','README.md'],check=True,capture_output=True)
            subprocess.run(['git','-C',d,'-c','user.name=Demo','-c','user.email=demo@example.invalid','commit','-m','fixture'],check=True,capture_output=True)
            self.assertEqual(git_context(root),{'state':'branch','branch':'demo'})
            subprocess.run(['git','-C',d,'checkout','--detach'],check=True,capture_output=True)
            self.assertEqual(git_context(root)['state'],'detached')

class WorkflowAsync(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)/'project';self.root.mkdir()
        (self.root/'README.md').write_text('# Structure\nWidget engine stores events in SQLite.',encoding='utf-8')
        self.store=Store(Path(self.tmp.name)/'db.sqlite3');self.pid=self.store.project(self.root)['id'];self.store.index(self.pid)
        self.settings=Settings(data=Path(self.tmp.name))
    async def asyncTearDown(self):
        self.tmp.cleanup()
    async def test_stop_cancels_inflight_and_queued_without_late_result(self):
        entered=asyncio.Event();cancelled=asyncio.Event()
        class Slow:
            async def completion(this,*args):
                entered.set()
                try:await asyncio.sleep(30)
                finally:cancelled.set()
        service=Service(self.store,self.settings,Slow())
        async def mcp(*a):return {'result':{'branch':'demo'}}
        with patch('assistant.service.project_call',mcp):
            a=service.create(self.pid,'Widget structure','local');b=service.create(self.pid,'Widget structure','local')
            await asyncio.wait_for(entered.wait(),2)
            self.assertEqual(self.store.task(b['id'])['status'],'queued')
            service.cancel(b['id']);service.cancel(a['id'])
            await asyncio.gather(*service.jobs.values(),return_exceptions=True)
            self.assertTrue(cancelled.is_set())
            for t in (a,b):
                state=self.store.task(t['id']);self.assertEqual(state['status'],'cancelled');self.assertEqual(state['answer'],'')
    async def test_fake_unknown_citation_is_rejected_and_quality_not_pass(self):
        class Liar:
            async def completion(this,*args):return {'content':'An invented claim [S999]'},{}
        service=Service(self.store,self.settings,Liar())
        async def mcp(*a):return {'result':{'branch':'demo'}}
        with patch('assistant.service.project_call',mcp):
            t=service.create(self.pid,'Widget structure','local');await service.jobs[t['id']]
            result=self.store.task(t['id']);self.assertEqual(result['status'],'failed');self.assertEqual(result['quality'],'NOT_ASSESSED')
    async def test_real_citation_does_not_become_semantic_pass(self):
        class Liar:
            async def completion(this,*args):return {'content':'An invented guarantee of 100% SLA [S1]'},{}
        service=Service(self.store,self.settings,Liar())
        async def mcp(*a):return {'result':{'branch':'demo'}}
        with patch('assistant.service.project_call',mcp):
            t=service.create(self.pid,'Widget structure','local');await service.jobs[t['id']]
            result=self.store.task(t['id']);self.assertEqual(result['status'],'completed');self.assertEqual(result['quality'],'NOT_ASSESSED')
            self.assertEqual(result['grounding'],'SOURCE_REFERENCES_UNVERIFIED')
    async def test_no_context_does_not_invoke_provider(self):
        class Forbidden:
            async def completion(this,*args):raise AssertionError('Unexpected inference')
        service=Service(self.store,self.settings,Forbidden())
        async def mcp(*a):return {'result':{'branch':'demo'}}
        with patch('assistant.service.project_call',mcp):
            t=service.create(self.pid,'astronomical unicorns','local');await service.jobs[t['id']]
            result=self.store.task(t['id']);self.assertEqual(result['grounding'],'INSUFFICIENT_CONTEXT')
    async def test_tool_budget_unknown_args_and_untrusted_tests(self):
        executor=Executor(self.root,self.store,self.pid,self.settings,max_calls=1)
        with self.assertRaises(ValueError):await executor.execute('shell',{})
        with self.assertRaises(ValueError):await executor.execute('read_project_file',{'path':'README.md','command':'anything'})
        self.assertTrue((await executor.execute('project_tree',{}))['files'])
        with self.assertRaises(ValueError):await executor.execute('project_tree',{})
        other=Executor(self.root,self.store,self.pid,self.settings)
        with self.assertRaises(ValueError):await other.execute('run_project_tests',{'suite':'unit'})
    async def test_repeated_tool_loop_stops(self):
        class Loop:
            async def completion(this,*args):return {'role':'assistant','content':'','tool_calls':[{'id':'call','type':'function','function':{'name':'project_tree','arguments':'{}'}}]},{}
        service=Service(self.store,self.settings,Loop())
        async def mcp(*a):return {'result':{'branch':'demo'}}
        with patch('assistant.service.project_call',mcp):
            t=service.create(self.pid,'Widget structure','local',allow_tools=True);await service.jobs[t['id']]
            result=self.store.task(t['id']);self.assertEqual(result['status'],'failed');self.assertIn('Repeated',result['error'])
    async def test_exhausted_tools_are_not_reoffered_and_final_answer_is_possible(self):
        observed=[]
        class Budget:
            async def completion(this,provider,messages,tools,partial):
                observed.append(bool(tools))
                if len(observed)==1:
                    calls=[{'id':str(i),'type':'function','function':{'name':'read_project_file','arguments':json.dumps({'path':'README.md','start_line':i+1})}} for i in range(4)]
                    return {'role':'assistant','content':'','tool_calls':calls},{}
                return {'role':'assistant','content':'Widget engine uses SQLite [S1].'},{}
        service=Service(self.store,self.settings,Budget())
        async def mcp(*a):return {'result':{'branch':'demo'}}
        with patch('assistant.service.project_call',mcp):
            t=service.create(self.pid,'Widget structure','local',allow_tools=True);await service.jobs[t['id']]
            result=self.store.task(t['id']);self.assertEqual(result['status'],'completed');self.assertEqual(observed,[True,False])
            self.assertEqual(len([e for e in result['events'] if e['kind']=='system']),4)
            self.assertEqual(result['quality'],'NOT_ASSESSED')
