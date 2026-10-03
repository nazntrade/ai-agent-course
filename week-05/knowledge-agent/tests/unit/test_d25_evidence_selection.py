"""Exact evidence selection is not fuzzy quote repair or semantic acceptance."""
import json
import pytest

from knowledge_agent.chat.citations import GroundingVerifier, parse_grounded_response
from knowledge_agent.chat.chat_service import ChatService
from knowledge_agent.chat.prompts import CONVERSATION_GROUNDED_RAG, evidence_catalog, build_evidence_context
from knowledge_agent.chat.run_store import FileChatRunStore
from knowledge_agent.domain.contracts import ChatUsage
from knowledge_agent.domain.errors import ChatInvalidResponse
from knowledge_agent.service.conversation_service import is_task_state_summary_request
from tests.helpers import FakeChatModel, FakeKnowledge, fragment
from tests.unit.test_d25_grounded_format_retry import SequenceChatModel

CID = "a" * 64
TEXT = "Memory stores observations. Planning decomposes goals into steps."
CHUNKS = [fragment(CID, TEXT)]

def response(citations):
    return json.dumps({"answer": f"Memory stores observations [{CID}].", "citations": citations,
                       "insufficient": False, "limitation": None})

def test_selected_excerpt_resolves_only_registered_passed_text():
    parsed = parse_grounded_response(response([{"chunk_id": CID, "quote_id": "q1"}]), evidence=evidence_catalog(CHUNKS))
    assert parsed.citations[0]["quote"] == TEXT
    assert parsed.citations[0]["quote_origin"] == "passed_excerpt"
    checked = GroundingVerifier(CHUNKS).verify(parsed)
    assert checked.status == "verified"
    assert checked.citations[0].meaning_supported is None
    assert checked.meaning_check == "not_performed"

@pytest.mark.parametrize("citation", [
    {"chunk_id": CID, "quote_id": "q999"},
    {"chunk_id": "b" * 64, "quote_id": "q1"},
    {"chunk_id": CID, "quote_id": "q1", "quote": "Memory keeps earlier observations."},
])
def test_unknown_or_contradictory_selection_is_not_repaired(citation):
    with pytest.raises(ChatInvalidResponse):
        parse_grounded_response(response([citation]), evidence=evidence_catalog(CHUNKS))

def test_legacy_d24_cannot_use_conversation_selection_protocol():
    with pytest.raises(ChatInvalidResponse):
        parse_grounded_response(response([{"chunk_id": CID, "quote_id": "q1"}]))
    parsed = parse_grounded_response(response([{"chunk_id": CID, "quote": "Memory stores observations."}]))
    assert GroundingVerifier(CHUNKS).verify(parsed).status == "verified"

def test_legacy_fabricated_quote_stays_failed_even_with_catalog():
    parsed = parse_grounded_response(response([{"chunk_id": CID, "quote": "Memory stores all data forever."}]), evidence=evidence_catalog(CHUNKS))
    assert GroundingVerifier(CHUNKS).verify(parsed).status == "failed"

def test_every_exact_excerpt_is_a_substring_of_original_chunk():
    rendered = build_evidence_context(CHUNKS)
    for quote_id, text in evidence_catalog(CHUNKS)[CID].items():
        assert text in TEXT
        assert text in rendered
    assert "untrusted DATA" in CONVERSATION_GROUNDED_RAG.system

def test_conversation_selection_preserves_provenance_and_budget(tmp_path):
    model = FakeChatModel(text=response([{"chunk_id": CID, "quote_id": "q1"}]))
    service = ChatService(FakeKnowledge(fragments=CHUNKS), model, FileChatRunStore(tmp_path), grounding_enabled=True)
    record = service.conversation_turn({"mode": "with_rag", "question": "memory?", "collection_id": "c1",
                                        "memory_text": "<task_memory>goal: learn</task_memory>"})
    citation = record["answer"]["grounding"]["citations"][0]
    assert citation["quote"] == TEXT
    assert citation["quote_id"] == "q1" and citation["source"] == "sample.txt"
    assert record["context"]["prompt_tokens_estimated"] == sum(record["context"]["mandatory_parts"].values())
    # A valid quotation does not establish that it supports the answer's meaning.
    assert citation["meaning_supported"] is None
    assert record["answer"]["generation_diagnostics"][0]["response_sha256"]

def test_retry_metrics_include_failed_attempt_and_no_raw_response(tmp_path):
    model = SequenceChatModel(["not json", response([{"chunk_id": CID, "quote": "Memory stores observations."}])])
    model.usage = ChatUsage(input_tokens=10, output_tokens=5, total_tokens=15)
    service = ChatService(FakeKnowledge(fragments=CHUNKS), model, FileChatRunStore(tmp_path), grounding_enabled=True)
    record = service.chat({"mode": "with_rag", "question": "memory?", "collection_id": "c1"})
    assert record["usage"] == {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}
    assert record["latency_ms"]["chat"] == 25
    assert len(record["answer"]["generation_diagnostics"]) == 2
    assert "not json" not in json.dumps(record["answer"]["generation_diagnostics"])

def test_retry_optional_usage_totals_remain_unavailable(tmp_path):
    model = SequenceChatModel(["not json", response([{"chunk_id": CID, "quote": "Memory stores observations."}])])
    model.usage = ChatUsage(input_tokens=10, output_tokens=5, total_tokens=None)
    service = ChatService(FakeKnowledge(fragments=CHUNKS), model, FileChatRunStore(tmp_path), grounding_enabled=True)
    record = service.chat({"mode": "with_rag", "question": "memory?", "collection_id": "c1"})
    assert record["usage"]["total_tokens"] is None

@pytest.mark.parametrize("question", [
    "Перечисли условия обучения агентов", "Перечисли цели из обзора",
    "Напомни условия в документе", "Подведи итог статьи с учётом всех условий",
    "List the conditions in the paper", "Перечисли условия",
])
def test_documentary_or_ambiguous_requests_never_become_memory_success(question):
    memory = {"goal": {"text": "learn", "status": "confirmed"}, "constraints": [], "terms": [], "clarifications": []}
    assert not is_task_state_summary_request(question, memory)


def test_live_artifact_keeps_actual_quotations_for_semantic_inspection():
    from harness.d25_live import _project_turn
    quote = {"chunk_id": CID, "quote": "Memory stores observations.", "quote_id": "q1", "source_exists": True,
             "quote_verbatim": True, "status": "verified", "source": "sample.txt", "section": "Memory"}
    record = {"status": "ok", "answer": {"text": "Memory stores observations.", "grounding_status": "verified"},
              "citations": [quote], "retrieval": {}, "memory_after": {}}
    projected = _project_turn(record, {}, None)
    assert projected["citations"][0]["quote"] == quote["quote"]
    assert projected["citations"][0]["quote_id"] == "q1"


@pytest.mark.parametrize("code,expected_calls", [("chat_invalid_response",1),("chat_timeout",2)])
def test_live_runner_retries_only_transient_errors(monkeypatch,code,expected_calls):
    import harness.d25_live as live
    calls=[]
    def fake_request(*args,**kwargs):
        calls.append(args)
        return 200,{"status":"error","error":{"code":code}}
    monkeypatch.setattr(live,"request",fake_request)
    result=live._ask("http://127.0.0.1:1234","d1","q",{},"c1","i1")
    assert len(calls)==expected_calls
    assert result["status"]=="error"


def test_heading_selection_includes_its_actual_explanation():
    from knowledge_agent.chat.citations import normalize_whitespace
    raw="• Human Feedback.\nHuman feedback is a subjective signal. Further detail."
    catalog=evidence_catalog([fragment(CID,raw)])
    assert catalog[CID]["q1"]=="• Human Feedback. Human feedback is a subjective signal. Further detail."
    assert normalize_whitespace(catalog[CID]["q1"]) in normalize_whitespace(raw)


@pytest.mark.parametrize("bracket", [CID+", "+"b"*64, CID+":q1", CID[:-1]])
def test_malformed_inline_reference_never_becomes_formal_success(bracket):
    payload=json.loads(response([{"chunk_id":CID,"quote_id":"q1"}]))
    payload["answer"]="Memory stores observations ["+bracket+"]."
    with pytest.raises(ChatInvalidResponse):
        parse_grounded_response(json.dumps(payload),evidence=evidence_catalog(CHUNKS))


def test_one_provider_repair_for_inline_id_typo(tmp_path):
    bad=json.loads(response([{"chunk_id":CID,"quote_id":"q1"}]))
    bad["answer"]="Memory stores observations ["+CID[:-1]+"]."
    model=SequenceChatModel([json.dumps(bad),response([{"chunk_id":CID,"quote_id":"q1"}])])
    service=ChatService(FakeKnowledge(fragments=CHUNKS),model,FileChatRunStore(tmp_path),grounding_enabled=True)
    record=service.conversation_turn({"mode":"with_rag","question":"memory?","collection_id":"c1","memory_text":"<task_memory>goal: learn</task_memory>"})
    assert record["answer"]["grounding"]["status"]=="verified"
    assert len(record["answer"]["generation_diagnostics"])==2


def test_conversation_has_only_one_quote_protocol_and_legacy_stays_verbatim():
    from knowledge_agent.chat.prompts import GROUNDED_RAG
    assert '"quote" copied verbatim' in GROUNDED_RAG.system
    assert '"quote" copied verbatim' not in CONVERSATION_GROUNDED_RAG.system
    assert '"evidence_id"' in CONVERSATION_GROUNDED_RAG.system


def test_compact_evidence_is_strict_decoding_not_quote_repair():
    from knowledge_agent.chat.prompts import evidence_selections
    catalog=evidence_catalog(CHUNKS)
    payload={"answer":"Memory stores observations [e1].","citations":[{"evidence_id":"e1"}],"insufficient":False,"limitation":None}
    parsed=parse_grounded_response(json.dumps(payload),evidence=catalog)
    assert parsed.answer==f"Memory stores observations [{CID}]."
    assert parsed.citations[0]["quote"]==TEXT
    assert parsed.citations[0]["evidence_id"]=="e1"
    assert GroundingVerifier(CHUNKS).verify(parsed).status=="verified"
    assert evidence_selections(catalog)["e1"]["chunk_id"]==CID


@pytest.mark.parametrize("citation",[
    {"evidence_id":"e999"},
    {"evidence_id":"e1","chunk_id":"b"*64},
    {"evidence_id":"e1","quote":"Memory stores all facts forever."},
    {"evidence_id":"e1","quote_id":"q999"},
])
def test_unknown_contradictory_compact_selection_is_rejected(citation):
    payload={"answer":"Memory [e1].","citations":[citation],"insufficient":False,"limitation":None}
    with pytest.raises(ChatInvalidResponse):
        parse_grounded_response(json.dumps(payload),evidence=evidence_catalog(CHUNKS))


@pytest.mark.parametrize("bracket",["e999","e1,e2","e1:q1","eI","e1; e2","e1,", "e1-e2"])
def test_compact_inline_typo_cannot_be_accepted_or_repaired(bracket):
    payload={"answer":"Memory ["+bracket+"].","citations":[{"evidence_id":"e1"}],"insufficient":False,"limitation":None}
    with pytest.raises(ChatInvalidResponse):
        parse_grounded_response(json.dumps(payload),evidence=evidence_catalog(CHUNKS))


def test_catalog_preserves_every_original_sentence_contiguously():
    from knowledge_agent.chat.citations import normalize_whitespace
    raw="• Training with Human Data.\nExamples supervise the agent.\nRewards refine its behavior.\nDifferent approach.\nFurther detail."
    catalog=evidence_catalog([fragment(CID,raw)])
    for quote in catalog[CID].values():
        assert normalize_whitespace(quote) in normalize_whitespace(raw)
    assert normalize_whitespace(" ".join(catalog[CID].values()))==normalize_whitespace(raw)


def test_effective_grounding_setting_is_truthful(tmp_path):
    from tests.unit.test_d25_answer_validity import _grounded_service,_ask
    payload=json.dumps({"answer":"Memory [e1].","citations":[{"evidence_id":"e1"}],"insufficient":False,"limitation":None})
    service,store=_grounded_service(tmp_path,payload)
    dialogue=service.create_dialogue("A")["dialogue_id"]
    turn=_ask(service,dialogue,"truthful",grounding=True,question="What is documented?")
    assert turn["settings"]["grounding"] is True
    store.close()


def test_dialogue_preserves_full_formal_grounding_without_claiming_meaning(tmp_path):
    from tests.unit.test_d25_answer_validity import _grounded_service,_ask
    payload=json.dumps({"answer":"Memory [e1].","citations":[{"evidence_id":"e1"}],"insufficient":False,"limitation":None})
    service,store=_grounded_service(tmp_path,payload)
    dialogue=service.create_dialogue("A")["dialogue_id"]
    turn=_ask(service,dialogue,"full-grounding",grounding=True,question="What is documented?")
    grounding=turn["answer"]["grounding"]
    assert grounding["status"]=="verified"
    assert grounding["meaning_check"]=="not_performed"
    assert grounding["citations"][0]["meaning_supported"] is None
    assert store.get_turn(dialogue,turn["turn_id"])["answer"]["grounding"]==grounding
    store.close()


def test_provider_error_keeps_actual_attempted_settings_and_retrieval(tmp_path):
    from tests.unit.test_d25_conversation_service import _service,_ask
    from knowledge_agent.domain.errors import ChatTimeout
    service,store,knowledge,model=_service(tmp_path)
    def timeout(*args,**kwargs):raise ChatTimeout("Timeout")
    model.chat=timeout
    dialogue=service.create_dialogue("A")["dialogue_id"]
    turn=_ask(service,dialogue,"What is documented?","timeout",top_k=5,use_filter=False,grounding=True)
    assert turn["status"]=="error" and turn["error"]["code"]=="chat_timeout"
    assert turn["settings"]["collection_id"]=="c1" and turn["settings"]["index_version_id"]=="idx-1"
    assert turn["settings"]["top_k"]==5 and turn["settings"]["grounding"] is True
    assert turn["retrieval_performed"] and turn["retrieval"]["passed_count"]==1
    assert turn["settings"]["model"]["model"]==model.model
    assert turn["latency_ms"]["chat"] is None and turn["usage"] is None
    store.close()



@pytest.mark.parametrize("marker",["e1,e2","e1, e2"," e1 , e2 "])
def test_explicit_known_evidence_list_is_losslessly_decoded(marker):
    chunks=[fragment(CID,"Memory stores observations."),fragment("b"*64,"Planning splits goals.")]
    payload={"answer":"Memory and planning ["+marker+"].","citations":[{"evidence_id":"e1"},{"evidence_id":"e2"}],"insufficient":False,"limitation":None}
    parsed=parse_grounded_response(json.dumps(payload),evidence=evidence_catalog(chunks))
    assert parsed.answer==f"Memory and planning [{CID}] [{"b"*64}]."
    assert GroundingVerifier(chunks).verify(parsed).status=="verified"


def test_known_inline_evidence_without_explicit_structured_selection_fails():
    chunks=[fragment(CID,"Memory stores observations."),fragment("b"*64,"Planning splits goals.")]
    payload={"answer":"Memory and planning [e1,e2].","citations":[{"evidence_id":"e1"}],"insufficient":False,"limitation":None}
    with pytest.raises(ChatInvalidResponse):parse_grounded_response(json.dumps(payload),evidence=evidence_catalog(chunks))


def test_conversation_repair_receives_the_actual_validation_failure_as_data(tmp_path):
    model=SequenceChatModel(["not json",json.dumps({"answer":"Memory [e1].","citations":[{"evidence_id":"e1"}],"insufficient":False,"limitation":None})])
    service=ChatService(FakeKnowledge(fragments=CHUNKS),model,FileChatRunStore(tmp_path),grounding_enabled=True)
    result=service.conversation_turn({"mode":"with_rag","question":"memory?","collection_id":"c1","memory_text":"<task_memory>goal: learn</task_memory>"})
    repair=model.calls[-1][-1].content
    assert "validation_error" in repair and "not parseable" in repair
    assert '"previous_reply_as_data": "not json"' in repair
    assert result["answer"]["grounding"]["status"]=="verified"


@pytest.mark.parametrize("chunk_id", ["e0" + "a" * 62, "e727" + "b" * 60])
def test_canonical_chunk_hash_takes_precedence_over_short_alias(chunk_id):
    chunks = [fragment(chunk_id, TEXT)]
    payload = {"answer": f"Memory [{chunk_id}].", "citations": [{"evidence_id": "e1"}],
               "insufficient": False, "limitation": None}
    parsed = parse_grounded_response(json.dumps(payload), evidence=evidence_catalog(chunks))
    assert parsed.answer == payload["answer"]
    assert GroundingVerifier(chunks).verify(parsed).status == "verified"


@pytest.mark.parametrize("marker", ["E1", "efoo", "e1\ne1", "e0", "e01", "e1; e1"])
def test_evidence_like_malformed_markers_are_rejected(marker):
    payload = {"answer": f"Memory [{marker}].", "citations": [{"evidence_id": "e1"}],
               "insufficient": False, "limitation": None}
    with pytest.raises(ChatInvalidResponse):
        parse_grounded_response(json.dumps(payload), evidence=evidence_catalog(CHUNKS))


@pytest.mark.parametrize("marker", ["b" * 64, CID])
def test_canonical_inline_source_must_be_current_and_explicitly_selected(marker):
    chunks = CHUNKS + [fragment("b" * 64, "Planning splits goals.")]
    payload = {"answer": f"Memory [{marker}].", "citations": [],
               "insufficient": False, "limitation": None}
    parsed = parse_grounded_response(json.dumps(payload), evidence=evidence_catalog(chunks))
    assert GroundingVerifier(chunks).verify(parsed).status == "failed"


def test_nonprotocol_bracketed_prose_is_not_a_citation():
    payload = {"answer": "Memory [Appendix A] [e1].", "citations": [{"evidence_id": "e1"}],
               "insufficient": False, "limitation": None}
    parsed = parse_grounded_response(json.dumps(payload), evidence=evidence_catalog(CHUNKS))
    assert "[Appendix A]" in parsed.answer


def test_repair_uses_quoted_json_objects_after_string_citation_failure(tmp_path):
    bad = json.dumps({"answer": "Memory [e1].", "citations": ["e1"],
                      "insufficient": False, "limitation": None})
    good = json.dumps({"answer": "Memory [e1].", "citations": [{"evidence_id": "e1"}],
                       "insufficient": False, "limitation": None})
    model = SequenceChatModel([bad, good])
    service = ChatService(FakeKnowledge(fragments=CHUNKS), model, FileChatRunStore(tmp_path), grounding_enabled=True)
    record = service.conversation_turn({"mode": "with_rag", "question": "memory?", "collection_id": "c1",
                                       "memory_text": "<task_memory>goal: learn</task_memory>"})
    repair = model.calls[-1][-1].content
    assert '"citations":[{"evidence_id":"e1"}]' in repair
    assert 'a citation is not an object' in repair
    assert record["answer"]["grounding"]["status"] == "verified"



def test_owned_runner_captures_provider_body_but_not_raw_headers_or_profile(tmp_path, monkeypatch):
    import harness.d25_live as live
    from knowledge_agent.domain.contracts import ChatMessage
    model = FakeChatModel(text="invalid JSON provider body")
    attempts = []
    live.capture_provider_attempts(model, attempts)
    result = model.chat([ChatMessage("user", "Owned corpus question")])
    assert result.text == "invalid JSON provider body"
    assert attempts[0]["response_text"] == result.text
    assert attempts[0]["messages_as_untrusted_data"] == [{"role": "user", "content": "Owned corpus question"}]
    assert set(attempts[0]) == {"attempt", "response_text", "response_sha256", "finish_reason", "usage", "messages_as_untrusted_data"}
    monkeypatch.setattr(live, "MODULE_DIR", tmp_path)
    reference = live.save_provider_diagnostics(tmp_path / "local-data" / "d25", "unique-run", attempts)
    artifact = json.loads((tmp_path / reference["path"]).read_text("utf-8"))
    assert artifact["attempts"][0]["response_text"] == result.text
    import hashlib
    assert reference["sha256"] == hashlib.sha256((tmp_path / reference["path"]).read_bytes()).hexdigest()



def test_dialogue_budget_is_separate_from_legacy_chat_service(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import knowledge_agent.__main__ as entry
    from knowledge_agent.config import load_settings
    import dataclasses
    settings = dataclasses.replace(load_settings({}), dialogue_db_path=str(tmp_path / "dialogues.db"),
                                   d25_chat_context_tokens=32768, d25_chat_max_output_tokens=8192)
    legacy = SimpleNamespace(knowledge=object(), max_context_tokens=8192, reserved_output_tokens=1024)
    dialogue_chat = SimpleNamespace()
    calls = []
    def build(config, knowledge):
        calls.append((config, knowledge))
        return dialogue_chat
    monkeypatch.setattr(entry, "build_chat_service", build)
    service, store = entry.build_conversation_service(settings, legacy)
    assert service.chat is dialogue_chat
    assert calls[0][0].chat_context_tokens == 32768
    assert calls[0][0].chat_max_output_tokens == 8192
    assert calls[0][1] is legacy.knowledge
    assert legacy.max_context_tokens == 8192 and legacy.reserved_output_tokens == 1024
    assert settings.chat_context_tokens == 8192 and settings.chat_max_output_tokens == 1024
    store.close()


def test_owned_diagnostics_preserve_safe_typed_empty_budget_failure():
    from harness.d25_live import capture_provider_attempts
    from knowledge_agent.domain.contracts import ChatMessage
    from knowledge_agent.domain.errors import ChatInvalidResponse
    model = FakeChatModel()
    def fail(messages, options=None):
        raise ChatInvalidResponse("The selected provider returned an empty answer.",
                                  details={"finish_reason": "length", "output_tokens": 8192, "visible_content_chars": 0})
    model.chat = fail
    attempts = []
    capture_provider_attempts(model, attempts)
    with pytest.raises(ChatInvalidResponse):
        model.chat([ChatMessage("user", "Owned question")])
    assert attempts[0]["error"]["details"]["output_tokens"] == 8192
    assert "response_text" not in attempts[0]



def test_optional_ui_context_request_cannot_raise_legacy_server_budget(tmp_path):
    service = ChatService(FakeKnowledge(fragments=CHUNKS), FakeChatModel(text="Memory"),
                          FileChatRunStore(tmp_path), max_context_tokens=8192, reserved_output_tokens=1024)
    record = service.chat({"mode": "with_rag", "question": "memory?", "collection_id": "c1",
                           "max_context_tokens": 32768})
    assert record["context"]["max_context_tokens"] == 8192
    assert record["context"]["reserved_output_tokens"] == 1024



@pytest.mark.parametrize("kind,expected", [(None, (8192, 1024)), ("local", (16384, 3072)), ("remote", (32768, 8192))])
def test_automatic_dialogue_budget_matches_provider_kind_and_keeps_legacy(kind, expected):
    from types import SimpleNamespace
    from knowledge_agent.config import load_settings, d25_generation_settings
    import dataclasses
    settings = load_settings({})
    if kind is not None:
        settings = dataclasses.replace(settings, test_profile=SimpleNamespace(kind=kind))
    actual = d25_generation_settings(settings)
    assert (actual.chat_context_tokens, actual.chat_max_output_tokens) == expected
    assert (settings.chat_context_tokens, settings.chat_max_output_tokens) == (8192, 1024)


def test_dialogue_budget_explicit_override_stays_explicit_for_network():
    from types import SimpleNamespace
    from knowledge_agent.config import load_settings, d25_generation_settings
    import dataclasses
    settings = dataclasses.replace(load_settings({"D25_CHAT_CONTEXT_TOKENS": "16384", "D25_CHAT_MAX_OUTPUT_TOKENS": "2048"}),
                                   test_profile=SimpleNamespace(kind="remote"))
    actual = d25_generation_settings(settings)
    assert (actual.chat_context_tokens, actual.chat_max_output_tokens) == (16384, 2048)



@pytest.mark.parametrize("kind,window,expected", [("remote", 8192, (8192, 2048)), ("local", 4096, (4096, 1024))])
def test_automatic_dialogue_reserve_respects_actual_advertised_window(kind, window, expected):
    from types import SimpleNamespace
    from knowledge_agent.config import load_settings, d25_generation_settings
    import dataclasses
    settings = dataclasses.replace(load_settings({}), test_profile=SimpleNamespace(kind=kind))
    actual = d25_generation_settings(settings, available_context=window)
    assert (actual.chat_context_tokens, actual.chat_max_output_tokens) == expected


def test_explicit_oversized_reserve_is_not_silently_reduced_to_model_window():
    from types import SimpleNamespace
    from knowledge_agent.config import load_settings, d25_generation_settings
    import dataclasses
    settings = dataclasses.replace(load_settings({"D25_CHAT_MAX_OUTPUT_TOKENS": "8192"}),
                                   test_profile=SimpleNamespace(kind="remote"))
    actual = d25_generation_settings(settings, available_context=8192)
    assert actual.chat_context_tokens == 8192 and actual.chat_max_output_tokens == 8192
    # Existing ContextBudget fails explicitly: no silent loss of mandatory memory.
    from knowledge_agent.chat.context import ContextBudget
    from knowledge_agent.domain.errors import ContextOverflow
    budget = ContextBudget(max_context_tokens=actual.chat_context_tokens, reserved_output_tokens=actual.chat_max_output_tokens)
    with pytest.raises(ContextOverflow):
        budget.plan(mandatory_texts=["question", "instructions"], candidates=[])
