"""Stage calls of the Day 13 task state machine.

``StageExecutor`` runs one provider call per stage action and normalizes the
reply into a structured result: a plan, a validation verdict, or a step text.
Every budget, parser and retry rule comes from ``task_prompts``, so the stage
contract has a single source of truth.

Error classification (FR-26): a provider exception never escapes the executor.
The caller passes whether the failed request was streamed (``stream=True`` plus
an ``on_chunk`` consumer) and the executor maps the exception to

* ``context_overflow`` — the provider rejected the request because the context
  is too long (duck-typed: ``status_code == 400`` and a "context" message);
* ``stream_error`` — the exception happened while streaming was requested;
* ``provider_error`` — any other provider failure.

The executor never writes to the database: on any error the task, its artifacts
and its version stay untouched, and only the caller records ``API_ERROR``.

Planning and validation are always non-streaming. A truncated plan, verdict or
step (``finish_reason`` in ``length``/``max_tokens``) and an invalid plan or
verdict get exactly one retry with the larger budget of the stage; a second
failure is reported as ``truncated`` or ``invalid_response``. Streaming text is
never persisted: on success only the completed step text is returned, and the
partial stream text of a failed step is discarded.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from stats import TurnStats, aggregate_stats
from task_prompts import (
    TASK_PLAN_MAX_TOKENS,
    TASK_PLAN_RETRY_MAX_TOKENS,
    TASK_PLAN_TEMPERATURE,
    TASK_STEP_MAX_TOKENS,
    TASK_STEP_RETRY_MAX_TOKENS,
    TASK_STEP_TEMPERATURE,
    TASK_VALIDATION_MAX_TOKENS,
    TASK_VALIDATION_RETRY_MAX_TOKENS,
    TASK_VALIDATION_TEMPERATURE,
    StepFactsViolationError,
    is_truncated,
    parse_plan_response,
    parse_validation_response,
    plan_retry_feedback,
    step_retry_feedback,
    validate_step_text,
)
from tasks import (
    API_ERROR_CONTEXT_OVERFLOW,
    API_ERROR_INVALID_RESPONSE,
    API_ERROR_PROVIDER,
    API_ERROR_STREAM,
    API_ERROR_TRUNCATED,
)


@dataclass
class StageExecutionResult:
    """Outcome of one stage action.

    ``text`` is the model reply (empty on error), ``plan``/``validation`` hold
    the parsed structure when the action produced one, ``attempts`` keeps the
    statistics of every executed provider call, and ``stats`` is their
    none-aware aggregate. ``error_kind`` is one of the ``tasks.API_ERROR_*``
    kinds and is set together with ``error``.
    """

    text: str = ""
    plan: dict | None = None
    validation: dict | None = None
    stats: TurnStats | None = None
    attempts: list = field(default_factory=list)
    error: str | None = None
    error_kind: str | None = None

    @property
    def ok(self) -> bool:
        """Whether the action produced a usable result."""
        return self.error is None

    @property
    def attempt_count(self) -> int:
        """The number of provider calls made for this action."""
        return len(self.attempts)


def classify_exception(exc, *, streaming=False) -> str:
    """Map a provider exception to an ``API_ERROR`` kind (FR-26).

    The exception type alone cannot tell whether the failed request was
    streamed, so the caller passes the ``streaming`` flag of the call it made.
    """
    if getattr(exc, "status_code", None) == 400 and "context" in str(exc).lower():
        return API_ERROR_CONTEXT_OVERFLOW
    return API_ERROR_STREAM if streaming else API_ERROR_PROVIDER


def _failed_call(exc, attempts, *, streaming) -> StageExecutionResult:
    """Build the error result of a failed provider call.

    The statistics of the calls that already succeeded are kept so the caller
    can still bill them; the failed call itself reported none.
    """
    return StageExecutionResult(
        attempts=list(attempts),
        stats=aggregate_stats(attempts),
        error=str(exc),
        error_kind=classify_exception(exc, streaming=streaming),
    )


class StageExecutor:
    """Run the task stage actions through a duck-typed completer.

    ``completer`` must expose
    ``complete(messages, *, max_tokens=None, temperature=None, stream=False,
    on_chunk=None) -> (text, TurnStats)``; ``ChatAgent`` satisfies it. A custom
    completer (a fake in tests, a different provider later) only has to keep
    that contract.
    """

    def __init__(self, completer):
        self._completer = completer

    def _call(self, messages, *, max_tokens, temperature, streaming, on_chunk):
        return self._completer.complete(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            stream=streaming,
            on_chunk=on_chunk,
        )

    def run_planning(self, messages, *, on_chunk=None) -> StageExecutionResult:
        """Produce and strictly parse the plan (planning is never streamed).

        ``on_chunk`` is accepted for a uniform stage API and deliberately not
        forwarded: the plan is JSON and must be received in one piece. A plan
        rejected as execution-incompatible is retried once with corrective
        feedback appended to the messages.
        """
        return self._run_structured(
            messages,
            parser=parse_plan_response,
            budget=TASK_PLAN_MAX_TOKENS,
            retry_budget=TASK_PLAN_RETRY_MAX_TOKENS,
            temperature=TASK_PLAN_TEMPERATURE,
            streaming=False,
            on_chunk=None,
            result_field="plan",
            retry_feedback=plan_retry_feedback,
        )

    def run_validation(self, messages, *, steps_count) -> StageExecutionResult:
        """Produce and strictly parse the validation verdict (never streamed).

        ``defect`` indexes are bounded to ``steps_count`` so a verdict cannot
        point at a step outside the plan.
        """

        def parser(text):
            return parse_validation_response(text, steps_count=steps_count)

        return self._run_structured(
            messages,
            parser=parser,
            budget=TASK_VALIDATION_MAX_TOKENS,
            retry_budget=TASK_VALIDATION_RETRY_MAX_TOKENS,
            temperature=TASK_VALIDATION_TEMPERATURE,
            streaming=False,
            on_chunk=None,
            result_field="validation",
        )

    def run_step(self, messages, *, stream=False, on_chunk=None, facts=None) -> StageExecutionResult:
        """Produce the text of one execution step.

        Streaming happens only when ``stream`` is true *and* an ``on_chunk``
        consumer exists, exactly like ``ChatAgent.complete``. A truncated step
        is retried once with the larger budget; a step that is still truncated
        is reported as ``truncated`` and its partial text is dropped. An empty
        step text is reported as ``invalid_response``, because the domain
        rejects an empty ``execution_result``.

        When ``facts`` is provided, the non-empty reply is validated against the
        attached storage snapshot. A reply that invents journal identifiers or
        contradicts the snapshot is retried once with corrective feedback and
        the larger budget; a second violation is reported as
        ``invalid_response``. The retried reply replaces the rejected one, so
        the partial stream of a failed attempt is never persisted. With
        ``facts=None`` the behavior is unchanged.
        """
        streaming = bool(stream) and on_chunk is not None
        attempts: list = []
        last_reason = None
        last_error = None
        retry_messages = messages

        for budget in (TASK_STEP_MAX_TOKENS, TASK_STEP_RETRY_MAX_TOKENS):
            try:
                text, stats = self._call(
                    retry_messages,
                    max_tokens=budget,
                    temperature=TASK_STEP_TEMPERATURE,
                    streaming=streaming,
                    on_chunk=on_chunk,
                )
            except Exception as exc:
                return _failed_call(exc, attempts, streaming=streaming)

            attempts.append(stats)
            last_reason = stats.finish_reason
            if is_truncated(last_reason):
                last_error = (
                    "model response was truncated "
                    f"(finish_reason={last_reason})"
                )
                retry_messages = messages
                continue
            if not str(text or "").strip():
                return StageExecutionResult(
                    attempts=list(attempts),
                    stats=aggregate_stats(attempts),
                    error="The step result is empty",
                    error_kind=API_ERROR_INVALID_RESPONSE,
                )
            if facts is not None:
                try:
                    validate_step_text(text, facts)
                except StepFactsViolationError as exc:
                    last_error = str(exc)
                    retry_messages = list(messages) + list(
                        step_retry_feedback(exc)
                    )
                    continue
            return StageExecutionResult(
                text=text,
                attempts=list(attempts),
                stats=aggregate_stats(attempts),
            )

        kind = (
            API_ERROR_TRUNCATED
            if is_truncated(last_reason)
            else API_ERROR_INVALID_RESPONSE
        )
        return StageExecutionResult(
            attempts=list(attempts),
            stats=aggregate_stats(attempts),
            error=last_error or "The step result could not be validated",
            error_kind=kind,
        )

    def _run_structured(
        self,
        messages,
        *,
        parser,
        budget,
        retry_budget,
        temperature,
        streaming,
        on_chunk,
        result_field,
        retry_feedback=None,
    ) -> StageExecutionResult:
        """Call, parse and, on truncation or invalid JSON, retry once.

        Both planning and validation use this path: the first failure triggers
        one retry with the larger budget, and only a second failure becomes an
        ``API_ERROR`` (``truncated`` when the last reply hit the output limit,
        ``invalid_response`` otherwise).

        ``retry_feedback`` is an optional callable that receives the parser
        error and returns extra messages for the retry. It is applied only after
        a parser ``ValueError`` (planning uses it to correct an
        execution-incompatible plan); a truncated reply is always retried with
        the original messages.
        """
        attempts: list = []
        last_reason = None
        last_error = None
        retry_messages = messages

        for attempt_number, budget_for_attempt in enumerate((budget, retry_budget)):
            try:
                text, stats = self._call(
                    retry_messages,
                    max_tokens=budget_for_attempt,
                    temperature=temperature,
                    streaming=streaming,
                    on_chunk=on_chunk,
                )
            except Exception as exc:
                return _failed_call(exc, attempts, streaming=streaming)

            attempts.append(stats)
            last_reason = stats.finish_reason
            if is_truncated(last_reason):
                last_error = (
                    "model response was truncated "
                    f"(finish_reason={last_reason})"
                )
                retry_messages = messages
                continue
            try:
                parsed = parser(text)
            except ValueError as exc:
                last_error = str(exc)
                retry_messages = messages
                if retry_feedback is not None and attempt_number == 0:
                    feedback = retry_feedback(exc)
                    if feedback:
                        retry_messages = list(messages) + list(feedback)
                continue
            return StageExecutionResult(
                text=text,
                stats=aggregate_stats(attempts),
                attempts=list(attempts),
                **{result_field: parsed},
            )

        kind = (
            API_ERROR_TRUNCATED
            if is_truncated(last_reason)
            else API_ERROR_INVALID_RESPONSE
        )
        return StageExecutionResult(
            attempts=list(attempts),
            stats=aggregate_stats(attempts),
            error=last_error or "The model response could not be parsed",
            error_kind=kind,
        )
