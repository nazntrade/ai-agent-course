from app.context.builder import ContextBuilder
from app.config import load_settings
from app.providers.external_http import ExternalHttpProvider
from app.providers.base import ChatMessage
import io,json

def test_large_evidence_fits_and_trace_matches_prompt():
    b=ContextBuilder(max_context_chars=2000)
    messages,trace=b.build(question="Explain planning",rag_enabled=True,fragments=[{"text":"evidence "*1000,"chunk_id":"first"},{"text":"omitted","chunk_id":"second"}])
    assert sum(len(m.content) for m in messages)<=2000
    assert trace["omitted_count"]==1
    assert trace["sources"][0]["truncated"]
    assert trace["sources"][0]["quote"] in messages[-1].content
    assert messages[-1].content.endswith("Explain planning")

def test_invalid_profile_never_counts_as_complete():
    s=load_settings({"AI_TEST_MODEL_KIND":"wrong","AI_TEST_MODEL_NAME":"x","AI_TEST_MODEL_BASE_URL":"file:///x","AI_TEST_MODEL_API_KEY":"test"})
    assert s.external_profile_partial()
    assert not s.external_profile_complete()
    assert len(s.external_profile_errors())==2

def test_lifecycle_marker_alone_is_not_absent_profile():
    s=load_settings({"AI_TEST_MODEL_LEASE_ID":"test-lease"})
    assert s.external_profile_partial()

def test_external_request_passes_lease_without_leaking_key_in_identity():
    requests=[]
    def opener(request,timeout):
        requests.append(request)
        return io.BytesIO(json.dumps({"choices":[{"message":{"content":"answer"},"finish_reason":"stop"}],"model":"actual"}).encode())
    p=ExternalHttpProvider(base_url="http://localhost/v1",model_id="configured",api_key="private-test",lease_id="lease-one",opener=opener)
    result=p.chat([ChatMessage("user","question")])
    assert requests[0].get_header("X-ai-test-model-lease-id")=="lease-one"
    assert result.model=="actual"
    assert "private-test" not in json.dumps(p.identity())

def test_one_correct_quote_cannot_hide_fabricated_quote_with_same_reference():
    from app.rag.citations import CitationVerifier
    result=CitationVerifier([{"text":"real evidence","chunk_id":"a"}]).verify('"real evidence" [1] and "invented evidence" [1]')
    assert result.status != "verified"
    assert len(result.citations)==2

def test_grounding_correction_is_bounded_and_keeps_failed_attempt():
    from app.rag.grounded_answer import generate_grounded
    from app.providers.base import ChatResult
    class Provider:
        calls=0
        def chat(self,messages):
            self.calls+=1
            return ChatResult('"invented" [99]',"same-model","stop",None,2,{})
    provider=Provider()
    result=generate_grounded(provider,[ChatMessage("user","question")],[{"text":"real evidence","chunk_id":"a"}])
    assert provider.calls==2
    assert len(result.parameters["grounding_attempts"])==2
    assert result.parameters["grounding_attempts"][-1]["citation_check"]["status"]=="failed"
    assert result.parameters["semantic_quality"]=="NOT_ASSESSED"
    assert result.latency_ms==4

def test_reading_view_preserves_original_index_text_and_origin():
    original="left right column mixed"
    messages,trace=ContextBuilder().build(question="question",rag_enabled=True,fragments=[{"chunk_id":"same-id","text":original,"reading_view":"left column. right column.","reading_locations":[{"page":6,"column":1}]}])
    assert "left column. right column." in messages[-1].content
    assert trace["sources"][0]["original_index_text"]==original
    assert trace["sources"][0]["chunk_id"]=="same-id"
    assert trace["sources"][0]["reading_locations"]==[{"page":6,"column":1}]

def test_reading_aid_never_displaces_original_fragments():
    original1="original first evidence "*20
    original2="original second evidence "*20
    messages,trace=ContextBuilder(max_context_chars=2700).build(question="question",rag_enabled=True,fragments=[{"text":original1,"reading_view":"supplement "*1000},{"text":original2}])
    assert original1 in messages[-1].content
    assert original2 in messages[-1].content
    assert len(trace["sources"])==2
    assert sum(len(m.content) for m in messages)<=2700
