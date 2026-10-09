import threading
import time
from types import SimpleNamespace
from pathlib import Path
import pytest
from app.config import load_settings
from app.errors import InvalidRequest
from app.service import ChatService
from app.optimization import OptimizationLab,CANDIDATE
from app.context.builder import ContextBuilder
from tests.integration.test_history_and_switch import StubGemma,StubProvider

def make(tmp_path):
    settings=load_settings({'DIALOGUE_DB_PATH':str(tmp_path/'chat.sqlite'),'GGUF_DIR':str(tmp_path)})
    manager=StubGemma();manager.gguf_path=str(tmp_path/'first.gguf');manager.context_tokens=8192
    service=ChatService(settings,local_provider=StubProvider('local','first'),network_provider=StubProvider('network','network'),gemma_manager=manager)
    lab=OptimizationLab(service);service.optimization=lab
    return service,lab

def test_lifecycle_rechecks_after_waiting_for_generation(tmp_path):
    s,lab=make(tmp_path);failures=[];s._generation_lock.acquire()
    def worker():
        try:s.unload_local()
        except InvalidRequest:failures.append('busy')
    thread=threading.Thread(target=worker);thread.start();time.sleep(.03);lab.busy=True;s._generation_lock.release();thread.join(2)
    assert failures==['busy'] and s.gemma.stops==0
    lab.busy=False;s.close()

def test_changed_model_clears_profile_and_verification(tmp_path):
    s,lab=make(tmp_path);(tmp_path/'other.gguf').write_bytes(b'fixture')
    lab.active=CANDIDATE;lab.last_runtime={'pid':1,'actual_context_tokens':6144};s.gemma.context_tokens=6144
    s.select_model('other.gguf')
    assert lab.active is None and lab.last_runtime is None and s.gemma.context_tokens==s.settings.gemma_context_tokens
    s.close()

def test_unloaded_descriptor_cannot_show_old_context_verification(tmp_path):
    s,lab=make(tmp_path);lab.last_runtime={'pid':42,'actual_context_tokens':6144};assert lab.state()['runtime'] is None;s.close()

def test_borrowed_context_cannot_be_changed(tmp_path):
    s,lab=make(tmp_path)
    s.settings=load_settings({'DIALOGUE_DB_PATH':str(tmp_path/'external.sqlite'),'AI_TEST_MODEL_KIND':'local','AI_TEST_MODEL_BASE_URL':'http://127.0.0.1:5555','AI_TEST_MODEL_NAME':'borrowed','AI_TEST_MODEL_API_KEY':'dummy'})
    with pytest.raises(InvalidRequest):lab.apply(CANDIDATE)
    assert s.gemma.starts==0 and s.gemma.stops==0;s.close()

def test_quote_choices_are_real_substrings_and_budgeted():
    source='Memory reading retrieves relevant information. Memory writing stores perceived facts.'
    messages,trace=ContextBuilder(max_context_chars=2000,prompt_template=CANDIDATE.prompt_template,quote_hints=True).build(question='How does memory reading work?',fragments=[{'text':source}],rag_enabled=True)
    assert sum(len(m.content) for m in messages)<=2000
    import re
    hints=messages[-1].content.split('Optional exact short quote choices')[1]
    for phrase in re.findall(r'"([^"]+)"',hints):assert phrase in trace['sources'][0]['quote']

def test_multi_module_retrieval_respects_scope_and_filter():
    from app.rag.retrieval import retrieve
    class Embed:
        def embed_query(self,q):return q
    class Index:
        def __init__(self):self.calls=[]
        def search(self,q,top_k=5,min_score=None):
            self.calls.append((q,top_k,min_score));return [{'chunk_id':q,'text':q}]
    index=Index();items=retrieve(index,Embed(),'Roles of profile, memory, planning and action modules?',top_k=5,min_score=.6)
    assert len(items)==5 and len(index.calls)==5 and all(call[2]==.6 for call in index.calls)
