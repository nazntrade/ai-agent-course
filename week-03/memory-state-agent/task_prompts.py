"""Stage prompt contracts: system prompts, budgets, builders and parsers.

The module owns the provider contract of the three task stages (planning,
execution, validation): the Russian service prompts and synthetic action
messages of the context packet, the token budgets of the calls, and the strict
parsers of the model's JSON replies. It is pure stdlib and performs no I/O.

``format_specification_markdown`` and ``format_final_result_markdown`` are
re-exported from :mod:`tasks`, which owns the artifact text of the domain.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from tasks import (  # noqa: F401  (re-exported: the artifact text lives in the domain)
    DOMAIN_ACTIONS,
    ERROR_MESSAGE_MAX_LENGTH,
    REFUSAL_REASONS,
    STAGES,
    STATUS_COMPLETED,
    format_final_result_markdown,
    format_specification_markdown,
    normalize_verdict,
)

# Reasoning models spend part of the visible budget on hidden reasoning, so the
# base budget covers the common case and the retry budget is used once when the
# model truncates (FR-26).
TASK_PLAN_MAX_TOKENS = 2400
TASK_PLAN_RETRY_MAX_TOKENS = 4800
TASK_STEP_MAX_TOKENS = 2000
TASK_STEP_RETRY_MAX_TOKENS = 4000
TASK_VALIDATION_MAX_TOKENS = 1600
TASK_VALIDATION_RETRY_MAX_TOKENS = 3200

TASK_PLAN_TEMPERATURE = 0.2
TASK_STEP_TEMPERATURE = 0.2
TASK_VALIDATION_TEMPERATURE = 0.0

TRUNCATED_FINISH_REASONS = ("length", "max_tokens")

TASK_PLANNING_SYSTEM_PROMPT = (
    "Ты планируешь выполнение задачи. Верни СТРОГО один JSON-объект и ничего "
    "больше, без пояснений и без текста вокруг. Формат:\n"
    "{\"summary\": \"краткое описание плана\", "
    "\"acceptance_criteria\": [\"непустой критерий приёмки\", ...], "
    "\"steps\": [{\"index\": 1, \"title\": \"краткое название шага\", "
    "\"description\": \"что именно нужно сделать\"}, ...]}\n"
    "Правила: шагов не меньше одного; index идут подряд с 1 без пропусков; "
    "title и description каждого шага непустые; критериев приёмки не меньше "
    "одного; шаги проверяемы и не дублируют друг друга; не выдумывай данные, "
    "которых нет в цели и брифе. Текущая стадия planning — это этап "
    "формирования самого плана; шаги плана будут выполняться в стадии "
    "execution. Шаги плана — это действия и проверки самой задачи, выполнимые "
    "в стадии execution. Не включай в план шаги про сам workflow и переходы: "
    "planning, планирование, формирование, просмотр, принятие, утверждение или "
    "отклонение плана, запуск или завершение execution, validation. "
    "Перечисленные внешние переходы не являются шагами плана. Если шаг должен "
    "проверить предыдущую стадию, он проверяет её как уже состоявшийся факт по "
    "Event timeline или артефактам (например: «Проверить, что событие "
    "PLAN_ACCEPTED зафиксировано в журнале»), а не выполняет планирование или "
    "принятие плана заново. Задача не может одновременно находиться в planning "
    "и execution. Критерии приёмки формулируй только про результаты самой "
    "задачи и уже прошедшие факты; нельзя требовать в критериях validation, "
    "done или VALIDATION_PASSED. Возврат в planning допустим в плане только как "
    "проверка запрета через Transition guard или аудит отказов, а не как "
    "реальный переход."
)

TASK_EXECUTION_SYSTEM_PROMPT = (
    "Ты выполняешь один шаг плана и возвращаешь его результат. Отвечай по "
    "существу шага, без вступлений и извинений. Если в шаге перечислены "
    "замечания предыдущей проверки, устрани именно их. Не описывай другие шаги "
    "плана и не меняй сам план. Результат — это готовый текст шага, а не отчёт "
    "о процессе. Опирайся только на блок task_facts, который прикрепляет код: "
    "фактические stage, status, current_step, события журнала и отказы читаются "
    "из storage и передаются тебе как снимок. Не выдумывай и не называй "
    "идентификаторы и события, которых нет в этом снимке, не сочиняй Event "
    "timeline, не заявляй чужую стадию или статус и не отрицай уже записанные "
    "события. Не утверждай, что действие или переход был выполнен, отклонён, "
    "заблокирован или записан в аудит, если это не подтверждено блоком "
    "task_facts (refusals и их id). Read-only Transition guard описывай только "
    "условно: «Transition guard показывает, что Finish execution сейчас был бы "
    "отклонён с причиной …». Верни только содержимое шага."
)

TASK_VALIDATION_SYSTEM_PROMPT = (
    "Ты проверяешь результат выполнения задачи по критериям приёмки. Верни "
    "СТРОГО один JSON-объект и ничего больше. Формат:\n"
    "{\"passed\": true|false, \"defects\": [{\"step_index\": 1, "
    "\"description\": \"что именно не выполнено\"}], \"notes\": \"краткие "
    "пояснения\"}\n"
    "Правила: passed=true только если все критерии выполнены, и тогда defects "
    "пуст; при passed=false перечисли defects, каждый с номером шага из плана "
    "и конкретным описанием; не выдумывай дефекты, которых нет в результатах."
)

_SYNTHETIC_ACTION_PREFIX = "Действие: "


def is_truncated(finish_reason) -> bool:
    """Return ``True`` when the provider stopped because of the output limit."""
    return finish_reason in TRUNCATED_FINISH_REASONS


def strip_code_fences(text) -> str:
    """Return the text without a single surrounding Markdown code fence."""
    stripped = str(text or "").strip()
    if not stripped:
        return ""
    lines = stripped.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _step_view(step) -> dict:
    """Normalize a plan step mapping or a ``StepProgress`` into a view dict."""
    if isinstance(step, dict):
        index = step.get("index")
        title = step.get("title")
        description = step.get("description")
    else:
        index = getattr(step, "step_index", None)
        title = getattr(step, "title", None)
        description = getattr(step, "description", None)
    return {
        "index": index,
        "title": str(title or "").strip(),
        "description": str(description or "").strip(),
    }


def build_plan_messages(goal, task_brief=None) -> list:
    """Build the planning call: planning instruction plus the synthetic action."""
    lines = [_SYNTHETIC_ACTION_PREFIX + "run_planning", "", "Цель задачи:", str(goal or "").strip()]
    brief = str(task_brief or "").strip()
    if brief:
        lines.extend(["", "Бриф задачи:", brief])
    lines.extend(
        [
            "",
            "Составь план выполнения этой задачи.",
            "Напоминание: шаги плана выполняются в стадии execution и не должны "
            "описывать workflow-переходы (planning, принятие плана, "
            "validation); предыдущую стадию проверяй как уже состоявшийся факт "
            "по Event timeline или артефактам.",
            "Критерии приёмки — только о результатах самой задачи и уже "
            "прошедших фактах; validation, done и VALIDATION_PASSED не бывают "
            "ни шагами, ни критериями. Возврат в planning — только как проверка "
            "запрета через Transition guard или аудит отказов.",
        ]
    )
    return [
        {"role": "system", "content": TASK_PLANNING_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]


def build_step_messages(step, defects=None) -> list:
    """Build the execution call for one step, including its rework defects."""
    view = _step_view(step)
    lines = [_SYNTHETIC_ACTION_PREFIX + "run_step", ""]
    lines.append(f"Шаг {view['index']}: {view['title']}")
    if view["description"]:
        lines.extend(["", view["description"]])
    defect_lines = [str(defect).strip() for defect in (defects or []) if str(defect).strip()]
    if defect_lines:
        lines.extend(["", "Устрани замечания предыдущей проверки:"])
        lines.extend(f"- {defect}" for defect in defect_lines)
    lines.extend(
        [
            "",
            "Напоминание: не выдумывай и не называй идентификаторы и события "
            "журнала; опирайся только на блок task_facts, который прикрепляет "
            "код, не заявляй чужую стадию или статус и верни только содержимое "
            "шага. Не заявляй совершённую, отклонённую или записанную в аудит "
            "попытку перехода, если она не подтверждена task_facts; read-only "
            "Transition guard формулируй условно («был бы отклонён»).",
        ]
    )
    return [
        {"role": "system", "content": TASK_EXECUTION_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]


def build_validation_messages(
    goal, acceptance_criteria=None, steps=None, executions=None
) -> list:
    """Build the validation call: the criteria plus the strict-JSON instruction.

    The goal, the plan steps and the step results are already carried by the
    packet's artifact blocks (``task_snapshot``, ``plan``, ``execution_results``),
    so inlining them here duplicated most of the prompt. ``goal``, ``steps`` and
    ``executions`` stay in the signature because ``task_context`` passes them
    positionally; only the acceptance criteria are rendered, and the system
    prompt owns the strict JSON format the verdict must follow.
    """
    lines = [
        _SYNTHETIC_ACTION_PREFIX + "run_validation",
        "",
        "Проверь результат выполнения задачи по критериям приёмки и верни СТРОГО "
        "один JSON-вердикт в формате системного сообщения, без пояснений и без "
        "текста вокруг.",
    ]
    criteria = [
        str(criterion).strip()
        for criterion in (acceptance_criteria or [])
        if str(criterion).strip()
    ]
    if criteria:
        lines.extend(["", "Критерии приёмки:"])
        lines.extend(f"- {criterion}" for criterion in criteria)
    return [
        {"role": "system", "content": TASK_VALIDATION_SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(lines)},
    ]


# --- Step facts guard -------------------------------------------------------

# The execution reply is free text, but it must stay inside the storage facts
# the code attaches to the packet. The model never owns the journal: it may only
# describe the snapshot it was given. The check is deliberately conservative
# (regex heuristics, no morphology): it blocks fabricated identifiers, numeric
# references to unknown events, a wrong stage/status claim and the denial of a
# recorded fact, while leaving domain vocabulary and guard frames alone.
REASON_FABRICATED_REFERENCE = "fabricated_reference"
REASON_CONTRADICTS_FACTS = "contradicts_facts"

# A real event id is an integer; a token like ``EVT-EXEC-001`` is invented.
_FABRICATED_ID_RE = re.compile(r"\bEVT[-_]\w*", re.IGNORECASE)

# "event 42", "event id 42", "событие 42": a numeric event reference. The number
# must belong to the attached snapshot, otherwise the model invented it.
_EVENT_REFERENCE_RE = re.compile(
    r"(?:event[_ ]?id|event|событие)\D{0,8}(\d+)", re.IGNORECASE
)

# "stage: execution", "стадия = planning", "status: active", "статус: active".
_STAGE_STATUS_CLAIM_RE = re.compile(
    r"(stage|стадия|status|статус)\s*[:=]\s*(\w+)", re.IGNORECASE
)
_STAGE_CLAIM_KEYWORDS = ("stage", "стадия")

_SENTENCE_SPLIT_RE = re.compile(r"[.!?;\n]+")

# Negations that turn a named fact into a denial ("переход ещё не выполнялся").
_NEGATION_PHRASES = (
    "не выполнялся",
    "не выполнен",
    "не выполнена",
    "не выполнено",
    "не был выполнен",
    "не была выполнена",
    "не зафиксирован",
    "не зафиксирована",
    "не наступил",
    "не наступила",
    "не состоялся",
    "не состоялась",
    "отсутствует в журнале",
)

# A guard/audit frame legitimately quotes refusals and alternative transitions,
# so a contradiction there is not a factual claim about the stored state.
_STEP_FACT_FRAMES = (
    "transition guard",
    "аудит отказов",
    "аудит попыток",
    "refusal audit",
    "запрещ",
)

_STEP_FACT_DENIAL_PHRASES = ("переход", "transition")
_DONE_CLAIM = "задача выполнена"


@dataclass(frozen=True)
class StepEventFact:
    """One journal event as attached to the execution packet."""

    id: int | None = None
    event_type: str = ""
    created_at: str | None = None


@dataclass(frozen=True)
class StepRefusalFact:
    """One refusal-audit row as attached to the execution packet."""

    id: int | None = None
    action: str = ""
    reason: str = ""
    created_at: str | None = None


@dataclass(frozen=True)
class StepFacts:
    """Read-only snapshot of the stored task attached to an execution call.

    It lives in ``task_prompts`` (not ``task_context``) so the validator and the
    step text contract share one type without a ``task_context`` ↔
    ``task_prompts`` import cycle. ``events``/``refusals`` are tuples of
    :class:`StepEventFact`/:class:`StepRefusalFact`.
    """

    stage: str = ""
    status: str = ""
    current_step: str = ""
    current_step_index: int | None = None
    expected_action_type: str = ""
    expected_action_text: str = ""
    version: int | None = None
    events: tuple = ()
    refusals: tuple = ()


@dataclass(frozen=True)
class StepTextViolation:
    """One factual violation of a step result: its reason and text fragment."""

    reason: str
    fragment: str


class StepFactsViolationError(ValueError):
    """Raised when a step text contradicts the attached storage snapshot.

    It subclasses :class:`ValueError` so the stage executor handles it exactly
    like a parser failure: one corrective retry, then
    ``API_ERROR(invalid_response)``. ``violations`` carries the detailed list.
    """

    def __init__(self, violations):
        self.violations = tuple(violations)
        super().__init__(_format_step_violation_summary(self.violations))


def _format_step_violation_summary(violations) -> str:
    """Compact, fragment-only summary safe for the 200-char error message."""
    parts = [
        f"{violation.reason} ({violation.fragment.strip()[:40]})"
        for violation in violations[:3]
    ]
    remaining = len(violations) - len(parts)
    if remaining > 0:
        parts.append(f"… +{remaining} more")
    summary = (
        "The step result contradicts the stored task facts: " + ", ".join(parts)
    )
    return summary[:ERROR_MESSAGE_MAX_LENGTH]


def _known_event_ids(facts) -> set:
    """Return the integer ids of the snapshot's recorded events."""
    ids = set()
    for event in getattr(facts, "events", ()) or ():
        value = getattr(event, "id", None)
        if isinstance(value, int) and not isinstance(value, bool):
            ids.add(value)
    return ids


def _sentence_has_frame(sentence: str) -> bool:
    return any(frame in sentence for frame in _STEP_FACT_FRAMES)


def _names_recorded_fact(sentence: str, facts) -> bool:
    """Whether the sentence names a recorded event or the current transition."""
    for event in getattr(facts, "events", ()) or ():
        event_type = str(getattr(event, "event_type", "") or "").casefold()
        if event_type and event_type in sentence:
            return True
    stage = str(getattr(facts, "stage", "") or "").casefold()
    if stage and stage in sentence and any(
        phrase in sentence for phrase in _STEP_FACT_DENIAL_PHRASES
    ):
        return True
    return False


def _contradiction_violations(text: str, facts) -> list:
    """Return stage/status/done/negation violations of the whole text."""
    violations = []
    status = str(getattr(facts, "status", "") or "").casefold()
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        lowered = sentence.casefold()
        if not lowered.strip() or _sentence_has_frame(lowered):
            continue
        for match in _STAGE_STATUS_CLAIM_RE.finditer(sentence):
            keyword = match.group(1).casefold()
            value = match.group(2).casefold()
            if keyword in _STAGE_CLAIM_KEYWORDS:
                expected = str(getattr(facts, "stage", "") or "").casefold()
            else:
                expected = status
            if value != expected:
                violations.append(
                    StepTextViolation(
                        REASON_CONTRADICTS_FACTS, match.group(0).strip()
                    )
                )
        if status != STATUS_COMPLETED.casefold() and _DONE_CLAIM in lowered:
            violations.append(
                StepTextViolation(REASON_CONTRADICTS_FACTS, _DONE_CLAIM)
            )
        if any(phrase in lowered for phrase in _NEGATION_PHRASES) and (
            _names_recorded_fact(lowered, facts)
        ):
            violations.append(
                StepTextViolation(
                    REASON_CONTRADICTS_FACTS, sentence.strip()[:80]
                )
            )
    return violations


# --- Unconfirmed attempt guard ----------------------------------------------

# A step reply may describe a guard or a refusal audit, but it may not claim
# that an attempt happened, was rejected or was recorded unless the attached
# storage snapshot confirms it. The check is a conservative regex heuristic: it
# only fires on explicit Russian/English forms and leaves conditional
# sentences (the read-only ``Transition guard`` frame) alone.
REASON_UNCONFIRMED_ATTEMPT = "unconfirmed_attempt"

# "audit 42", "audit id 42", "audit #42", "аудит №42", "аудит: 42": a numeric
# reference to the refusal audit. The id must belong to the attached snapshot,
# otherwise the model invented it. Only an optional ``id``/``#``/``№``/``:`` may
# sit between the word and the number, so unrelated digits after "аудит от ..."
# (a date) are not read as an audit id.
_AUDIT_REFERENCE_RE = re.compile(
    r"(?:audit|аудит)\s*(?:id\s*|#\s*|№\s*|:\s*)?(\d+)", re.IGNORECASE
)

_TRANSITION_WORDS = ("переход", "transition")
_REFUSAL_WORDS = ("отказ", "refusal")
_INITIATED_WORD = "инициирован"
_ATTEMPT_WORD = "попытк"
_ATTEMPT_PARTICIPLES = (
    "предпринят",
    "выполнен",
    "выполнена",
    "выполнял",
    "завершён",
    "завершен",
    "отклонён",
    "отклонен",
    "заблокиров",
    "зафиксиров",
    "записан",
    "внесён",
    "внесен",
    # Performed participles that the performed-state list already matched as
    # substrings ("совершена" ⊃ "совершен"). Without them the attempt word
    # ("попытка перехода совершена") fell through to the performed branch and,
    # with no named stage, passed unconfirmed. Listed as stems so every gender
    # and aspect form is caught; negation is handled by ``_find_marker``.
    # The state stem is "состоял", not "состоя": the shorter form also matched
    # the noun "состояние", so truthful phrases like "попытка проверки
    # состояния" were misread as unconfirmed attempt claims.
    "соверш",
    "состоял",
    "осуществл",
    "произвед",
)
_TRANSITION_STATES = ("отклонён", "отклонен", "заблокирован", "инициирован")
_PERFORMED_STATES = (
    "выполнен",
    "выполнена",
    "выполнял",
    "состоялся",
    "совершён",
    "совершен",
    "осуществлён",
    "произведён",
    "завершён",
    "завершен",
)
_RECORDED_STATES = (
    "зафиксирован",
    "записан",
    "внесён",
    "внесен",
    "зарегистрирован",
    "recorded",
    "logged",
)

# Conditional frames: a prediction about what the guard would do is not a
# claim that anything happened. The words are matched on word boundaries:
# as substrings they also matched unrelated stems ("поможет" contains "может"),
# which silently turned a real claim into a prediction.
_CONDITIONAL_WORD_RE = re.compile(
    r"(?<!\w)(?:будет|будут|может|могут|показывает|покажет|если)(?!\w)",
    re.IGNORECASE,
)
_CONDITIONAL_BY_RE = re.compile(r"\bбы\b", re.IGNORECASE)

# Domain action/reason names used to tell "a refusal is named but absent from
# the snapshot" from "a refusal is described without specifics".
_KNOWN_REFUSAL_TOKENS = tuple(
    sorted(
        {str(action).casefold() for action in DOMAIN_ACTIONS}
        | {str(action).replace("_", " ").casefold() for action in DOMAIN_ACTIONS}
        | {str(reason).casefold() for reason in REFUSAL_REASONS}
    )
)


def _known_refusal_ids(facts) -> set:
    """Return the integer ids of the snapshot's refusal-audit rows."""
    ids = set()
    for refusal in getattr(facts, "refusals", ()) or ():
        value = getattr(refusal, "id", None)
        if isinstance(value, int) and not isinstance(value, bool):
            ids.add(value)
    return ids


def _refusal_snapshot_texts(facts) -> tuple:
    """Render the stored refusal actions/reasons as matchable text."""
    texts = []
    for refusal in getattr(facts, "refusals", ()) or ():
        action = str(getattr(refusal, "action", "") or "").strip().casefold()
        reason = str(getattr(refusal, "reason", "") or "").strip().casefold()
        if action:
            texts.append(action)
            texts.append(action.replace("_", " "))
        if reason:
            texts.append(reason)
    return tuple(text for text in texts if text)


def _is_conditional(sentence: str) -> bool:
    """Whether the sentence is a guard prediction rather than an assertion."""
    if _CONDITIONAL_BY_RE.search(sentence):
        return True
    return bool(_CONDITIONAL_WORD_RE.search(sentence))


# A negation particle within this many characters in front of a marker turns it
# into a denial ("переход НЕ выполнен", "ещё не инициирован"). The bounded
# window keeps a "не" from an earlier clause from disabling a later marker.
_NEGATION_BEFORE_RE = re.compile(r"\bне\b", re.IGNORECASE)
_NEGATION_WINDOW = 18


def _has_negation_before(sentence: str, index: int) -> bool:
    """Whether a standalone ``не`` sits shortly before ``sentence[index:]``.

    Russian negates a participle with a particle in front of it, so a marker
    preceded by ``не`` is a denial, not a claim.
    """
    start = max(0, index - _NEGATION_WINDOW)
    return bool(_NEGATION_BEFORE_RE.search(sentence[start:index]))


def _find_marker(sentence: str, markers) -> int | None:
    """Return the index of the first unnegated marker occurrence, else ``None``."""
    for marker in markers:
        start = 0
        while True:
            index = sentence.find(marker, start)
            if index < 0:
                break
            if not _has_negation_before(sentence, index):
                return index
            start = index + len(marker)
    return None


def _attempt_kind(sentence: str) -> str | None:
    """Classify one casefolded sentence: ``attempt``, ``performed`` or ``None``.

    The attempt forms win over the performed form, so a sentence that both
    initiates and finishes is still reported as an unconfirmed attempt. A
    negated marker ("не выполнен") is a denial, not a claim, and never counts.
    """
    has_transition = any(word in sentence for word in _TRANSITION_WORDS)
    if has_transition and _find_marker(sentence, (_INITIATED_WORD,)) is not None:
        return "attempt"
    if _ATTEMPT_WORD in sentence and _find_marker(
        sentence, _ATTEMPT_PARTICIPLES
    ) is not None:
        return "attempt"
    if has_transition and _find_marker(sentence, _TRANSITION_STATES) is not None:
        return "attempt"
    if any(word in sentence for word in _REFUSAL_WORDS) and _find_marker(
        sentence, _RECORDED_STATES
    ) is not None:
        return "attempt"
    if has_transition and _find_marker(sentence, _PERFORMED_STATES) is not None:
        return "performed"
    return None


def _named_stages(sentence: str) -> list:
    """Return every stage named by the sentence, in ``STAGES`` order."""
    return [stage for stage in STAGES if stage in sentence]


def _unconfirmed_attempt_violations(text: str, facts) -> list:
    """Return invented audit ids and unconfirmed attempt claims of the text.

    An attempt claim is confirmed only by the attached snapshot: a named
    ``refusal.action``/``refusal.reason`` that exists there, or at least one
    stored refusal when the sentence stays generic. With an empty audit every
    attempt claim is a violation. A performed-transition claim is a violation
    only when it names at least one stage and none of them matches
    ``facts.stage``. Conditional sentences are predictions, and a negated marker
    is a denial, so neither is ever treated as a claim.
    """
    violations = []
    if facts is None:
        return violations
    body = str(text or "")
    known_ids = _known_refusal_ids(facts)
    for match in _AUDIT_REFERENCE_RE.finditer(body):
        if int(match.group(1)) not in known_ids:
            violations.append(
                StepTextViolation(
                    REASON_FABRICATED_REFERENCE, match.group(0).strip()
                )
            )
    refusals = tuple(getattr(facts, "refusals", ()) or ())
    snapshot_texts = _refusal_snapshot_texts(facts)
    for sentence in _SENTENCE_SPLIT_RE.split(body):
        lowered = sentence.casefold()
        if not lowered.strip() or _is_conditional(lowered):
            continue
        kind = _attempt_kind(lowered)
        if kind is None:
            continue
        fragment = sentence.strip()[:80]
        if kind == "performed":
            stages = _named_stages(lowered)
            expected = str(getattr(facts, "stage", "") or "").casefold()
            # A performed claim is unconfirmed only when it names stages and
            # none of them is the stored one ("из planning в execution" is fine
            # once execution is the current stage).
            if stages and expected not in stages:
                violations.append(
                    StepTextViolation(REASON_UNCONFIRMED_ATTEMPT, fragment)
                )
            continue
        if not refusals:
            violations.append(
                StepTextViolation(REASON_UNCONFIRMED_ATTEMPT, fragment)
            )
        elif any(text in lowered for text in snapshot_texts):
            continue
        elif any(token in lowered for token in _KNOWN_REFUSAL_TOKENS):
            violations.append(
                StepTextViolation(REASON_UNCONFIRMED_ATTEMPT, fragment)
            )
    return violations


def validate_step_text(text, facts) -> None:
    """Reject a step result that contradicts the attached storage snapshot.

    The detector is conservative and only flags invented references
    (``EVT-...`` tokens, numeric event references to unknown ids), a claimed
    stage/status that differs from the snapshot, the "task is done" claim while
    the task is not completed, the denial of a recorded event or of the
    current stage, and an attempt/transition claim that the snapshot does not
    confirm. ``facts is None`` disables the check, which keeps every
    caller that has no snapshot on the previous behavior.
    """
    if facts is None:
        return
    body = str(text or "")
    violations = []
    for match in _FABRICATED_ID_RE.finditer(body):
        violations.append(
            StepTextViolation(REASON_FABRICATED_REFERENCE, match.group(0))
        )
    known_ids = _known_event_ids(facts)
    for match in _EVENT_REFERENCE_RE.finditer(body):
        if int(match.group(1)) not in known_ids:
            violations.append(
                StepTextViolation(
                    REASON_FABRICATED_REFERENCE, match.group(0).strip()
                )
            )
    violations.extend(_contradiction_violations(body, facts))
    violations.extend(_unconfirmed_attempt_violations(body, facts))
    if violations:
        raise StepFactsViolationError(violations)


def step_retry_feedback(exc) -> list:
    """Build the corrective user message for a rejected step result.

    It mirrors :func:`plan_retry_feedback`: one Russian user message that
    reminds the model the journal is owned by the code and asks it to rely only
    on the attached ``task_facts`` snapshot.
    """
    violations = tuple(getattr(exc, "violations", ()) or ())
    lines = [
        "Предыдущий результат шага отклонён: он содержит вымышленные или "
        "противоречащие фактам ссылки.",
    ]
    if violations:
        lines.append("Нарушения:")
        for violation in violations[:4]:
            fragment = violation.fragment.strip()[:80]
            lines.append(f"- {violation.reason}: {fragment}")
    lines.extend(
        [
            "",
            "Исправь результат шага:",
            "- не выдумывай и не называй идентификаторы и события журнала, "
            "которых нет в переданном снимке фактов;",
            "- опирайся только на факты из блока task_facts (stage, status, "
            "current_step, события журнала, аудит отказов);",
            "- не заявляй стадию или статус, отличные от снимка, и не отрицай "
            "уже записанные события;",
            "- не заявляй совершённую, отклонённую или записанную в аудит "
            "попытку перехода, если она не подтверждена task_facts "
            "(refusals/audit id); read-only Transition guard описывай только "
            "условно («был бы отклонён»);",
            "- верни только содержимое шага, без отчёта о процессе и "
            "переходах.",
        ]
    )
    return [{"role": "user", "content": "\n".join(lines)}]


# --- Execution-compatible plan-step guard ----------------------------------

REASON_REQUIRES_OTHER_STAGE = "requires_other_stage"
REASON_MISSING_ENTITY = "missing_entity"
REASON_NONEXISTENT_TRANSITION = "nonexistent_transition"

# Code-verb stems. An item that implements or documents the product itself may
# legitimately mention planning/validation/done words, so the future/prior-stage
# heuristics skip such items; the HARD phrases stay unconditional.
CODE_VERB_STEMS = (
    "реализ",
    "добав",
    "настро",
    "внедр",
    "исправ",
    "доработ",
    "рефактор",
    "переимен",
    "вынес",
    "перенес",
    "описа",
    "задокументир",
    "написа",
    "покрыт",
    "обработ",
    "поддерж",
)

# future_stage markers: the item acts on a stage that can only happen later. At
# planning, execution steps run before any validation/done transition and the
# acceptance criteria are checked at validation, which is still before done.
FUTURE_STAGE_ID_MARKERS = ("validation_passed", "validation_failed")
FUTURE_STAGE_STAGE_WORDS = ("validation", "валидац")
FUTURE_STAGE_STEMS = (
    "подтверд",
    "убед",
    "провер",
    "свер",
    "зафиксир",
    "дожд",
    "ожида",
    "выполн",
    "запуст",
    "провед",
    "пройд",
    "иницииру",
    "вызва",
    "заверш",
    "перевед",
)
FUTURE_STAGE_STATUS_TOKENS = (
    "validation",
    "валидац",
    "validation_passed",
    "validation_failed",
    "done",
    "completed",
)
FUTURE_STAGE_ADJACENCY_STEMS = (
    "выполн",
    "запуст",
    "провед",
    "пройд",
    "дожд",
    "ожида",
    "иницииру",
    "вызва",
    "заверш",
)
FUTURE_STAGE_ADJACENCY_TARGETS = ("execution", "validation", "валидац")
FUTURE_STAGE_STATUS_PHRASES = (
    "в done",
    "стадию done",
    "стадия done",
    "статус done",
    "в completed",
    "статус completed",
    "стадию completed",
)
FUTURE_STAGE_DONE_PHRASES = ("задача выполнена",)

# Event names that contain a future status token as a substring. ``completed``
# lives inside ``step_completed``, so a legitimate retrospective check of that
# already-recorded event would be misread as acting on the future ``done``
# status. Masking keeps the token check about the status itself.
STATUS_TOKEN_COLLISION_EVENTS = ("step_completed", "execution_finished")

_FUTURE_STAGE_ADJACENCY_RE = re.compile(
    "(?:"
    + "|".join(FUTURE_STAGE_ADJACENCY_STEMS)
    + r")\w*\s+(?:в|на|по)\s+(?:"
    + "|".join(FUTURE_STAGE_ADJACENCY_TARGETS)
    + ")"
)

# Phrases that always make a step invalid: the step performs a workflow
# transition that the FSM allows only in another stage (planning, plan
# review/accept, finish_execution, validation/done). Bare words like
# "planning"/"validation" are deliberately absent so a task that implements
# those stages itself is not blocked.
PLAN_STEP_HARD_MARKERS = (
    "выполнить validation",
    "выполнить и проверить validation",
    "запустить validation",
    "провести validation",
    "пройти validation",
    "выполнить валидацию",
    "запустить валидацию",
    "провести валидацию",
    "run_validation",
    "run validation",
    "finish_execution",
    "finish execution",
    "завершить execution",
    "завершить задачу",
    "проверить завершение задачи",
    "завершение задачи",
    "проверить done",
    "перевести задачу в done",
    "сформировать план",
    "формирование плана",
    "принять план",
    "принятие плана",
    "утвердить план",
    "утверждение плана",
    "отклонить план",
    "запуск планирования",
    "run_planning",
    "run planning",
    "accept_plan",
    "accept plan",
    "review plan",
    "reject plan",
)

# prior_stage markers: a previous stage may be named only as an already recorded
# fact, i.e. inside a retrospective frame.
PRIOR_STAGE_PHRASES = (
    "проверка planning",
    "проверка планирования",
    "проверить стадию planning",
    "проверить стадию планирования",
)
PRIOR_STAGE_ENTITY_TOKENS = ("plan_accepted", "plan_created")

# nonexistent_transition markers: ``planning`` has no user transition to return
# to once the task left it. Only the framework diagnostics words are a
# legitimate exception, so a bare "отказ/запрещ/отклон" must not act as a frame.
NONEXISTENT_TRANSITION_STEMS = (
    "вернут",
    "возврат",
    "откат",
    "откатить",
    "перевед",
    "перейти",
    "назад",
    "обратно",
    "→",
    "->",
)
NONEXISTENT_TRANSITION_PLANNING_WORDS = ("planning", "планировани")
NONEXISTENT_TRANSITION_FRAMES = (
    "transition guard",
    "аудит отказов",
    "аудит попыток",
    "диагностик",
    "refusal audit",
)

# Kept for import compatibility: the prior-stage phrases plus the validation
# retrospective phrasings that the future_stage class now owns.
PLAN_STEP_RETRO_MARKERS = PRIOR_STAGE_PHRASES + (
    "проверить validation",
    "проверить стадию validation",
    "проверить стадию валидации",
)

PLAN_STEP_RETRO_FRAMES = (
    "event timeline",
    "timeline",
    "журнал",
    "истори",
    "артефакт",
    "artifact",
)

# Phrases that refer to entities that do not exist in the product.
PLAN_STEP_MISSING_ENTITY_MARKERS = (
    "тестовая копия",
    "тестовой копии",
    "тестовую копию",
    "test copy",
    "копия состояния",
    "копии состояния",
    "копию состояния",
)


@dataclass(frozen=True)
class PlanViolation:
    """One plan item that cannot hold on its planning surface.

    ``location`` is ``"step"`` or ``"criterion"``; ``index`` is 1-based inside
    that surface. ``PlanStepViolation`` stays as an alias so existing call sites
    that construct or read ``.index/.title/.reason`` keep working.
    """

    index: int
    title: str
    reason: str
    location: str = "step"


PlanStepViolation = PlanViolation


class ExecutionIncompatiblePlanError(ValueError):
    """Raised when a structurally valid plan still has impossible items.

    It subclasses :class:`ValueError` so every caller that already treats a
    rejected planning reply as invalid keeps working; ``violations`` carries the
    detailed list for the corrective retry feedback.
    """

    def __init__(self, violations):
        self.violations = tuple(violations)
        super().__init__(_format_violation_summary(self.violations))


def _format_violation_summary(violations) -> str:
    """Compact, index-only summary safe for the 200-char error message."""
    parts = [
        f"{'criterion' if violation.location == 'criterion' else 'step'} "
        f"{violation.index} ({violation.reason})"
        for violation in violations[:4]
    ]
    remaining = len(violations) - len(parts)
    if remaining > 0:
        parts.append(f"… +{remaining} more")
    summary = (
        "Plan steps are not executable in the execution stage: "
        + ", ".join(parts)
    )
    return summary[:ERROR_MESSAGE_MAX_LENGTH]


def _has_code_verb(haystack: str) -> bool:
    return any(stem in haystack for stem in CODE_VERB_STEMS)


def _mask_status_token_collisions(haystack: str) -> str:
    """Blank out event names whose text embeds a future status token.

    The replacement keeps the original length so the surrounding text is
    unaffected; only the tokens themselves stop matching.
    """
    for event in STATUS_TOKEN_COLLISION_EVENTS:
        if event in haystack:
            haystack = haystack.replace(event, " " * len(event))
    return haystack


def _has_future_stage(haystack: str) -> bool:
    """Return True when the item acts on or requires a not-yet-happened stage."""
    code_verb = _has_code_verb(haystack)
    if not code_verb and any(
        marker in haystack for marker in FUTURE_STAGE_ID_MARKERS
    ):
        return True
    if (
        not code_verb
        and "стади" in haystack
        and any(word in haystack for word in FUTURE_STAGE_STAGE_WORDS)
    ):
        return True
    if not code_verb:
        masked = _mask_status_token_collisions(haystack)
        if any(stem in haystack for stem in FUTURE_STAGE_STEMS) and any(
            token in masked for token in FUTURE_STAGE_STATUS_TOKENS
        ):
            return True
    if not code_verb and _FUTURE_STAGE_ADJACENCY_RE.search(haystack):
        return True
    if not code_verb and (
        ("задач" in haystack and "заверш" in haystack)
        or any(phrase in haystack for phrase in FUTURE_STAGE_DONE_PHRASES)
    ):
        return True
    if not code_verb and any(
        phrase in haystack for phrase in FUTURE_STAGE_STATUS_PHRASES
    ):
        return True
    return False


def _has_nonexistent_transition(haystack: str) -> bool:
    if any(frame in haystack for frame in NONEXISTENT_TRANSITION_FRAMES):
        return False
    if not any(
        word in haystack for word in NONEXISTENT_TRANSITION_PLANNING_WORDS
    ):
        return False
    return any(stem in haystack for stem in NONEXISTENT_TRANSITION_STEMS)


def _has_prior_stage(haystack: str) -> bool:
    # The retrospective frame wins first: a fact-check of a previous stage is
    # legitimate even when the item also implements the product.
    if any(frame in haystack for frame in PLAN_STEP_RETRO_FRAMES):
        return False
    # Like the future/prior entity tokens, an item that implements or documents
    # the product itself may legitimately name a previous stage, so the
    # code-verb exception gates these phrases too.
    code_verb = _has_code_verb(haystack)
    if not code_verb and any(
        phrase in haystack for phrase in PRIOR_STAGE_PHRASES
    ):
        return True
    if any(token in haystack for token in PRIOR_STAGE_ENTITY_TOKENS):
        return not code_verb
    return False


def _item_violation_reason(haystack: str) -> str | None:
    """Classify one casefolded item text, or return ``None`` when acceptable.

    Precedence: HARD → future_stage → missing_entity → nonexistent_transition
    → prior_stage.
    """
    if any(marker in haystack for marker in PLAN_STEP_HARD_MARKERS):
        return REASON_REQUIRES_OTHER_STAGE
    if _has_future_stage(haystack):
        return REASON_REQUIRES_OTHER_STAGE
    if any(marker in haystack for marker in PLAN_STEP_MISSING_ENTITY_MARKERS):
        return REASON_MISSING_ENTITY
    if _has_nonexistent_transition(haystack):
        return REASON_NONEXISTENT_TRANSITION
    if _has_prior_stage(haystack):
        return REASON_REQUIRES_OTHER_STAGE
    return None


def _step_violation_reason(haystack: str) -> str | None:
    """Backward-compatible alias of :func:`_item_violation_reason`."""
    return _item_violation_reason(haystack)


def plan_violations(plan) -> tuple:
    """Return the incompatible steps and acceptance criteria, in plan order.

    Steps are checked against their execution surface and criteria against the
    validation surface that runs after ``EXECUTION_FINISHED``. The check is
    tolerant: a missing or malformed plan yields an empty tuple.
    """
    if not isinstance(plan, dict):
        return ()

    violations = []
    steps = plan.get("steps")
    if isinstance(steps, list):
        for position, step in enumerate(steps, start=1):
            if not isinstance(step, dict):
                continue
            title = str(step.get("title") or "").strip()
            description = str(step.get("description") or "").strip()
            reason = _item_violation_reason(f"{title}\n{description}".casefold())
            if reason is None:
                continue
            index = step.get("index")
            if not isinstance(index, int) or isinstance(index, bool):
                index = position
            violations.append(
                PlanViolation(
                    index=index, title=title, reason=reason, location="step"
                )
            )

    criteria = plan.get("acceptance_criteria")
    if isinstance(criteria, list):
        for position, criterion in enumerate(criteria, start=1):
            if not isinstance(criterion, str):
                continue
            title = criterion.strip()
            if not title:
                continue
            reason = _item_violation_reason(title.casefold())
            if reason is None:
                continue
            violations.append(
                PlanViolation(
                    index=position,
                    title=title,
                    reason=reason,
                    location="criterion",
                )
            )
    return tuple(violations)


def plan_step_violations(plan) -> tuple:
    """Return the execution-incompatible steps of a plan, in plan order.

    Steps-only compatibility view over :func:`plan_violations`.
    """
    return tuple(
        violation
        for violation in plan_violations(plan)
        if violation.location == "step"
    )


def plan_retry_feedback(exc) -> list:
    """Build the corrective user message for a rejected planning reply.

    A parser failure can be a structural JSON error or an
    :class:`ExecutionIncompatiblePlanError`. The opening line is chosen by the
    error: a well-formed plan with impossible steps or criteria keeps the
    execution-incompatible wording, while any other parser ``ValueError``
    (syntax or structure) is described as a strict-format violation instead of
    falsely claiming impossible items. The corrective instruction is shared.
    """
    violations = tuple(getattr(exc, "violations", ()) or ())
    if violations:
        lines = [
            "Предыдущий план отклонён: он содержит шаги или критерии приёмки, "
            "невыполнимые в стадии execution.",
        ]
        steps = [
            violation for violation in violations if violation.location == "step"
        ]
        criteria = [
            violation
            for violation in violations
            if violation.location == "criterion"
        ]
        if steps:
            lines.append("Недопустимые шаги:")
            for violation in steps:
                lines.append(
                    f"- шаг {violation.index}: {violation.title} "
                    f"({violation.reason})"
                )
        if criteria:
            lines.append("Недопустимые критерии приёмки:")
            for violation in criteria:
                lines.append(
                    f"- критерий {violation.index}: {violation.title} "
                    f"({violation.reason})"
                )
    else:
        lines = [
            "Предыдущий план отклонён: ответ не соответствует строгому формату "
            "плана.",
        ]
    lines.extend(
        [
            "",
            "Исправь план так, чтобы каждый шаг был действием или проверкой, "
            "допустимой в стадии execution:",
            "- убери шаги, которые выполняют planning, validation, "
            "finish_execution или переводят задачу в done;",
            "- если нужно проверить прошлую стадию, сформулируй шаг как "
            "ретроспективную проверку Event timeline или артефактов (например, "
            "PLAN_ACCEPTED, plan rev 1);",
            "- критерии приёмки формулируй только про результаты самой задачи и "
            "уже прошедшие факты; validation, done и VALIDATION_* не бывают ни "
            "шагами, ни критериями приёмки;",
            "- возврат в planning допустим только как проверка запрета через "
            "Transition guard или аудит отказов, а не как реальный переход;",
            "- запрещены шаги про «тестовую копию» и другие несуществующие "
            "сущности.",
            "Верни исправленный план в том же строгом JSON-формате.",
        ]
    )
    return [{"role": "user", "content": "\n".join(lines)}]


def parse_plan_response(text) -> dict:
    """Parse and strictly validate the planning reply.

    Rejects empty, fenced-invalid, truncated and structurally wrong JSON with a
    ``ValueError`` the caller reports as ``API_ERROR(invalid_response)``. A
    structurally valid plan whose steps cannot run in the execution stage raises
    :class:`ExecutionIncompatiblePlanError` (also a ``ValueError``).
    """
    stripped = strip_code_fences(text)
    if not stripped:
        raise ValueError("Plan response is empty")
    try:
        payload = json.loads(stripped)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid plan JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("Plan response must be a JSON object")

    steps = payload.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("Plan must contain at least one step")
    normalized_steps = []
    for position, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise ValueError(f"Plan step #{position} must be an object")
        index = step.get("index")
        if not isinstance(index, int) or isinstance(index, bool) or index != position:
            raise ValueError(
                "Plan step indexes must be continuous starting at 1; "
                f"expected {position}"
            )
        title = str(step.get("title") or "").strip()
        if not title:
            raise ValueError(f"Plan step {index} has an empty title")
        description = step.get("description")
        normalized_steps.append(
            {
                "index": index,
                "title": title,
                "description": description if isinstance(description, str) else str(description or ""),
            }
        )

    criteria = payload.get("acceptance_criteria")
    if not isinstance(criteria, list) or not criteria:
        raise ValueError("Plan must contain at least one acceptance criterion")
    cleaned_criteria = []
    for criterion in criteria:
        if not isinstance(criterion, str) or not criterion.strip():
            raise ValueError("Every acceptance criterion must be a non-empty string")
        cleaned_criteria.append(criterion.strip())

    summary = payload.get("summary")
    normalized = {
        "summary": summary if isinstance(summary, str) else str(summary or ""),
        "acceptance_criteria": cleaned_criteria,
        "steps": normalized_steps,
    }
    violations = plan_violations(normalized)
    if violations:
        raise ExecutionIncompatiblePlanError(violations)
    return normalized


def parse_validation_response(text, steps_count=None) -> dict:
    """Parse and strictly validate the validation reply.

    ``steps_count`` bounds the defect indexes to the plan. Invalid verdicts
    raise ``ValueError`` so the caller can retry once and then record
    ``API_ERROR(invalid_response)``.
    """
    stripped = strip_code_fences(text)
    if not stripped:
        raise ValueError("Validation response is empty")
    try:
        payload = json.loads(stripped)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid validation JSON: {exc}") from exc
    return normalize_verdict(payload, steps_count=steps_count)
