"""Resolve conversational references into a standalone search query.

The original user question is always preserved for generation; only the
retrieval query is rewritten. When an antecedent cannot be resolved
unambiguously the resolver asks a clarification question instead of guessing
(SPEC D25 6.6, 8.2).
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from ..domain.contracts import ReferenceResolution

_SECOND_OPTION = re.compile(r"второй вариант|второй способ|second option|second approach", re.IGNORECASE)
_RELATED = re.compile(r"как (?:это|это всё|они|он|она) связан\w*\s*(?:с|со)?\s*(.*)", re.IGNORECASE)
_THIS = re.compile(r"(?:^|\s)(?:это|этот|эта|он|она|они|it|that|this)(?:\s|$|\?|!|\.)", re.IGNORECASE)
_CONTINUE = re.compile(r"продолж[аи]\b|итогов\w*\s+план|continue|final\s+plan", re.IGNORECASE)
_LIST_ITEM = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s*(.+?)\s*$")


class ReferenceResolver:
    def resolve(
        self,
        question: str,
        history: Sequence[Mapping[str, Any]],
        memory: Mapping[str, Any] | None,
    ) -> ReferenceResolution:
        original = str(question or "").strip()
        memory = memory or {}
        if not original:
            return ReferenceResolution(original_query=original, search_query=original)

        last_assistant = _last_assistant_text(history)
        goal_text = _goal_text(memory)
        last_topic = _last_documentary_question(history)

        if _SECOND_OPTION.search(original):
            items = _list_items(last_assistant)
            if len(items) >= 2 and items[1].strip():
                return ReferenceResolution(
                    original_query=original,
                    search_query=items[1].strip(),
                    used_history=True,
                    used_memory=bool(goal_text),
                    resolved_antecedent=items[1].strip(),
                )
            return ReferenceResolution(
                original_query=original,
                search_query=original,
                used_history=bool(history),
                ambiguous=True,
                clarification_question="Which second option do you mean? Please name the alternative.",
                reason="ambiguous_reference",
            )

        related = _RELATED.search(original)
        if related:
            topic = (related.group(1) or "").strip(" ?.!")
            parts = [part for part in (topic, last_topic, goal_text) if part]
            if parts:
                return ReferenceResolution(
                    original_query=original,
                    search_query=_search_terms(" ".join(parts)),
                    used_history=bool(last_assistant),
                    used_memory=bool(goal_text),
                    resolved_antecedent=topic or goal_text,
                )
            if last_assistant:
                return ReferenceResolution(
                    original_query=original,
                    search_query=_trim(last_assistant),
                    used_history=True,
                    used_memory=False,
                )
            return ReferenceResolution(
                original_query=original,
                search_query=original,
                ambiguous=True,
                clarification_question="What should I relate this to?",
                reason="missing_antecedent",
            )

        if _CONTINUE.search(original) and (goal_text or last_topic):
            return ReferenceResolution(
                original_query=original,
                search_query=_search_terms(" ".join(part for part in (original, last_topic, goal_text) if part)),
                used_history=bool(last_topic), used_memory=bool(goal_text),
                resolved_antecedent=last_topic or goal_text,
            )

        if _THIS.search(original):
            antecedent = goal_text or _last_user_question(history)
            if antecedent:
                return ReferenceResolution(
                    original_query=original,
                    search_query=_search_terms(f"{antecedent} {original}"),
                    used_history=bool(history),
                    used_memory=bool(goal_text),
                    resolved_antecedent=antecedent,
                )
            if last_assistant:
                return ReferenceResolution(
                    original_query=original,
                    search_query=_trim(last_assistant),
                    used_history=True,
                )
            return ReferenceResolution(
                original_query=original,
                search_query=original,
                ambiguous=True,
                clarification_question="What are you referring to?",
                reason="missing_antecedent",
            )

        return ReferenceResolution(original_query=original, search_query=_search_terms(original))


def _last_assistant_text(history: Sequence[Mapping[str, Any]]) -> str:
    for turn in reversed(list(history)):
        answer = turn.get("answer") or {}
        text = str(answer.get("text") or "").strip()
        if text:
            return text
    return ""


def _last_user_question(history: Sequence[Mapping[str, Any]]) -> str:
    for turn in reversed(list(history)):
        text = str(turn.get("user_message") or turn.get("original_query") or "").strip()
        if text:
            return text
    return ""


def _goal_text(memory: Mapping[str, Any]) -> str:
    goal = memory.get("goal") or {}
    return str(goal.get("text") or "").strip()


def _list_items(text: str) -> list[str]:
    if not text:
        return []
    items = [_match.group(1) for line in text.splitlines() if (_match := _LIST_ITEM.match(line))]
    if len(items) >= 2:
        return items
    # Two prose sentences are not two named alternatives.
    return []


def _trim(text: str) -> str:
    return " ".join(str(text).split())[:400]


__all__ = ["ReferenceResolver"]


def _last_documentary_question(history: Sequence[Mapping[str, Any]]) -> str:
    """Find the antecedent topic, skipping acknowledgements/errors/refusals.

    A weather refusal or a condition update cannot become the subject of a
    continuation of the previous documentary plan. Only user questions are
    search inputs; generated assistant facts never enter the retrieval query.
    """
    for turn in reversed(list(history)):
        if turn.get("status") in ("refused", "error", "citation_failed", "clarification", "incomplete"):
            continue
        answer = turn.get("answer") or {}
        question = str(turn.get("user_message") or turn.get("original_query") or "").strip()
        if answer.get("task_state_summary") or answer.get("origin") == "confirmed_task_memory":
            continue
        if re.match(r"^(цель:|условие:|термин:|уточнение:|goal:|constraint:|измени условие)", question, re.IGNORECASE):
            continue
        if question:
            return question
    return ""


def _search_terms(text: str) -> str:
    """Small bilingual technical vocabulary for cross-language retrieval.

    These are query aliases, never reference answers, section identifiers or
    evaluation expectations. The original question is unchanged for generation.
    Every search still uses the selected index and its actual similarity scores.
    """
    aliases = []
    for pattern, terms in (
        (r"обучени|обуча|учить", "agent capability acquisition training learning"),
        (r"памят", "memory retrieval"),
        (r"планир|план(?:\s|$)", "planning task decomposition"),
        (r"архитектур|компонент|модул", "agent architecture modules"),
        (r"\brag\b", "retrieval augmented generation memory"),
    ):
        if re.search(pattern, text, re.IGNORECASE):
            aliases.append(terms)
    original = " ".join(str(text).split())[:400]
    return original + (" " + " ".join(aliases) if aliases else "")
