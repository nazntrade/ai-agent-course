"""Owned evaluation lifecycle regressions; model/provider effects are mocked."""
import dataclasses
import hashlib
import json
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace
import pytest
from harness import rag_eval_live as runner
from knowledge_agent.config import load_settings
from knowledge_agent.domain.contracts import ChatModelIdentity

ENV={'AI_TEST_LIVE_POLICY':'allowed','AI_TEST_MODEL_KIND':'remote',
     'AI_TEST_MODEL_BASE_URL':'http://127.0.0.1:9999/v1','AI_TEST_MODEL_NAME':'selected-model',
     'AI_TEST_MODEL_API_KEY':'fixture-secret'}

@pytest.mark.parametrize('policy',['forbidden','invalid'])
def test_forbidden_runner_zero_effects_even_with_stale_profile(monkeypatch,policy):
    monkeypatch.setenv('AI_TEST_LIVE_POLICY',policy)
    monkeypatch.setenv('AI_TEST_MODEL_KIND','broken')
    def forbidden(*a,**k): pytest.fail('blocked runner performed an effect')
    for name in ('load_test_profile','load_settings','selected_source','TestSession','run_owned'):
        monkeypatch.setattr(runner,name,forbidden)
    monkeypatch.setattr(runner.tempfile,'mkdtemp',forbidden)
    monkeypatch.setattr(runner.urllib.request,'urlopen',forbidden)
    assert runner.main([])==3


def test_missing_selected_profile_does_not_fallback_to_old_backend(monkeypatch):
    for key in tuple(runner.os.environ):
        if key.startswith('AI_TEST_MODEL_'): monkeypatch.delenv(key)
    monkeypatch.setenv('AI_TEST_LIVE_POLICY','allowed')
    monkeypatch.setenv('CHAT_MODEL','stale-model')
    monkeypatch.setenv('KNOWLEDGE_PORT','8770')
    monkeypatch.setattr(runner,'TestSession',lambda *a,**k:pytest.fail('lease/network invoked'))
    assert runner.main([])==3


def test_source_is_explicit_registered_corpus_not_arbitrary_collection(tmp_path,monkeypatch):
    label='registered.pdf'
    registered=tmp_path/'local-data'/'input'/label
    registered.parent.mkdir(parents=True)
    registered.write_bytes(b'fixture')
    monkeypatch.setattr(runner,'MODULE_DIR',tmp_path)
    monkeypatch.setattr(runner.rag_eval,'load_questions',lambda p:{'corpus':{'label':label},'questions':[{}]*10})
    source,name=runner.selected_source(load_settings(ENV))
    assert source==registered.resolve() and name==label
    other=tmp_path/'other.pdf'
    other.write_bytes(b'other')
    with pytest.raises(runner.RunnerBlocked):
        runner.selected_source(load_settings({**ENV,'KNOWLEDGE_SOURCE_PATH':str(other)}))


def test_session_ready_environment_used_for_backend(monkeypatch,tmp_path):
    for key in tuple(runner.os.environ):
        if key.startswith('AI_TEST_MODEL_') or key=='AI_TEST_LIVE_POLICY': monkeypatch.delenv(key)
    env={**ENV,'AI_TEST_MODEL_KIND':'local','AI_TEST_MODEL_ID':'fixture',
         'AI_TEST_MODEL_LEASE_URL':'http://127.0.0.1:9999/api/local-models/fixture/test-leases'}
    for key,value in env.items():monkeypatch.setenv(key,value)
    events=[]
    class Session:
        def __init__(self,values): assert values['AI_TEST_MODEL_NAME']=='selected-model'
        def __enter__(self):
            events.append('enter')
            return {**env,'AI_TEST_MODEL_LEASE_ID':'owned','AI_TEST_MODEL_PARENT_READY':'1'}
        def __exit__(self,*args):events.append('release')
    monkeypatch.setattr(runner,'TestSession',Session)
    monkeypatch.setattr(runner,'selected_source',lambda s:(tmp_path/'registered.pdf','registered.pdf'))
    def run(settings,source,label):
        assert settings.test_profile.lease_id=='owned'
        assert settings.test_profile.name=='selected-model'
        assert settings.embed_model=='embeddinggemma:300m'
        events.append('run')
        return 0
    monkeypatch.setattr(runner,'run_owned',run)
    assert runner.main([])==0
    assert events==['enter','run','release']


def owned_fixture(tmp_path,monkeypatch):
    monkeypatch.setenv('AI_TEST_LIVE_POLICY','allowed')
    monkeypatch.setattr(runner,'MODULE_DIR',tmp_path)
    monkeypatch.setattr(runner.tempfile,'gettempdir',lambda:str(tmp_path))
    source=tmp_path/'registered.pdf'
    source.write_bytes(b'fixture corpus')
    digest=hashlib.sha256(source.read_bytes()).hexdigest()
    events=[]
    store=SimpleNamespace(close=lambda:events.append('store-close'))
    class Service:
        _embedder=SimpleNamespace(preflight=lambda:{'reachable':True,'model_present':True,'version':'0.35.0'})
        def create_collection(self,name):return {'collection_id':'owned-collection'}
        def build(self,collection,sources,strategy,wait):
            assert sources==[{'path':str(source)}] and wait and strategy=='structure'
            events.append('build')
            return {'index_version_id':'owned-index'}
        def get_index_version(self,id):
            return {'status':'ready','index_version_id':id,'strategy':'structure','model':'embeddinggemma:300m',
                    'manifest':{'sources':[{'content_sha256':digest}]}}
        def set_active_index(self,collection,index):
            assert collection=='owned-collection' and index=='owned-index'
    service=Service()
    def build(settings):
        assert Path(settings.db_path).parent!=tmp_path/'user-data'
        assert Path(settings.db_path).parent.name.startswith('knowledge-rag-eval-')
        assert Path(settings.chat_runs_path).parent==tmp_path/'local-data'/'chat-runs'
        return service,store
    model=SimpleNamespace(preflight=lambda:{'reachable':True,'model_present':True},
        identity=lambda:ChatModelIdentity('openai-compatible','', 'selected-model',
            default_options={'model_check_kind':'remote'}))
    monkeypatch.setattr(runner,'build_service',build)
    monkeypatch.setattr(runner,'build_chat_service',lambda *a:SimpleNamespace(chat_model=model))
    class Backend:
        def __init__(self,*args):pass
        def start(self):events.append('start');return 'http://127.0.0.1:18421'
        def stop(self):events.append('stop')
    monkeypatch.setattr(runner,'OwnedBackend',Backend)
    monkeypatch.setattr(runner,'verify_health',lambda base,profile:events.append('health'))
    return source,events

@pytest.mark.parametrize('result',[0,1,'interrupt'])
def test_eval_success_failure_interrupt_clean_only_owned_backend_and_temp(tmp_path,monkeypatch,result):
    source,events=owned_fixture(tmp_path,monkeypatch)
    user_data=tmp_path/'user-data'
    user_data.mkdir()
    user_db=user_data/'index.db'
    user_db.write_bytes(b'unchanged')
    def evaluate(base,collection,output,model):
        assert base!='http://127.0.0.1:8770' and collection=='owned-collection'
        events.append('eval')
        if result=='interrupt':raise KeyboardInterrupt()
        if result==0:
            pairs=[]
            for i in range(10):
                branches={}
                for mode in ('with_rag','without_rag'):
                    id=f'owned-{i}-{mode}'
                    (output/(id+'.json')).write_text('{}')
                    branches[mode]={'run_id':id}
                pairs.append(branches)
            (output/'eval-summary-fixture.json').write_text(json.dumps({'pairs':pairs}))
        return result
    monkeypatch.setattr(runner,'evaluate',evaluate)
    assert runner.run_owned(load_settings(ENV),source,source.name)==(130 if result=='interrupt' else result)
    assert events[-2:]==['stop','store-close']
    assert not list(tmp_path.glob('knowledge-rag-eval-*'))
    assert user_db.read_bytes()==b'unchanged'
    report=list((tmp_path/'local-data'/'chat-runs').glob('rag-eval-*/rag-eval-receipt.json'))[0]
    receipt=json.loads(report.read_text())
    assert receipt['cleanup_status']=='PASS' and receipt['quality_status']=='NOT_ASSESSED'
    assert receipt['pairs_completed']==(10 if result==0 else 0)
    assert 'fixture-secret' not in report.read_text() and str(tmp_path) not in report.read_text()


def test_backend_drain_failure_keeps_store_and_temp_intact(tmp_path,monkeypatch):
    source,events=owned_fixture(tmp_path,monkeypatch)
    class Stuck:
        def __init__(self,*args):pass
        def start(self):return 'http://127.0.0.1:18421'
        def stop(self):raise runner.CleanupFailed('not drained')
    monkeypatch.setattr(runner,'OwnedBackend',Stuck)
    monkeypatch.setattr(runner,'evaluate',lambda *a:1)
    assert runner.run_owned(load_settings(ENV),source,source.name)==1
    assert 'store-close' not in events
    assert list(tmp_path.glob('knowledge-rag-eval-*'))


def test_ready_index_rejects_other_source_and_not_ready_status():
    active=[]
    service=SimpleNamespace(create_collection=lambda name:{'collection_id':'owned'},
        build=lambda *a,**k:{'index_version_id':'idx'},
        get_index_version=lambda id:{'status':'ready','index_version_id':id,'manifest':{'sources':[{'content_sha256':'other'}]}},
        set_active_index=lambda *a:active.append(a))
    with pytest.raises(runner.RunnerBlocked):runner.ready_index(service,Path('registered.pdf'),'expected')
    assert active==[]


def test_owned_backend_prebound_free_port_and_cleanup_without_external_process(monkeypatch):
    import uvicorn
    seen=[]
    class FakeServer:
        started=False
        should_exit=False
        def __init__(self,config):pass
        def run(self,sockets):
            seen.append(sockets[0].getsockname())
            self.started=True
            while not self.should_exit:time.sleep(.005)
    monkeypatch.setattr(runner,'create_app',lambda *a:object())
    def config(*args,**kwargs):
        assert kwargs['timeout_graceful_shutdown'] is None
        return object()
    monkeypatch.setattr(uvicorn,'Config',config)
    monkeypatch.setattr(uvicorn,'Server',FakeServer)
    backend=runner.OwnedBackend(object(),object())
    base=backend.start()
    assert base.startswith('http://127.0.0.1:')
    assert seen[0][1]!=8770
    backend.stop()
    assert not backend.thread.is_alive() and backend.socket.fileno()==-1
