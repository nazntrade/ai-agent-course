"""D22 eval set schema: exactly 10 questions, one not answerable (D22-10)."""

from __future__ import annotations

import json
from pathlib import Path

QUESTIONS = Path(__file__).resolve().parents[2] / "eval" / "d22" / "questions.json"


def test_eval_set_has_exactly_ten_questions_one_unanswerable():
    data = json.loads(QUESTIONS.read_text(encoding="utf-8"))
    assert data["schema_version"] == "rag-eval-questions-v1"
    assert data["corpus"]["label"] == "agents-survey.pdf"
    questions = data["questions"]
    assert len(questions) == 10
    assert sum(1 for question in questions if question.get("answerable") is True) == 9
    assert [question["question_id"] for question in questions] == [
        f"D22-Q{index:02d}" for index in range(1, 11)
    ]
    unanswerable = [question for question in questions if question.get("answerable") is False]
    assert len(unanswerable) == 1
    assert unanswerable[0]["expected_behavior"] == "state_unknown_without_fabrication"


def test_eval_set_is_outside_the_indexed_corpus_directory():
    # The questions live under eval/, never under local-data/ (the user corpus).
    assert "local-data" not in QUESTIONS.parts
    assert "eval" in QUESTIONS.parts
