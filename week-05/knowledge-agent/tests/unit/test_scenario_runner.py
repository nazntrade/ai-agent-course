"""Dispatcher boundary regressions, offline only; no actual model calls."""
import json
import os
from pathlib import Path
import sys
import pytest
from harness import scenario_runner as runner

REMOTE = {'AI_TEST_LIVE_POLICY':'allowed', 'AI_TEST_MODEL_KIND':'remote',
          'AI_TEST_MODEL_BASE_URL':'https://example.invalid/v1',
          'AI_TEST_MODEL_NAME':'fixture-model', 'AI_TEST_MODEL_API_KEY':'fixture-secret'}


def scenario(tmp_path, kind='offline', body='pass', declaration=None):
    directory = tmp_path / 'tests' / 'scenarios'
    directory.mkdir(parents=True)
    (directory / 'probe.py').write_text(body, encoding='utf-8')
    (directory / 'probe.json').write_text(json.dumps(declaration or {'schema_version':'test-scenario-v1','kind':kind}), encoding='utf-8')
    return directory

@pytest.mark.parametrize('args', [[], ['probe','extra'], ['../probe'], ['probe.py'], ['Probe'], ['проба'], ['a'*65], ['/probe'], ['a\\b']])
def test_dispatcher_rejects_arbitrary_args_and_paths(tmp_path, monkeypatch, args):
    monkeypatch.setattr(runner.runpy,'run_path',lambda *a,**k:pytest.fail('executed invalid slug'))
    assert runner.execute(tmp_path,args)==2

@pytest.mark.parametrize('policy', [None, 'forbidden', 'invalid'])
def test_live_denied_before_script_execution(tmp_path, monkeypatch, policy, capsys):
    scenario(tmp_path,kind='live')
    for key, value in REMOTE.items():monkeypatch.setenv(key,value)
    if policy is None:monkeypatch.delenv('AI_TEST_LIVE_POLICY')
    else:monkeypatch.setenv('AI_TEST_LIVE_POLICY',policy)
    monkeypatch.setattr(runner.runpy,'run_path',lambda *a,**k:pytest.fail('blocked LIVE executed'))
    assert runner.execute(tmp_path,['probe'])==3
    assert 'fixture-secret' not in capsys.readouterr().out

@pytest.mark.parametrize('change', [{'AI_TEST_MODEL_API_KEY':''}, {'AI_TEST_MODEL_KIND':'broken'}, {'AI_TEST_MODEL_BASE_URL':'https://user:secret@example.invalid/v1'}, {'AI_TEST_MODEL_BASE_URL':'https://example.invalid:bad/v1'}, {'AI_TEST_MODEL_LEASE_URL':'http://127.0.0.1:1/lease'}, {'AI_TEST_MODEL_KIND':'local','AI_TEST_MODEL_BASE_URL':'http://127.0.0.1:1234/v1'}])
def test_incomplete_or_invalid_live_profile_zero_execution(tmp_path,monkeypatch,change):
    scenario(tmp_path,kind='live')
    for key,value in {**REMOTE,**change}.items():monkeypatch.setenv(key,value)
    monkeypatch.setattr(runner.runpy,'run_path',lambda *a,**k:pytest.fail('invalid profile executed'))
    assert runner.execute(tmp_path,['probe'])==3

@pytest.mark.parametrize('local',[False,True])
def test_complete_selected_profile_executes_without_extra_arguments(tmp_path,monkeypatch,local):
    scenario(tmp_path,kind='live',body="import sys\nassert len(sys.argv)==1\nassert sys.argv[0].endswith('probe.py')")
    env=dict(REMOTE)
    if local:env.update(AI_TEST_MODEL_KIND='local',AI_TEST_MODEL_BASE_URL='http://127.0.0.1:1234/v1',AI_TEST_MODEL_ID='fixture',AI_TEST_MODEL_LEASE_URL='http://127.0.0.1:1234/api/local-models/fixture/test-leases')
    for key,value in env.items():monkeypatch.setenv(key,value)
    assert runner.execute(tmp_path,['probe'])==0

@pytest.mark.parametrize('ending,result',[('',0), ('raise SystemExit(7)',7), ('raise RuntimeError("fixture-secret")',1), ('raise KeyboardInterrupt()',130)])
def test_offline_clears_profile_and_restores_state_on_every_exit(tmp_path,monkeypatch,ending,result,capsys):
    body="""import os, sys
assert os.environ['AI_TEST_LIVE_POLICY']=='forbidden'
assert os.environ['KNOWLEDGE_SKIP_ENV_FILE']=='1'
assert not any(k.startswith(('AI_TEST_MODEL_','AI_TEST_TAVILY_')) for k in os.environ)
assert len(sys.argv)==1
os.environ['SCENARIO_CHANGED']='1'
sys.argv.append('changed')
sys.path.append('changed')
"""+ending
    scenario(tmp_path,body=body)
    for key,value in REMOTE.items():monkeypatch.setenv(key,value)
    monkeypatch.setenv('AI_TEST_TAVILY_API_KEY','fixture-search-secret')
    before_env=dict(os.environ)
    argv,path=sys.argv,sys.path
    argv_values,path_values=list(argv),list(path)
    main=sys.modules['__main__']
    assert runner.execute(tmp_path,['probe'])==result
    assert dict(os.environ)==before_env
    assert sys.argv is argv and sys.argv==argv_values
    assert sys.path is path and sys.path==path_values
    assert sys.modules['__main__'] is main
    assert 'fixture-secret' not in capsys.readouterr().out

@pytest.mark.parametrize('manifest',[{'schema_version':'bad','kind':'offline'}, {'schema_version':'test-scenario-v1','kind':'unknown'}, {'schema_version':'test-scenario-v1','kind':'offline','command':'anything'}, ['offline']])
def test_manifest_schema_exact(tmp_path,monkeypatch,manifest):
    scenario(tmp_path,declaration=manifest)
    monkeypatch.setattr(runner.runpy,'run_path',lambda *a,**k:pytest.fail('invalid manifest executed'))
    assert runner.execute(tmp_path,['probe'])==2


def test_linked_ancestor_and_linked_script_denied(tmp_path,monkeypatch):
    folder=scenario(tmp_path)
    original=Path.lstat
    # Synthetic Windows reparse flag verifies rejection on hosts without symlink privileges.
    class Reparse:
        st_mode=0o040755
        st_file_attributes=0x400
    monkeypatch.setattr(Path,'lstat',lambda self:Reparse() if self==folder.parent else original(self))
    monkeypatch.setattr(runner.runpy,'run_path',lambda *a,**k:pytest.fail('linked ancestor executed'))
    assert runner.execute(tmp_path,['probe'])==2
    class Linked:
        st_mode=0o120777
        st_file_attributes=0
    monkeypatch.setattr(Path,'lstat',lambda self:Linked() if self==folder/'probe.py' else original(self))
    assert runner.execute(tmp_path,['probe'])==2
