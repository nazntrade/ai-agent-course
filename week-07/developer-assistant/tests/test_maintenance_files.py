"""D34-03/07/10: real bytes, pagination, strict tools and hostile scope cases."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
import tracemalloc
import unittest
from unittest.mock import patch
from assistant.maintenance import Run,ScopeError,Conflict,ContextLimit,WorkspaceLease,digest,decoded,encoded
from assistant.maintenance_mcp import session,validate_arguments
from assistant.tools import unpack

class Files(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name);self.root=self.base/'project';self.root.mkdir();self.data=self.base/'data'
        (self.root/'README.md').write_text('hello\n',encoding='utf-8');(self.root/'api.py').write_text('\n'.join('fact '+str(i) for i in range(1,241)),encoding='utf-8')
        self.run=Run.create(self.data,self.root,'run','pid','cid','maintain','local')
    def tearDown(self):self.tmp.cleanup()
    def test_late_read_and_multiple_search_pages(self):
        r=self.run.call('read_file',{'path':'api.py','start_line':215,'count':10});self.assertIn('fact 220',r['text']);self.assertEqual(r['total_lines'],240)
        first=self.run.call('search_text',{'query':'fact','page_size':50});second=self.run.call('search_text',{'query':'fact','page_size':50,'cursor':first['next_cursor']})
        self.assertFalse(first['coverage_complete']);self.assertEqual(second['matches'][0]['line'],first['matches'][-1]['line']+1)
        matches=first['matches']+second['matches'];page=second
        for _ in range(20):
            if not page['next_cursor']:break
            page=self.run.search_text('fact',cursor=page['next_cursor'],page_size=50);matches+=page['matches']
        self.assertTrue(page['coverage_complete']);self.assertEqual([m['line'] for m in matches],list(range(1,241)))
        self.assertTrue(self.run.call('search_text',{'query':'not here'})['coverage_complete'])
    def test_persisted_draft_and_new_file_diff(self):
        r=self.run.call('read_file',{'path':'README.md'});self.run.call('apply_edit',dict(path='README.md',expected_hash=r['version'],old_text='hello',new_text='updated'))
        self.run.call('create_file',dict(path='docs/new.md',content='new actual bytes',expected_missing=True))
        reopened=Run(self.run.folder);diff=reopened.call('get_diff',{})
        self.assertEqual((reopened.folder/'draft/docs/new.md').read_bytes(),b'new actual bytes');self.assertIn('/dev/null',diff['text']);self.assertIn('-hello',diff['text'])
        self.assertEqual((self.root/'README.md').read_text(),'hello\n')
        with self.assertRaises(Conflict):self.run.call('apply_edit',dict(path='README.md',expected_hash=r['version'],old_text='updated',new_text='stale'))
    def test_denied_paths_schema_and_protected_rules(self):
        for path in ['../x.md','/x.md','C:/x.md','//host/share/x.md','a\\x.md','x.md:stream','CON.md','a./x.md','a//x.md','.env','settings.json','node_modules/x.py','local-data/data.md']:
            with self.subTest(path=path),self.assertRaises(ScopeError):self.run.create_file(path,'synthetic',True)
        with self.assertRaises(ScopeError):self.run.create_file('AGENTS.md','new rules',True)
        for name,args in [('read_file',{'path':'README.md','count':True}),('read_file',{'path':'README.md','count':201}),('list_files',{'shell':'x'}),('delete_file',{'path':'README.md'})]:
            with self.assertRaises(ValueError):validate_arguments(name,args)
    def test_stale_cursor_long_line(self):
        (self.root/'long.txt').write_text('X'*25000,encoding='utf-8');r=Run.create(self.data,self.root,'long','pid','cid','goal','local')
        first=r.read_file('long.txt');self.assertFalse(first['range_complete']);second=r.read_file('long.txt',continuation=first['next_continuation']);self.assertEqual(second['offset_chars'],12000)
        page=r.list_files(page_size=1);r.create_file('new.md','new',True)
        with self.assertRaises(Conflict):r.list_files(cursor=page['next_cursor'])
    def test_binary_oversized_missing_exclusions(self):
        (self.root/'binary.txt').write_bytes(b'\x00binary');(self.root/'huge.txt').write_bytes(b'x'*512001);(self.root/'.env').write_text('SYNTHETIC_NEVER_READ')
        r=Run.create(self.data,self.root,'cases','p','c','g','local')
        self.assertIn('binary',r.m['inventory']['exclusions']);self.assertIn('oversized',r.m['inventory']['exclusions'])
        for path in ['binary.txt','huge.txt','missing.txt']:
            with self.assertRaises(ScopeError):r.read_file(path)
        self.assertNotIn('.env',r.overlay())
    def test_links_case_hardlinks_and_writer(self):
        with WorkspaceLease(self.root):
            with self.assertRaises(Conflict):
                with WorkspaceLease(self.root):pass
        with self.assertRaises(ScopeError):self.run.read_file('readme.md')
        os.link(self.root/'README.md',self.root/'linked.md')
        with self.assertRaises(ScopeError):self.run.create_file('linked.md','x',True)
        try:os.symlink(self.base,self.root/'escape',target_is_directory=True)
        except OSError:return
        with self.assertRaises(ScopeError):self.run.create_file('escape/out.md','no',True)
    async def test_actual_sdk_typed_calls(self):
        async with session(self.run) as (client,offered,discovered):
            self.assertEqual(len(discovered),7)
            result=unpack(await client.call_tool('read_file',{'path':'api.py','start_line':220,'count':2}))
            self.assertIn('fact 220',result['text'])
            denied=unpack(await client.call_tool('create_file',{'path':'../outside.md','content':'x','expected_missing':True}))
            self.assertIn('error',denied)
            malformed=await client.call_tool('read_file',{'path':'README.md','shell':'synthetic'})
            self.assertTrue(malformed.is_error)
    async def test_actual_sdk_continuation_only_retains_nondefault_range(self):
        (self.root/'sdk-ranges.txt').write_bytes(('paragraph '+'x'*90+'\r\n').encode()*240)
        run=Run.create(self.data,self.root,'sdk-ranges','p','c','g','local');run.m['tool_result_budget_chars']=1800;run.save()
        async with session(run) as (client,offered,schemas):
            first=unpack(await client.call_tool('read_file',{'path':'sdk-ranges.txt','start_line':117,'count':110}));self.assertIsNotNone(first['next_continuation'])
            following=unpack(await client.call_tool('read_file',first['next_call']))
            self.assertEqual(following['original_range'],{'start_line':117,'count':110});self.assertEqual(following['offset_chars'],len(first['text']))
    def test_cancelled_generation_cannot_write(self):
        child=Run(self.run.folder,self.run.token);self.run.cancel()
        with self.assertRaises(Conflict):child.create_file('late.md','late',True)
        self.assertFalse((self.run.folder/'draft/late.md').exists())
    def test_dense_search_page_bounds_work_and_resumes_exact_offsets(self):
        (self.root/'dense.txt').write_bytes(b'a'*512000)
        r=Run.create(self.data,self.root,'dense','p','c','g','local');hash_sizes=[]
        def counted(blob):hash_sizes.append(len(blob));return digest(blob)
        started=time.monotonic();tracemalloc.start()
        try:
            with patch('assistant.maintenance.digest',side_effect=counted),patch.object(r,'version',wraps=r.version) as versions:
                first=r.call('search_text',{'query':'a','prefix':'dense.txt','page_size':1})
                self.assertEqual(versions.call_count,1)
                self.assertEqual(sum(n==512000 for n in hash_sizes),1)
            _,peak=tracemalloc.get_traced_memory()
        finally:tracemalloc.stop()
        elapsed=time.monotonic()-started
        self.assertLess(elapsed,15)
        self.assertLess(peak,8*1024*1024)
        print(json.dumps({'dense_search_bytes':512000,'page_size':1,'full_file_hashes_first_page':sum(n==512000 for n in hash_sizes),'peak_bytes':peak,'elapsed_s':round(elapsed,4)}))
        self.assertEqual(len(first['matches']),1);self.assertEqual(first['matches'][0]['offset'],0)
        self.assertFalse(first['coverage_complete']);self.assertIsNone(first['total_matches']);self.assertFalse(first['total_matches_exact'])
        second=r.call('search_text',{'query':'a','prefix':'dense.txt','page_size':1,'cursor':first['next_cursor']})
        self.assertEqual(second['matches'][0]['offset'],1);self.assertEqual(second['matches_emitted'],2)
        self.assertEqual(first['matches'][0]['sha256'],second['matches'][0]['sha256'])
        with self.assertRaises(Conflict):r.call('search_text',{'query':'b','prefix':'dense.txt','page_size':1,'cursor':first['next_cursor']})
    def test_search_exhaustion_totals_unicode_offsets_and_scan_budget(self):
        (self.root/'unicode.txt').write_text('Ёж ёж\nёж',encoding='utf-8')
        r=Run.create(self.data,self.root,'unicode','p','c','g','local')
        page=r.search_text('ЁЖ',page_size=2,prefix='unicode.txt')
        self.assertEqual([(x['line'],x['offset']) for x in page['matches']],[(1,0),(1,3)])
        final=r.search_text('ЁЖ',cursor=page['next_cursor'],page_size=2,prefix='unicode.txt')
        self.assertTrue(final['coverage_complete']);self.assertTrue(final['total_matches_exact']);self.assertEqual(final['total_matches'],3)
        for n in range(65):(self.root/f'scan-{n:03d}.txt').write_text('no match')
        r=Run.create(self.data,self.root,'scan-budget','p','c','g','local')
        first=r.search_text('absent',prefix='scan-');self.assertFalse(first['scan_complete']);self.assertEqual(first['loaded_files_this_page'],64)
        self.assertEqual(first['matches'],[]);self.assertIsNone(first['total_matches'])
        final=r.search_text('absent',prefix='scan-',cursor=first['next_cursor']);self.assertTrue(final['scan_complete']);self.assertEqual(final['scanned_files'],65);self.assertEqual(final['total_matches'],0)
    def test_host_context_caps_preserve_whole_read_search_and_diff_pages(self):
        source=('quoted " \\ text\n'*1000)+'LATE-END'
        (self.root/'paged.txt').write_text(source,encoding='utf-8');r=Run.create(self.data,self.root,'context-pages','p','c','g','local')
        r.m['tool_result_budget_chars']=2200;r.save();continuation=None;start=1;parts=[];pages=0
        while True:
            value=r.read_file('paged.txt',start_line=start,count=200,continuation=continuation);pages+=1
            self.assertLess(pages,100,'Read continuation did not progress');self.assertTrue(value.get('text'),value)
            self.assertLessEqual(len(json.dumps(value,ensure_ascii=False)),2200);parts.append(value['text'])
            if value['next_continuation']:continuation=value['next_continuation']
            elif value['next_start_line']:start=value['next_start_line'];continuation=None
            else:break
        self.assertGreater(pages,2);self.assertTrue(''.join(parts)==(self.root/'paged.txt').read_bytes().decode('utf-8'),'Continuation must reconstruct exact original bytes, including Windows CRLF')
        result=r.search_text('quoted',page_size=20,prefix='paged.txt');self.assertLess(result['effective_page_size'],20);self.assertIsNotNone(result['next_cursor']);self.assertFalse(result['coverage_complete'])
        self.assertLessEqual(len(json.dumps(result,ensure_ascii=False)),2200)
        r.create_file('report.md','line\n'*2000,True);cursor=None;text='';pages=0;first_token=None
        while True:
            page=r.call('get_diff',{'cursor':cursor} if cursor else {'path':'report.md'});self.assertLessEqual(len(json.dumps(page,ensure_ascii=False)),2200);text+=page['text'];pages+=1;cursor=page['next_cursor'];first_token=first_token or cursor
            self.assertLess(pages,100,'Diff continuation did not progress');self.assertTrue(page['text'],page)
            if not cursor:break
        self.assertGreater(pages,1);self.assertIn('/dev/null',text);self.assertEqual(text,r.diff()[1])
        with self.assertRaises(Conflict):r.call('get_diff',{'path':'api.py','cursor':first_token})
        altered=decoded(first_token);altered[3]=1000000
        with self.assertRaises(Conflict):r.call('get_diff',{'cursor':encoded(altered)})
        r.create_file('another.md','new revision',True)
        with self.assertRaises(Conflict):r.call('get_diff',{'cursor':first_token})
    def test_continuation_only_resumes_original_nondefault_range_and_rejects_wrong_scope(self):
        source=''.join(f'line {n}: '+'part '*24+'\r\n' for n in range(1,241))
        (self.root/'ranges.txt').write_bytes(source.encode());r=Run.create(self.data,self.root,'self-contained','p','c','g','local');r.m['tool_result_budget_chars']=2300;r.save()
        first=r.call('read_file',{'path':'ranges.txt','start_line':117,'count':110});self.assertIsNotNone(first['next_continuation']);token=first['next_continuation'];parts=[first['text']];page=first;pages=0
        while page['next_continuation']:
            page=r.call('read_file',{'path':'ranges.txt','continuation':page['next_continuation']});parts.append(page['text']);pages+=1;self.assertLess(pages,100)
            self.assertEqual(page['original_range'],{'start_line':117,'count':110})
        self.assertTrue(''.join(parts)==''.join(source.splitlines(keepends=True)[116:226]));self.assertEqual(page['next_call'],{'path':'ranges.txt','start_line':227,'count':110})
        page=r.call('read_file',{'path':'ranges.txt','count':200});parts=[page['text']];pages=0
        while page['next_call']:
            page=r.call('read_file',page['next_call']);parts.append(page['text']);pages+=1;self.assertLess(pages,100)
        self.assertTrue(''.join(parts)==source)
        with self.assertRaises(Conflict):r.call('read_file',{'path':'README.md','continuation':token})
        altered=decoded(token);altered[3]=1000000
        with self.assertRaises(Conflict):r.call('read_file',{'path':'ranges.txt','continuation':encoded(altered)})
        altered=decoded(token);altered[0]='other-run'
        with self.assertRaises(Conflict):r.call('read_file',{'path':'ranges.txt','continuation':encoded(altered)})
        r.create_file('new.md','new',True)
        with self.assertRaises(Conflict):r.call('read_file',{'path':'ranges.txt','continuation':token})
    def test_write_acknowledgement_budget_refuses_before_any_bytes_or_revision(self):
        r=self.run;r.m['tool_result_budget_chars']=20;r.save();before=r.m['revision']
        with self.assertRaises(ContextLimit):r.create_file('blocked.md','substantive content',True)
        self.assertEqual(r.reload()['revision'],before);self.assertFalse((r.folder/'draft/blocked.md').exists())

if __name__=='__main__':unittest.main()
