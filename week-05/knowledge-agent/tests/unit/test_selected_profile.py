"""D22-P01/P02/P03/P04 observable selected-profile regressions (MOCK)."""
import io
import json
import urllib.error
import pytest
from knowledge_agent.config import load_settings
from knowledge_agent.chat.test_profile import load_test_profile
from knowledge_agent.chat.openai_chat import OpenAIChatModel
from knowledge_agent.chat.ollama_chat import OllamaChatModel
from knowledge_agent.domain.contracts import ChatMessage
from knowledge_agent.domain.errors import ChatInvalidResponse, ChatUnavailable, ContextOverflow
from knowledge_agent.chat.chat_service import ChatService
from knowledge_agent.chat.run_store import FileChatRunStore
from tests.helpers import FakeChatModel, FakeKnowledge
from harness.test_profile import TestSession, SessionError

ENV = {'AI_TEST_MODEL_KIND': 'local', 'AI_TEST_MODEL_BASE_URL': 'http://127.0.0.1:8888/v1',
       'AI_TEST_MODEL_NAME': 'selected-chat', 'AI_TEST_MODEL_API_KEY': 'fixture-secret',
       'AI_TEST_MODEL_ID': 'model-1',
       'AI_TEST_MODEL_LEASE_URL': 'http://127.0.0.1:8888/api/local-models/model-1/test-leases'}
MSG = [ChatMessage('user', 'hello')]

@pytest.mark.parametrize('missing', ['KIND','BASE_URL','NAME','API_KEY'])
def test_partial_profile_rejects_before_any_fallback(missing):
    env = {**ENV, 'CHAT_MODEL': 'fallback'}
    env.pop('AI_TEST_MODEL_' + missing)
    with pytest.raises(ValueError, match='test_profile_invalid'):
        load_settings(env)

@pytest.mark.parametrize('lease', ['http://127.0.0.1:9999/api/local-models/model-1/test-leases',
    'http://127.0.0.1:8888/api/local-models/other/test-leases',
    'http://127.0.0.1:8888/api/local-models/model-1/test-leases?secret=1'])
def test_lifecycle_origin_id_and_query_guard(lease):
    with pytest.raises(ValueError):
        load_test_profile({**ENV, 'AI_TEST_MODEL_LEASE_URL': lease})

def test_profile_does_not_change_embedding_or_leak_auth():
    settings = load_settings({**ENV, 'CHAT_MODEL':'wrong'})
    assert settings.test_profile.name == 'selected-chat'
    assert settings.embed_model == 'embeddinggemma:300m'
    assert 'fixture-secret' not in repr(settings)
    assert load_settings({}).test_profile is None

def test_remote_and_legacy_local_ownership_are_distinct():
    profile = load_test_profile({**ENV,'AI_TEST_MODEL_KIND':'remote','AI_TEST_MODEL_LEASE_URL':''})
    assert profile.kind == 'remote'
    env = {**ENV,'AI_TEST_MODEL_LEASE_URL':''}
    assert load_test_profile(env).kind == 'local'
    with pytest.raises(SessionError):
        with TestSession(env):
            pass

@pytest.mark.parametrize('error', [None, RuntimeError, KeyboardInterrupt])
def test_owned_lease_released_success_error_interrupt(error):
    calls = []
    def opener(req, timeout):
        calls.append(req.method)
        return io.BytesIO(json.dumps({'lease_id':'owned'} if req.method == 'POST' else {}).encode())
    try:
        with TestSession(ENV, opener) as child_env:
            assert child_env['AI_TEST_MODEL_PARENT_READY'] == '1'
            assert child_env['AI_TEST_MODEL_LEASE_ID'] == 'owned'
            with TestSession(child_env, opener):
                pass
            if error:
                raise error()
    except BaseException as exc:
        assert error and isinstance(exc, error)
    assert calls == ['POST','DELETE']

def test_release_failure_is_not_a_silent_success(capsys):
    def opener(req, timeout):
        if req.method == 'DELETE':
            raise OSError('fixture-secret')
        return io.BytesIO(b'{"lease_id":"owned"}')
    with pytest.raises(SessionError):
        with TestSession(ENV, opener):
            pass
    assert 'fixture-secret' not in capsys.readouterr().out

def test_openai_actual_name_usage_finish_reason_no_secret_projections():
    calls=[]
    def opener(req, timeout):
        calls.append(req)
        if req.full_url.endswith('/models'):
            return io.BytesIO(b'{"data":[{"id":"selected-chat","context_length":2048}]}')
        return io.BytesIO(b'{"model":"selected-chat","choices":[{"message":{"content":"ok"},"finish_reason":"length"}],"usage":{"prompt_tokens":12,"completion_tokens":4,"total_tokens":16}}')
    profile = load_test_profile({**ENV,'AI_TEST_MODEL_LEASE_ID':'owned','AI_TEST_MODEL_PARENT_READY':'1'})
    model = OpenAIChatModel(profile, opener=opener)
    assert model.identity().context_length == 2048
    result=model.chat(MSG)
    assert result.finish_reason == 'length'
    assert result.usage.total_tokens == 16
    assert result.output_tokens_per_second is None
    assert calls[-1].get_header('X-ai-test-model-lease-id') == 'owned'
    assert 'fixture-secret' not in repr(result)
    payload = json.loads(calls[-1].data)
    assert payload['model']=='selected-chat' and payload['max_tokens']==1024

def test_incremental_sse_yields_before_reading_remaining_provider_bytes():
    class Response:
        consumed=0
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def __iter__(self):
            self.consumed += 1
            yield b'data: {"choices":[{"delta":{"content":"hello"},"finish_reason":null}]}\n'
            self.consumed += 1
            yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"completion_tokens":2}}\n'
            yield b'data: [DONE]\n'
    response = Response()
    model = OpenAIChatModel(load_test_profile(ENV),opener=lambda *a,**k:response)
    events=model.stream_chat(MSG)
    assert next(events)=={'type':'token','text':'hello'}
    assert response.consumed == 1
    done=next(events)
    assert done['result'].usage.input_tokens is None
    assert done['result'].usage.output_tokens==2

def test_real_ollama_transport_is_incremental_and_closes_on_cancel():
    class Response:
        closed=False
        def __iter__(self):
            yield b'{"message":{"content":"first"}}\n'
            raise AssertionError('consumer must receive first before remaining data')
        def close(self): self.closed=True
    response=Response()
    model=OllamaChatModel(model='local',stream_opener=lambda *a,**k:response)
    events=model.stream_chat(MSG)
    assert next(events)['text']=='first'
    events.close()
    assert response.closed

def test_without_rag_overflow_is_checked_before_inference(tmp_path):
    fake=FakeChatModel()
    service=ChatService(FakeKnowledge(),fake,FileChatRunStore(tmp_path),max_context_tokens=32,reserved_output_tokens=20)
    with pytest.raises(ContextOverflow):
        service.chat({'mode':'without_rag','question':'q'})
    assert fake.calls==[]

@pytest.mark.parametrize('error', [TimeoutError, OSError])
def test_provider_failure_during_body_read_is_sanitized(error):
    class Response:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self): raise error('fixture-secret')
    model=OpenAIChatModel(load_test_profile(ENV),opener=lambda *a,**k:Response())
    from knowledge_agent.domain.errors import ChatTimeout
    with pytest.raises((ChatTimeout,ChatUnavailable)) as caught:
        model.chat(MSG)
    assert 'fixture-secret' not in str(caught.value)

@pytest.mark.parametrize('error', [TimeoutError, OSError])
def test_provider_failure_after_first_stream_token_is_sanitized(error):
    class Response:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n'
            raise error('fixture-secret')
    model=OpenAIChatModel(load_test_profile(ENV),opener=lambda *a,**k:Response())
    events=model.stream_chat(MSG)
    assert next(events)['text']=='ok'
    from knowledge_agent.domain.errors import ChatTimeout
    with pytest.raises((ChatTimeout,ChatUnavailable)) as caught:
        next(events)
    assert 'fixture-secret' not in str(caught.value)

def test_omitted_seed_is_not_claimed_as_applied():
    model=OpenAIChatModel(load_test_profile(ENV),seed=42)
    assert model.default_options['seed'] is None
    assert model.default_options['seed_supported'] is False
    assert 'seed' not in model._payload(MSG,None,False)

def test_health_recovers_after_temporary_provider_outage():
    calls=[]
    def opener(*args,**kwargs):
        calls.append(1)
        if len(calls)==1:
            raise OSError('unreachable')
        return io.BytesIO(b'{"data":[{"id":"selected-chat"}]}')
    model=OpenAIChatModel(load_test_profile(ENV),opener=opener,preflight_ttl=0)
    assert model.preflight()['reachable'] is False
    assert model.preflight()['model_present'] is True

def test_local_identity_preserves_selected_id_without_path():
    model=OpenAIChatModel(load_test_profile({**ENV,'AI_TEST_MODEL_PATH':'private/model.gguf'}))
    assert model.default_options['local_model_id']=='model-1'
    assert 'private/model.gguf' not in repr(model.default_options)

def test_heartbeat_renewal_failure_still_releases_and_fails():
    calls=[]
    def opener(req,timeout):
        calls.append(req.method)
        if req.method=='POST': return io.BytesIO(b'{"lease_id":"owned"}')
        if req.method=='PATCH': raise OSError('fixture-secret')
        return io.BytesIO(b'{}')
    session=TestSession(ENV,opener)
    with pytest.raises(SessionError,match='renewal'):
        with session:
            session.stop.wait=lambda interval:False
            session._heartbeat(1)
    assert calls==['POST','PATCH','DELETE']

def test_only_provider_timing_yields_generation_rate():
    model=OpenAIChatModel(load_test_profile(ENV))
    import time
    result=model._result('ok','stop',{'completion_tokens':3},'selected',time.perf_counter(),{'predicted_per_second':21.5})
    assert result.output_tokens_per_second==21.5
    unknown=model._result('ok','stop',{'completion_tokens':3},'selected',time.perf_counter())
    assert unknown.output_tokens_per_second is None

@pytest.mark.parametrize('body,expected', [
    (b'{"error":"unsupported option fixture-secret"}', ChatUnavailable),
    (b'{"error":"context length exceeded fixture-secret"}', __import__('knowledge_agent.domain.errors',fromlist=['ChatLengthError']).ChatLengthError)])
def test_bad_options_are_not_misreported_as_context_length(body,expected):
    def opener(req,timeout):
        raise urllib.error.HTTPError(req.full_url,400,'Bad Request',{},io.BytesIO(body))
    model=OpenAIChatModel(load_test_profile(ENV),opener=opener)
    with pytest.raises(expected) as caught:
        model.chat(MSG)
    assert 'fixture-secret' not in str(caught.value)

@pytest.mark.parametrize('kind,advertised,expected', [('local',True,True),('local',False,False),('remote',True,False)])
def test_bounded_reasoning_only_for_advertised_local_capabilities(kind,advertised,expected):
    calls=[]
    def opener(req,timeout):
        if req.full_url.endswith('/models'):
            return io.BytesIO(json.dumps({'data':[{'id':'selected-chat','capabilities':
                {'reasoning_effort':advertised,'chat_template_kwargs':advertised}}]}).encode())
        calls.append(json.loads(req.data))
        return io.BytesIO(b'{"choices":[{"message":{"content":"answer"},"finish_reason":"stop"}]}')
    env={**ENV,'AI_TEST_MODEL_KIND':kind}
    if kind=='remote': env['AI_TEST_MODEL_LEASE_URL']=''
    model=OpenAIChatModel(load_test_profile(env),opener=opener,max_output_tokens=64)
    identity=model.identity()
    result=model.chat(MSG)
    assert result.text=='answer'
    assert calls[0]['max_tokens']==64
    if expected:
        assert calls[0]['reasoning_effort']=='none'
        assert calls[0]['chat_template_kwargs']=={'enable_thinking':False}
        assert identity.default_options['reasoning_effort']=='none'
    else:
        assert 'reasoning_effort' not in calls[0]
        assert 'chat_template_kwargs' not in calls[0]


def test_reasoning_only_length_preserves_limit_and_usage_without_exposing_reasoning():
    def opener(req,timeout):
        return io.BytesIO(b'{"choices":[{"message":{"content":"","reasoning_content":"private thought"},"finish_reason":"length"}],"usage":{"completion_tokens":64}}')
    model=OpenAIChatModel(load_test_profile(ENV),opener=opener)
    result=model.chat(MSG)
    assert result.text=='' and result.finish_reason=='length'
    assert result.usage.output_tokens==64
    assert 'private thought' not in repr(result)
