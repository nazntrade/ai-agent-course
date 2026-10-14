"""Real isolated MCP boundary preflight; no provider or external notification."""
import tempfile
from pathlib import Path
import unittest
from assistant.config import MODULE
from assistant.support_store import SupportStore,Conflict
from assistant.support_mcp import context_call,minimal_env

class SupportBoundary(unittest.IsolatedAsyncioTestCase):
    async def test_real_sdk_reads_current_new_ticket_and_followup(self):
        with tempfile.TemporaryDirectory() as d:
            s=SupportStore(Path(d),MODULE/'examples/support/users-tickets.json')
            t=s.create('demo-alex','New request','My local server cannot connect','local',{'connection_state':'connection_refused'})
            r=s.create_run('demo-alex',t['id'],1,'What should I check?','local')
            path,expected=s.snapshot(r)
            result=await context_call(path,'demo-alex',t['id'],expected)
            self.assertEqual(set(result['discovered_tools']),{'get_user_context','get_ticket_context'})
            self.assertEqual(result['ticket']['id'],t['id']);self.assertEqual(result['context_version'],2)
            self.assertIn('What should I check?',result['history'][-1]['text'])
            s.cancel('demo-alex',r['id']);r2=s.create_run('demo-alex',t['id'],2,'I already restarted it','local')
            p2,e2=s.snapshot(r2);result2=await context_call(p2,'demo-alex',t['id'],e2)
            self.assertEqual(result2['context_version'],3);self.assertNotEqual(e2['sha256'],expected['sha256'])
            self.assertIn('already restarted',result2['history'][-1]['text'])

    async def test_wrong_ticket_and_malformed_json_remain_errors(self):
        with tempfile.TemporaryDirectory() as d:
            s=SupportStore(Path(d),MODULE/'examples/support/users-tickets.json')
            r=s.create_run('demo-alex','demo-auth',1,'Help','local');p,e=s.snapshot(r)
            with self.assertRaises(ValueError):await context_call(p,'demo-alex','demo-index',e)
            p.write_text('{bad',encoding='utf-8')
            with self.assertRaises(ValueError):await context_call(p,'demo-alex','demo-auth',e)

    def test_scope_version_secret_and_seed_restart(self):
        with tempfile.TemporaryDirectory() as d:
            s=SupportStore(Path(d),MODULE/'examples/support/users-tickets.json')
            with self.assertRaises(ValueError):s.ticket('demo-jamie','demo-auth')
            t=s.create('demo-alex','Safe title','api_key=pretend-secret-token and Bearer long-fake-bearer')
            self.assertNotIn('pretend-secret-token',t['description'])
            r=s.create_run('demo-alex',t['id'],1,'Hello','local')
            with self.assertRaises(Conflict):s.create_run('demo-alex',t['id'],2,'Duplicate','local')
            s2=SupportStore(Path(d),MODULE/'examples/support/users-tickets.json')
            self.assertEqual(s2.run('demo-alex',r['id'])['status'],'failed')
            self.assertEqual(len(s2.ticket('demo-alex',t['id'])['messages']),2)
            self.assertEqual(s2.ticket('demo-alex',t['id'])['state'],'open')
            self.assertFalse(any('KEY' in k or 'TOKEN' in k or 'SECRET' in k or 'TELEGRAM' in k for k in minimal_env()))
