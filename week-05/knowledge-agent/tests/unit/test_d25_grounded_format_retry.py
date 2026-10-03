"""Correction defect 1: the bounded grounded-JSON format retry.

The selected local model answered a long plan request in prose. ChatService may
re-ask the model once for the same contract, but parsing and citation checks stay
strict, and the retry is never used to weaken or bypass them.
"""

from __future__ import annotations

import json

import pytest

from knowledge_agent.chat.chat_service import ChatService
from knowledge_agent.chat.prompts import GROUNDED_JSON_REPAIR_INSTRUCTION
from knowledge_agent.chat.run_store import FileChatRunStore
from knowledge_agent.domain.errors import ChatInvalidResponse
from tests.helpers import FakeChatModel, FakeKnowledge, fragment

PASSED = "a" * 64
CHUNK_TEXT = "Memory stores observations. Planning decomposes goals into steps."


class SequenceChatModel(FakeChatModel):
    """Returns queued texts in order and records every outgoing message list."""

    def __init__(self, texts: list[str]) -> None:
        super().__init__(text=texts[0])
        self.texts = list(texts)

    def chat(self, messages, options=None):
        index = min(len(self.calls), len(self.texts) - 1)
        self.text = self.texts[index]
        return super().chat(messages, options)


def _grounded(answer: str, citations: list[dict]) -> str:
    return json.dumps(
        {"answer": answer, "citations": citations, "insufficient": False, "limitation": None}
    )


def _service(tmp_path, texts: list[str], *, grounding_enabled: bool = True):
    knowledge = FakeKnowledge(fragments=[fragment(PASSED, text=CHUNK_TEXT, rank=1, score=0.7)])
    model = SequenceChatModel(texts)
    service = ChatService(
        knowledge,
        model,
        FileChatRunStore(tmp_path / "runs"),
        grounding_enabled=grounding_enabled,
    )
    return service, model


def test_prose_answer_is_retried_once_and_recovers_the_json_object(tmp_path):
    valid = _grounded(f"Plan [{PASSED}].", [{"chunk_id": PASSED, "quote": "Memory stores observations."}])
    service, model = _service(tmp_path, ["Here is a long prose plan with no JSON at all.", valid])

    record = service.chat({"mode": "with_rag", "question": "q", "collection_id": "c1"})

    assert len(model.calls) == 2
    assert GROUNDED_JSON_REPAIR_INSTRUCTION in [message.content for message in model.calls[1]]
    assert record["answer"]["grounding"]["status"] == "verified"


def test_retry_never_accepts_a_still_malformed_answer(tmp_path):
    # Both attempts are prose: the format error is preserved, not weakened.
    service, model = _service(tmp_path, ["prose one", "prose two"])
    with pytest.raises(ChatInvalidResponse) as excinfo:
        service.chat({"mode": "with_rag", "question": "q", "collection_id": "c1"})
    assert excinfo.value.details.get("format") == "grounded_json"
    assert len(model.calls) == 2


def test_non_grounded_path_is_not_retried(tmp_path):
    service, model = _service(tmp_path, ["plain prose answer"], grounding_enabled=False)
    service.chat({"mode": "with_rag", "question": "q", "collection_id": "c1"})
    assert len(model.calls) == 1


def test_retry_can_be_disabled(tmp_path):
    service, model = _service(tmp_path, ["prose only"])
    service.grounded_format_retries = 0
    with pytest.raises(ChatInvalidResponse):
        service.chat({"mode": "with_rag", "question": "q", "collection_id": "c1"})
    assert len(model.calls) == 1
