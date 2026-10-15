"""D34-04/05/06/15: immutable domain facts and real closed-interpreter behavior."""
import shutil
import re
import json
from pathlib import Path
import tempfile
import unittest
from assistant.config import MODULE
from assistant.maintenance import Run,digest
from assistant.maintenance_checks import PureClient,Unsupported,ticketdesk_checks

class Content(unittest.TestCase):
    def test_exact_contract_restore_keeps_history_and_reaches_behavior_vectors(self):
        self.fix()
        for path in ('tests/test_contract.py','ticket_api.py'):
            original=self.r.read_file(path,count=1);modified=original['text']+'\n# temporary changed contract\n';self.r.apply_edit(path,original['version'],original['text'],modified)
            self.assertEqual(ticketdesk_checks(self.r)['status'],'FAIL');changed_revision=self.r.m['revision'];changed=self.r.read_file(path,count=3)
            self.r.apply_edit(path,changed['version'],modified,original['text'])
            self.assertIn(path,self.r.m['draft']);self.assertEqual(self.r.m['draft'][path]['sha256'],original['sha256']);self.assertTrue((self.r.folder/'revisions'/str(changed_revision)/path).exists())
            result=ticketdesk_checks(self.r);self.assertEqual(result['status'],'PASS',result);self.assertNotIn(path,[entry['path'] for entry in self.r.get_diff()['files']]);self.assertEqual(self.r.m['quality'],'NOT_ASSESSED')
        v=self.r.read_file('ticket_client.py');self.r.apply_edit(v['path'],v['version'],'/resolve','/close');self.assertEqual(ticketdesk_checks(self.r)['status'],'FAIL')
    def test_effective_contract_hash_and_missing_bytes_fail_closed(self):
        self.fix()
        for path in ('ticket_api.py','tests/test_contract.py','PROJECT_GUIDE.md'):
            original,_=self.r.blob(path);sha=digest(original);target=self.r.folder/'draft'/path;target.parent.mkdir(parents=True,exist_ok=True)
            # Isolated host-state fixtures do not expand model writes to GUIDE.
            for content,recorded in ((original+b'\n# changed\n',digest(original+b'\n# changed\n')),(original+b'\n# forged\n',sha),(original,'0'*64)):
                target.write_bytes(content);self.r.m['draft'][path]={'sha256':recorded,'bytes':len(content),'revision':self.r.m['revision']}
                self.assertEqual(ticketdesk_checks(self.r)['status'],'FAIL')
            target.write_bytes(original);self.r.m['draft'][path]={'sha256':sha,'bytes':len(original),'revision':self.r.m['revision']};self.assertEqual(ticketdesk_checks(self.r)['status'],'PASS')
            target.unlink();self.assertEqual(ticketdesk_checks(self.r)['status'],'FAIL');self.r.m['draft'].pop(path)
    def test_versioned_fixed_source_oracle_counterexamples(self):
        from assistant.maintenance_checks import CHECKER_VERSION
        cases=json.loads((MODULE/'tests/fixtures/maintenance_oracle_v2.json').read_text(encoding='utf-8'))['cases'];self.assertEqual(len(cases),15)
        results=[]
        for case in cases:
            with self.subTest(case=case['id']):
                self.assertEqual(digest(case['source'].encode()),case['source_sha256'])
                root=self.base/case['id'];shutil.copytree(MODULE/'examples/maintenance_demo',root)
                if case.get('baseline_release_hash_mismatch'):(root/'RELEASE_NOTES.md').write_text('# Different truthful working-tree documentation\n')
                run=Run.create(self.base/('data-'+case['id']),root,'case','p','c','Align client','local')
                v=run.read_file('ticket_client.py');run.apply_edit(v['path'],v['version'],v['text'],case['source'])
                if case.get('release_draft'):
                    v=run.read_file('RELEASE_NOTES.md');run.apply_edit(v['path'],v['version'],v['text'],case['release_draft'])
                if case.get('forbidden_draft_target'):
                    v=run.read_file(case['forbidden_draft_target']);run.apply_edit(v['path'],v['version'],v['text'],v['text']+'\n# changed\n')
                result=ticketdesk_checks(run);expected='FAIL' if case['id'] in {'legacy-wire','wrong-route','guessed-version','obsolete-response-input','wrong-response-value','swallowed-conflict','modified-trusted-server','modified-contract-tests'} else 'PASS'
                self.assertEqual(result['status'],expected,result);self.assertEqual(result['checker_version'],CHECKER_VERSION);self.assertEqual(result['checker_sha256'],digest((MODULE/'assistant/maintenance_checks.py').read_bytes()));self.assertEqual(run.m['quality'],'NOT_ASSESSED');self.assertEqual(run.m['goal_review'],'NOT_ASSESSED')
                if case.get('unsupported_claims'):self.assertIn('All tests passed',case['release_draft']);self.assertIn('independently NOT_ASSESSED',result['limitations'][-1])
                if case.get('baseline_release_hash_mismatch'):self.assertFalse(result['release_provenance']['matches_original'])
                results.append({'id':case['id'],'behavior':result['status'],'quality':run.m['quality']})
        print(json.dumps({'checker_version':CHECKER_VERSION,'fixed_source_cases':results}))
    def test_updated_release_apply_new_run_noop_and_contract_guards(self):
        cases=json.loads((MODULE/'tests/fixtures/maintenance_oracle_v2.json').read_text(encoding='utf-8'))['cases'];case=next(c for c in cases if c['id']=='valid-client-with-truthful-changelog')
        for path,text in (('ticket_client.py',case['source']),('RELEASE_NOTES.md',case['release_draft'])):
            v=self.r.read_file(path);self.r.apply_edit(path,v['version'],v['text'],text)
        self.assertEqual(ticketdesk_checks(self.r)['status'],'PASS');self.r.run_fixed_checks();diff=self.r.get_diff();self.r.review(diff['revision'],diff['digest']);self.r.transact(diff['revision'],diff['digest'])
        updated=Run.create(self.base/'updated-data',self.root,'updated','p','c','Repeat on updated workspace','local');result=ticketdesk_checks(updated);self.assertEqual(result['status'],'PASS',result);self.assertFalse(result['release_provenance']['matches_original']);self.assertFalse(updated.m['draft'])
        updated.run_fixed_checks();d=updated.get_diff();self.assertFalse(d['files']);updated.review(d['revision'],d['digest']);updated.transact(d['revision'],d['digest']);self.assertEqual(updated.m['application'],'NOOP')
        for path in ('ticket_api.py','tests/test_contract.py','PROJECT_GUIDE.md'):
            original=(self.root/path).read_bytes();(self.root/path).write_bytes(original+b'\n# foreign change\n')
            changed=Run.create(self.base/('changed-'+Path(path).name),self.root,'changed','p','c','Check fixture identity','local');self.assertEqual(ticketdesk_checks(changed)['status'],'NOT_ASSESSED');(self.root/path).write_bytes(original)
    def test_wrong_wire_content_errors_filters_and_unknown_ast_are_not_accepted(self):
        cases=json.loads((MODULE/'tests/fixtures/maintenance_oracle_v2.json').read_text(encoding='utf-8'))['cases'];good=next(c['source'] for c in cases if c['id']=='raw-legacy')
        wrong=[good.replace('"resolution": note','"note": note'),good.replace('"resolution": note','"resolution": "changed"'),good.replace('if response["status_code"] != 200:','if False:'),good.replace('and (assignee is None or ticket["assignee"] == assignee)','and True'),good.replace('ticket_id, note, version','ticket_id, renamed_note, version')]
        for source in wrong:
            v=self.r.read_file('ticket_client.py');self.r.apply_edit(v['path'],v['version'],v['text'],source);self.assertEqual(ticketdesk_checks(self.r)['status'],'FAIL')
        v=self.r.read_file('ticket_client.py');self.r.apply_edit(v['path'],v['version'],v['text'],good.replace('return {"method"','return unknown_call({"method"',1).replace('"expected_version": version}}','"expected_version": version}})',1));self.assertEqual(ticketdesk_checks(self.r)['status'],'NOT_ASSESSED')
    def test_live_docs_observation_accepts_changed_release_notes_only(self):
        from tests.maintenance_live import documentation_artifacts_changed
        required={'README.md','docs/API.md'}
        for note in ('RELEASE_NOTES.md','CHANGELOG.md','docs/changelog-release.md','docs/release-notes.md'):
            self.assertTrue(documentation_artifacts_changed(required|{note}))
        self.assertFalse(documentation_artifacts_changed(required),'Unchanged existing/draft-only release notes are absent from actual changed paths')
        self.assertFalse(documentation_artifacts_changed(required|{'RELEASE_NOTES.txt'}))
        self.assertFalse(documentation_artifacts_changed({'README.md','RELEASE_NOTES.md'}))
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name);self.root=self.base/'project';shutil.copytree(MODULE/'examples/maintenance_demo',self.root)
        self.r=Run.create(self.base/'data',self.root,'run','p','c','g','local')
    def tearDown(self):self.tmp.cleanup()
    def fix(self):
        self.edit('"/api/tickets/{ticket_id}/close"','"/api/tickets/{ticket_id}/resolve"')
        self.edit('"json": {"note": note, "version": version}','"json": {"resolution": note.strip(), "expected_version": version}')
        self.edit('"state": body["state"], "note": body["note"]','"status": body["status"], "resolution": body["resolution"]')
    def edit(self,old,new):
        v=self.r.read_file('ticket_client.py');self.r.apply_edit('ticket_client.py',v['version'],old,new)
    def test_fixture_truth_late_line_and_search_pages(self):
        lines=(self.root/'ticket_api.py').read_text().splitlines();self.assertGreater(next(i for i,s in enumerate(lines,1) if 'not resolution.strip()' in s),200)
        p=self.r.search_text('resolve',page_size=20);self.assertIsNotNone(p['next_cursor']);matches=p['matches'];pages=1
        for _ in range(20):
            if not p['next_cursor']:break
            p=self.r.search_text('resolve',cursor=p['next_cursor'],page_size=50);matches+=p['matches'];pages+=1
        expected=[(path,line_number,match.start()) for path in self.r.overlay() for line_number,line in enumerate((self.root/path).read_text(encoding='utf-8').splitlines(),1) for match in re.finditer('resolve',line,re.IGNORECASE)]
        self.assertGreaterEqual(pages,2);self.assertTrue(p['coverage_complete']);self.assertEqual([(m['path'],m['line'],m['offset']) for m in matches],expected)
    def test_real_adapter_behavior_before_after_and_static_separation(self):
        before=ticketdesk_checks(self.r);self.assertEqual(before['status'],'FAIL')
        self.fix();result=ticketdesk_checks(self.r);self.assertEqual(result['status'],'PASS',result)
        self.assertEqual(self.r.m['quality'],'NOT_ASSESSED');self.assertEqual(self.r.m['goal_review'],'NOT_ASSESSED')
    def test_wrong_but_valid_cited_report_never_accepted(self):
        self.r.create_file('migration.md','Use POST /api/tickets/{id}/close. Tests passed [ticket_api.py@123].',True)
        result=self.r.run_fixed_checks('static');self.assertEqual(result['static']['status'],'PASS')
        self.assertEqual(result['quality'],'NOT_ASSESSED');self.assertEqual(result['goal_review'],'NOT_ASSESSED')
    def test_weakened_model_tests_cannot_make_pass(self):
        v=self.r.read_file('tests/test_contract.py');self.r.apply_edit(v['path'],v['version'],'self.assertEqual(r["body"],{"id":"T-1","status":"resolved","resolution":"done","version":2})','self.assertTrue(True)')
        self.assertEqual(ticketdesk_checks(self.r)['status'],'FAIL')
    def test_unrelated_filter_drift_fails(self):
        self.fix();self.edit('and (assignee is None or ticket["assignee"] == assignee)','and True')
        result=ticketdesk_checks(self.r);self.assertEqual(result['status'],'FAIL');self.assertTrue(any('filter' in x for x in result['failures']))
    def test_arbitrary_execution_never_reaches_python(self):
        for source in ['import os\nos.system("synthetic")','def build_resolution(x):\n return x.__class__','def build_resolution(x):\n return open(x)','def filter_tickets(x):\n while True: pass']:
            with self.subTest(source=source):
                try:client=PureClient(source);client.call(next(iter(client.functions)),[]) 
                except (Unsupported,KeyError):pass
                else:self.fail('Unsupported execution accepted')
    def test_generic_other_project_has_honest_unsupported_checks(self):
        other=self.base/'other';other.mkdir();(other/'README.md').write_text('Catalog API /items')
        r=Run.create(self.base/'other-data',other,'other','p','c','Document catalog','local')
        self.assertEqual(r.list_files()['files'][0]['path'],'README.md');self.assertEqual(ticketdesk_checks(r)['status'],'NOT_ASSESSED')

if __name__=='__main__':unittest.main()
