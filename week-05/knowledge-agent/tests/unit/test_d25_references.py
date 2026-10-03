"""D25 reference resolution: antecedent substitution and honest clarification."""

from __future__ import annotations

from knowledge_agent.chat.references import ReferenceResolver

RESOLVER = ReferenceResolver()


def _turn(question: str, answer: str) -> dict:
    return {"user_message": question, "answer": {"text": answer}}


def test_second_option_resolves_against_previous_answer():
    history = [_turn("Which approaches?", "1. Fine tuning\n2. Retrieval augmentation\n3. Prompting")]
    result = RESOLVER.resolve("а второй вариант?", history, {})
    assert result.original_query == "а второй вариант?"
    assert result.search_query == "Retrieval augmentation"
    assert result.used_history is True and result.ambiguous is False


def test_ambiguous_reference_asks_for_clarification_without_guessing():
    history = [_turn("What approaches?", "A short undivided prose answer.")]
    result = RESOLVER.resolve("а второй вариант?", history, {})
    assert result.ambiguous is True
    assert result.clarification_question
    assert result.search_query == result.original_query


def test_related_question_uses_current_goal():
    memory = {"goal": {"text": "изучить память агентов"}}
    result = RESOLVER.resolve("как это связано с планированием?", [], memory)
    assert result.used_memory is True
    assert "память агентов" in result.search_query
    assert result.original_query == "как это связано с планированием?"


def test_plain_question_is_its_own_search_query():
    result = RESOLVER.resolve("Что такое RAG?", [], {})
    assert result.original_query == "Что такое RAG?"
    assert "retrieval augmented generation" in result.search_query
    assert result.ambiguous is False
    assert result.used_history is False and result.used_memory is False


def test_pronoun_without_antecedent_asks_for_clarification():
    result = RESOLVER.resolve("Расскажи про это", [], {})
    assert result.ambiguous is True
    assert result.clarification_question


def test_continuation_keeps_goal_and_skips_refused_unrelated_topic():
    history=[{"user_message":"Какие компоненты нужны для RAG-системы?","status":"ok","answer":{"text":"Documentary answer"}},
             {"user_message":"Какая сегодня погода?","status":"refused","answer":{"text":"Unknown"}}]
    result=RESOLVER.resolve("Продолжи план внедрения",history,{"goal":{"text":"внедрение RAG"}})
    assert "компоненты" in result.search_query and "RAG" in result.search_query
    assert "погода" not in result.search_query
    assert "planning task decomposition" in result.search_query
    assert result.used_history and result.used_memory


def test_relational_question_keeps_both_user_topic_and_target():
    result=RESOLVER.resolve("как это связано с памятью?",[_turn("обучение агентов","Answer")],{"goal":{"text":"мой проект"}})
    assert "обучение" in result.search_query and "memory retrieval" in result.search_query
    assert "Answer" not in result.search_query


def test_two_plain_sentences_are_not_two_options():
    result=RESOLVER.resolve("Explain the second option",[_turn("Describe memory","Memory stores observations. Planning handles goals.")],{})
    assert result.ambiguous and result.clarification_question
