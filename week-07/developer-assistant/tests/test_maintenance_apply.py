"""D34-08/09/12: guarded batches, immutable dependencies, restart recovery."""
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from assistant.maintenance import Run,Conflict,WorkspaceLease,workspace_guard,workspace_state,durable

class Apply(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name);self.root=self.base/'project';self.root.mkdir();self.data=self.base/'data'
        (self.root/'a.md').write_text('a');(self.root/'b.md').write_text('b');(self.root/'dependency.md').write_text('dep')
        self.r=Run.create(self.data,self.root,'run','p','c','g','local')
        for path in ['a.md','b.md']:
            v=self.r.read_file(path);self.r.apply_edit(path,v['version'],path[0],path[0]+' changed')
        self.r.run_fixed_checks();self.diff=self.r.get_diff();self.r.review(self.diff['revision'],self.diff['digest'])
    def tearDown(self):self.tmp.cleanup()
    def apply(self,**kw):return self.r.transact(self.diff['revision'],self.diff['digest'],**kw)
    def test_apply_undo_real_bytes_and_later_edits(self):
        self.apply();self.assertEqual((self.root/'a.md').read_text(),'a changed')
        (self.root/'b.md').write_text('user later')
        with self.assertRaises(Conflict):self.r.undo()
        self.assertEqual((self.root/'a.md').read_text(),'a changed')
        (self.root/'b.md').write_text('b changed');self.r.undo();self.assertEqual((self.root/'a.md').read_text(),'a')
    def test_read_dependency_conflict_changes_nothing(self):
        self.r.read_file('dependency.md');(self.root/'dependency.md').write_text('new dep')
        with self.assertRaises(Conflict):self.apply()
        self.assertEqual((self.root/'a.md').read_text(),'a')
    def test_search_new_consumer_conflict(self):
        self.r.search_text('dep');(self.root/'consumer.py').write_text('new = 1')
        with self.assertRaises(Conflict):self.apply()
        self.assertEqual((self.root/'b.md').read_text(),'b')
    def test_stale_review_target_and_noop(self):
        (self.root/'a.md').write_text('owner changed')
        with self.assertRaises(Conflict):self.apply()
        with self.assertRaises(Conflict):self.r.transact(0,'x'*64)
        r=Run.create(self.data,self.root,'noop','p','c','g','local');r.run_fixed_checks();d=r.get_diff();r.review(d['revision'],d['digest']);r.transact(d['revision'],d['digest']);self.assertEqual(r.m['application'],'NOOP')
    def test_fault_points_and_guarded_recovery(self):
        for point,index in [('prepared',None),('intent',0),('replaced',0),('receipt',0),('replaced',1)]:
            def fault(actual,i):
                if (actual,i)==(point,index):raise RuntimeError('Injected interruption')
            with self.assertRaises(RuntimeError):self.apply(fault=fault)
            reopened=Run(self.r.folder);self.assertEqual(reopened.m['stage'],'recovery_required')
            with self.assertRaises(Conflict):workspace_guard(self.root)
            reopened.recover();self.assertEqual((self.root/'a.md').read_text(),'a');self.assertEqual((self.root/'b.md').read_text(),'b')
            self.r=Run(self.r.folder);self.r.m['journal']=None;self.r.m['stage']='draft_ready';self.r.save()
    def test_foreign_recovery_preserved(self):
        def crash(point,index):
            if point=='replaced' and index==0:raise RuntimeError('crash')
        with self.assertRaises(RuntimeError):self.apply(fault=crash)
        (self.root/'a.md').write_text('later owner edit')
        with self.assertRaises(Conflict):Run(self.r.folder).recover()
        self.assertEqual((self.root/'a.md').read_text(),'later owner edit')
    def test_owned_process_crash_restart(self):
        result=subprocess.run([sys.executable,'-m','tests.test_maintenance_apply','--crash',str(self.r.folder),str(self.diff['revision']),self.diff['digest']],capture_output=True)
        self.assertEqual(result.returncode,77)
        reopened=Run(self.r.folder);self.assertEqual(reopened.m['journal']['status'],'prepared');reopened.recover()
        self.assertEqual((self.root/'a.md').read_text(),'a');self.assertEqual((self.root/'b.md').read_text(),'b')
    def test_new_file_undo_removes_only_exact_ours(self):
        self.r.create_file('new.md','ours',True);self.r.run_fixed_checks();d=self.r.get_diff();self.r.review(d['revision'],d['digest']);self.r.transact(d['revision'],d['digest']);self.r.undo();self.assertFalse((self.root/'new.md').exists())
    def test_cross_process_writer_is_denied(self):
        with WorkspaceLease(self.root):
            result=subprocess.run([sys.executable,'-m','tests.test_maintenance_apply','--lease',str(self.root)],capture_output=True)
        self.assertEqual(result.returncode,23)
    def test_stop_between_owner_transaction_writes(self):
        def stop(point,index):
            if point=='receipt' and index==0:Run(self.r.folder).cancel()
        with self.assertRaises(Conflict):self.apply(fault=stop)
        reopened=Run(self.r.folder);self.assertEqual(reopened.m['stage'],'recovery_required');self.assertEqual((self.root/'b.md').read_text(),'b')
        reopened.recover();self.assertEqual((self.root/'a.md').read_text(),'a')
    def test_fresh_noop_preserves_latest_real_batch_undo(self):
        self.apply();marker=workspace_state(self.root).read_bytes()
        second=Run.create(self.data,self.root,'no-op-after-apply','p','c','Repeat updated project','local')
        second.read_file('dependency.md');second.search_text('changed');revision=second.m['revision']
        second.run_fixed_checks();d=second.get_diff();second.review(d['revision'],d['digest']);second.transact(d['revision'],d['digest'])
        self.assertEqual(second.m['application'],'NOOP');self.assertEqual(second.m['revision'],revision)
        self.assertEqual(workspace_state(self.root).read_bytes(),marker)
        self.r.undo();self.assertEqual((self.root/'a.md').read_text(),'a');self.assertEqual((self.root/'b.md').read_text(),'b')
    def test_noop_still_checks_same_revision_read_dependencies(self):
        self.apply();marker=workspace_state(self.root).read_bytes()
        second=Run.create(self.data,self.root,'noop-dependency','p','c','Repeat updated project','local')
        second.read_file('dependency.md');second.run_fixed_checks();d=second.get_diff();second.review(d['revision'],d['digest'])
        (self.root/'dependency.md').write_text('concurrent change')
        with self.assertRaises(Conflict):second.transact(d['revision'],d['digest'])
        self.assertEqual(workspace_state(self.root).read_bytes(),marker);self.assertEqual((self.root/'a.md').read_text(),'a changed')
    def tree(self):return {p.relative_to(self.root).as_posix():p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
    def test_catchable_second_replace_failure_cleans_own_stage_and_whole_tree_recovers(self):
        lookalike=self.root/'a.md.user.tmp';lookalike.write_bytes(b'foreign lookalike');before=self.tree();original=OSError('owned second replacement denied');replace=os.replace;seen=[]
        def denied(src,dst):
            if Path(dst)==self.root/'b.md':seen.append((Path(src),Path(src).read_bytes()));raise original
            return replace(src,dst)
        with patch('assistant.maintenance.os.replace',side_effect=denied):
            with self.assertRaises(OSError) as caught:self.apply()
        self.assertIs(caught.exception,original);self.assertEqual(seen[0][1],b'b changed');self.assertFalse(seen[0][0].exists());self.assertEqual((self.root/'a.md').read_bytes(),b'a changed');self.assertEqual((self.root/'b.md').read_bytes(),b'b')
        reopened=Run(self.r.folder);self.assertEqual(reopened.m['stage'],'recovery_required');reopened.recover();self.assertEqual(self.tree(),before)
    def test_stage_collision_fsync_and_write_failures_preserve_destination_and_foreign_files(self):
        target=self.root/'new-target.md';foreign=self.root/'new-target.md.fixed.tmp';foreign.write_bytes(b'foreign collision')
        with patch('assistant.maintenance.uuid.uuid4',return_value=SimpleNamespace(hex='fixed')):
            with self.assertRaises(FileExistsError):durable(target,b'ours')
        self.assertEqual(foreign.read_bytes(),b'foreign collision');self.assertFalse(target.exists())
        target.write_bytes(b'before');before=self.tree()
        failure=OSError('fsync failure')
        with patch('assistant.maintenance.os.fsync',side_effect=failure):
            with self.assertRaises(OSError) as caught:durable(target,b'after')
        self.assertIs(caught.exception,failure);self.assertEqual(self.tree(),before)
        actual_open=Path.open;failure=OSError('write failure')
        class WriteFailure:
            def __init__(self,handle):self.handle=handle
            def __enter__(self):self.handle.__enter__();return self
            def __exit__(self,*args):return self.handle.__exit__(*args)
            def fileno(self):return self.handle.fileno()
            def write(self,data):raise failure
        def failing_open(path,*args,**kwargs):
            handle=actual_open(path,*args,**kwargs)
            return WriteFailure(handle) if args and args[0]=='xb' else handle
        with patch('assistant.maintenance.Path.open',autospec=True,side_effect=failing_open):
            with self.assertRaises(OSError) as caught:durable(target,b'after')
        self.assertIs(caught.exception,failure);self.assertEqual(self.tree(),before)
    def test_cleanup_failure_keeps_original_error_and_blocks_clean_recovery_claim(self):
        replace=os.replace;unlink=Path.unlink;original=OSError('replacement denied');stages=[]
        def denied(src,dst):
            if Path(dst)==self.root/'b.md':stages.append(Path(src));raise original
            return replace(src,dst)
        def deny_cleanup(path,*args,**kwargs):
            if path in stages:raise PermissionError('cleanup denied')
            return unlink(path,*args,**kwargs)
        with patch('assistant.maintenance.os.replace',side_effect=denied),patch('assistant.maintenance.Path.unlink',autospec=True,side_effect=deny_cleanup):
            with self.assertRaises(OSError) as caught:self.apply()
        self.assertIs(caught.exception,original);self.assertTrue(original.__notes__);self.assertEqual(stages[0].read_bytes(),b'b changed')
        reopened=Run(self.r.folder);self.assertEqual(reopened.m['journal']['staging_residues'][0]['path'],str(stages[0]))
        with self.assertRaisesRegex(Conflict,'Known staging residue'):reopened.recover()
        self.assertEqual(reopened.m['stage'],'recovery_required');self.assertEqual(reopened.m['application'],'PARTIAL_OR_UNCERTAIN');self.assertTrue(stages[0].exists())
    def test_recovery_cleanup_failure_survives_next_recovery_and_whole_tree_comparison(self):
        (self.root/'foreign-stage.tmp').write_bytes(b'foreign');before=self.tree()
        def interrupt(point,index):
            if point=='replaced' and index==1:raise RuntimeError('interrupted after both replacements')
        with self.assertRaises(RuntimeError):self.apply(fault=interrupt)
        replace=os.replace;unlink=Path.unlink;original=OSError('rollback replace denied');stages=[]
        def denied(src,dst):
            if Path(dst)==self.root/'b.md':stages.append(Path(src));raise original
            return replace(src,dst)
        def deny_cleanup(path,*args,**kwargs):
            if path in stages:raise PermissionError('rollback stage cleanup denied')
            return unlink(path,*args,**kwargs)
        with patch('assistant.maintenance.os.replace',side_effect=denied),patch('assistant.maintenance.Path.unlink',autospec=True,side_effect=deny_cleanup):
            with self.assertRaises(OSError) as caught:Run(self.r.folder).recover()
        self.assertIs(caught.exception,original);self.assertTrue(original.__notes__)
        reopened=Run(self.r.folder);self.assertEqual(reopened.m['journal']['staging_residues'][0],original.staging_cleanup_failure)
        self.assertEqual(reopened.m['stage'],'recovery_required');self.assertEqual(reopened.m['application'],'PARTIAL_OR_UNCERTAIN')
        residue=stages[0];self.assertEqual(residue.read_bytes(),b'b')
        with self.assertRaisesRegex(Conflict,'Known staging residue'):reopened.recover()
        expected=dict(before);expected[residue.relative_to(self.root).as_posix()]=b'b'
        self.assertEqual(self.tree(),expected);self.assertEqual(Run(self.r.folder).m['journal']['status'],'recovery_required')
        self.assertNotEqual(Run(self.r.folder).m['application'],'ROLLED_BACK')
        residue.unlink() # Explicit owner resolution of this exact known fixture file.
        Run(self.r.folder).recover();self.assertEqual(self.tree(),before)
    def test_substituted_stage_is_not_unlinked(self):
        target=self.root/'identity.md';original=OSError('replace failed after foreign substitution');substituted=[]
        def swap(src,dst):
            src=Path(src);moved=src.with_name(src.name+'.owned-moved');src.rename(moved);src.write_bytes(b'foreign substitute');substituted.extend([src,moved]);raise original
        with patch('assistant.maintenance.os.replace',side_effect=swap):
            with self.assertRaises(OSError) as caught:durable(target,b'ours')
        self.assertIs(caught.exception,original);self.assertEqual(substituted[0].read_bytes(),b'foreign substitute');self.assertEqual(substituted[1].read_bytes(),b'ours');self.assertTrue(original.staging_cleanup_failure)

if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--lease':
        try:
            with WorkspaceLease(sys.argv[2]):pass
        except Conflict:sys.exit(23)
        sys.exit(0)
    elif len(sys.argv)>1 and sys.argv[1]=='--crash':
        r=Run(sys.argv[2])
        def crash(point,index):
            if point=='replaced' and index==0:os._exit(77)
        r.transact(int(sys.argv[3]),sys.argv[4],fault=crash)
    else:unittest.main()
