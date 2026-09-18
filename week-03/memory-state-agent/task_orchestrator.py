"""Use cases of the Day 13 task state machine.

``TaskOrchestrator`` is the only place where the FSM, the repository, the chat
context and the provider meet. Every public method is one use case: it loads
the task, asks the FSM whether the action is allowed (``tasks.can_apply`` is the
single source of truth), reads the chat context from ``ChatStore`` through a
``ChatAgent``, builds a context packet, optionally calls the provider through
``StageExecutor`` and commits the transition with ``TaskRepository``.

Rules enforced here:

* Actions that need no model (accept/reject the plan, finish execution, pause,
  resume, block, unblock, cancel) only run the FSM and commit the transition.
* On a provider error nothing changes: the repository records ``API_ERROR``
  and the task keeps its stage, step and version, with no artifact.
* The API-error payload carries the kind, the truncated message, the finish
  reason and the usage (model, tokens, cost, attempts) but never a secret or a
  full service prompt.
* ``retry`` is allowed only for an active task right after ``API_ERROR``; a
  paused or blocked task is refused before any ``RETRY`` event is written, and
  the ``RETRY`` event itself is written before the action is repeated.
* Chat settings are re-read on every call, so changing them mid-task affects
  the next call only, never the stored artifacts or task state.
* Task calls never touch ``messages``, ``turns``, memory, facts, the summary or
  the chat statistics, because ``ChatAgent.complete`` is standalone.

``TaskActionResult.status`` is ``success`` for a completed action, ``error``
for a provider/storage failure and ``noop`` when the FSM does not allow the
action in the current state (nothing was written in that case).
"""

from __future__ import annotations

from dataclasses import dataclass

from agent import ChatAgent
from invariants import (
    PHASE_ACTION,
    PHASE_COMMIT,
    find_action_conflict,
    find_transition_conflict,
    format_conflict_message,
)
from stats import TurnStats, aggregate_stats
from task_context import StageContextBuilder, latest_executions
from task_stage import StageExecutionResult, StageExecutor
from task_storage import (
    DEFAULT_WORKFLOW_NAME,
    TaskDuplicateEventError,
    TaskNotFoundError,
    TaskRepository,
    TaskVersionConflictError,
)
from tasks import (
    ACTION_ACCEPT_PLAN,
    ACTION_BLOCK,
    ACTION_CANCEL,
    ACTION_FINISH_EXECUTION,
    ACTION_PAUSE,
    ACTION_REJECT_PLAN,
    ACTION_RESUME,
    ACTION_RETRY,
    ACTION_RUN_PLANNING,
    ACTION_RUN_STEP,
    ACTION_RUN_VALIDATION,
    ACTION_UNBLOCK,
    API_ERROR_PROVIDER,
    ARTIFACT_TASK_BRIEF,
    EVENT_API_ERROR,
    EVENT_BLOCK,
    EVENT_CANCEL,
    EVENT_EXECUTION_FINISHED,
    EVENT_PAUSE,
    EVENT_PLAN_ACCEPTED,
    EVENT_PLAN_CREATED,
    EVENT_PLAN_REJECTED,
    EVENT_RESUME,
    EVENT_RETRY,
    EVENT_STEP_COMPLETED,
    EVENT_UNBLOCK,
    EVENT_VALIDATION_FAILED,
    EVENT_VALIDATION_PASSED,
    EXPECTED_CONFIRM_PLAN,
    EXPECTED_FINISH_EXECUTION,
    EXPECTED_RUN_PLANNING,
    EXPECTED_RUN_STEP,
    EXPECTED_RUN_VALIDATION,
    InvalidTransitionError,
    STAGE_DONE,
    STAGE_EXECUTION,
    STAGE_PLANNING,
    STAGE_VALIDATION,
    STATUS_CANCELLED,
    Task,
    TransitionPayloadError,
    apply_transition,
    can_apply,
    plan_steps,
    ui_actions,
    validate_error_message,
)

STATUS_SUCCESS = "success"
STATUS_ERROR = "error"
STATUS_NOOP = "noop"
# ``refused`` means a hard invariant forbade the action or the transition; no
# provider call happened and nothing was written.
STATUS_REFUSED = "refused"

# Action names used by this layer; the domain actions keep their FSM names.
ACTION_CREATE_TASK = "create_task"
ACTION_SELECT_TASK = "select_task"
ACTION_NONE = "none"

ERROR_INVALID_TRANSITION = "invalid_transition"
ERROR_INVALID_INPUT = "invalid_input"
ERROR_NOT_FOUND = "not_found"
ERROR_VERSION_CONFLICT = "version_conflict"
ERROR_DUPLICATE_EVENT = "duplicate_event"
ERROR_STORAGE = "storage_error"
ERROR_INVARIANT_CONFLICT = "invariant_conflict"

# Preview target of every expected action: the packet the next model call would
# use. ``confirm_plan`` previews planning (re-planning is its model call) and
# ``finish_execution`` previews validation, which is the next model call.
_PREVIEW_TARGETS = {
    EXPECTED_RUN_PLANNING: (STAGE_PLANNING, ACTION_RUN_PLANNING),
    EXPECTED_CONFIRM_PLAN: (STAGE_PLANNING, ACTION_RUN_PLANNING),
    EXPECTED_RUN_STEP: (STAGE_EXECUTION, ACTION_RUN_STEP),
    EXPECTED_FINISH_EXECUTION: (STAGE_VALIDATION, ACTION_RUN_VALIDATION),
    EXPECTED_RUN_VALIDATION: (STAGE_VALIDATION, ACTION_RUN_VALIDATION),
}

# Fallback preview for a paused or blocked task, where the expected action may
# have been overwritten by ``user_action``.
_STAGE_LLM_ACTIONS = {
    STAGE_PLANNING: ACTION_RUN_PLANNING,
    STAGE_EXECUTION: ACTION_RUN_STEP,
    STAGE_VALIDATION: ACTION_RUN_VALIDATION,
}


@dataclass
class TaskActionResult:
    """Result of one orchestrated use case.

    ``task`` is the task after the action (a rejected action returns the
    unchanged task), ``error_kind``/``error_message`` explain an ``error`` or
    ``noop``, and ``stage_result``/``packet`` expose what was sent and returned
    for a model action.
    """

    task: Task | None
    action: str
    status: str
    error_kind: str | None = None
    error_message: str | None = None
    stage_result: StageExecutionResult | None = None
    packet: object | None = None

    @property
    def ok(self) -> bool:
        """Whether the action completed successfully."""
        return self.status == STATUS_SUCCESS


class TaskOrchestrator:
    """Use cases of the task state machine over one chat store.

    ``store`` is a ``ChatStore`` (duck-typed) and ``repository`` defaults to a
    ``TaskRepository`` on the same database file. Provider access is either an
    injected ``completer`` (anything with ``ChatAgent.complete``'s signature),
    an injected ``executor`` (``StageExecutor``), or a ``client`` used to build
    a fresh ``ChatAgent`` per call. A fresh agent per call keeps the packet in
    sync with the stored chat configuration and guarantees that no state leaks
    between reruns.
    """

    def __init__(
        self,
        store,
        *,
        repository=None,
        client=None,
        completer=None,
        builder=None,
        executor=None,
        invariants=None,
    ):
        self._store = store
        self._repository = (
            repository if repository is not None else TaskRepository(store.db_path)
        )
        self._client = client
        self._completer = completer
        self._builder = builder if builder is not None else StageContextBuilder()
        self._executor = executor
        self._invariants = invariants

    @property
    def repository(self):
        """The task repository this orchestrator commits through."""
        return self._repository

    @property
    def invariants(self):
        """The structural-invariant repository, or ``None`` when not injected."""
        return self._invariants

    def applicable_invariants(self, task_id=None) -> list:
        """Return the active invariants that apply to ``task_id``.

        Duck-typed and defensive: without a repository (all pre-Day-14 call
        sites) the list is empty and every packet stays byte-identical.
        """
        if self._invariants is None:
            return []
        lister = getattr(self._invariants, "list_applicable", None)
        if lister is None:
            return []
        try:
            return list(lister(task_id))
        except Exception:
            return []

    # --- Task selection ----------------------------------------------------

    def create_task(self, chat_id, title, goal, task_brief=None) -> TaskActionResult:
        """Create the task through the repository and make it the active one."""
        try:
            task = self._repository.create_task(
                chat_id, title, goal, task_brief=task_brief
            )
        except TaskNotFoundError as exc:
            return self._error(None, ACTION_CREATE_TASK, ERROR_NOT_FOUND, exc)
        except ValueError as exc:
            return self._error(None, ACTION_CREATE_TASK, ERROR_INVALID_INPUT, exc)

        try:
            self._repository.set_active_task_id(chat_id, task.id)
        except TaskNotFoundError as exc:
            return self._error(task, ACTION_CREATE_TASK, ERROR_NOT_FOUND, exc)
        return TaskActionResult(
            task=self._repository.get_task(task.id),
            action=ACTION_CREATE_TASK,
            status=STATUS_SUCCESS,
        )

    def select_task(self, chat_id, task_id) -> TaskActionResult:
        """Remember the task selected for the chat."""
        try:
            self._repository.set_active_task_id(chat_id, task_id)
        except TaskNotFoundError as exc:
            return self._error(None, ACTION_SELECT_TASK, ERROR_NOT_FOUND, exc)
        return TaskActionResult(
            task=self._repository.get_task(task_id),
            action=ACTION_SELECT_TASK,
            status=STATUS_SUCCESS,
        )

    def current_task(self, chat_id) -> Task | None:
        """Return the selected task of the chat, or a safe fallback."""
        return self._repository.resolve_display_task(chat_id)

    def allowed_actions(self, task_id) -> tuple:
        """Domain actions of ``can_apply`` plus the UI-only actions (FR-09)."""
        task = self._repository.get_task(task_id)
        if task is None:
            return ()
        last_event = self._repository.latest_event_type(task_id)
        progress = self._repository.step_progress(task_id)
        domain = can_apply(task, last_event_type=last_event, progress=progress)
        return tuple(domain) + tuple(ui_actions(task))

    # --- Model actions -----------------------------------------------------

    def run_planning(self, task_id) -> TaskActionResult:
        """Produce the plan and commit ``PLAN_CREATED``."""
        task, blocked = self._guarded(task_id, ACTION_RUN_PLANNING)
        if blocked is not None:
            return blocked
        progress = self._repository.step_progress(task_id)
        packet, executor, config = self._prepare(task, task.stage, ACTION_RUN_PLANNING)
        stage_result = executor.run_planning(packet.messages)
        return self._settle(
            task,
            ACTION_RUN_PLANNING,
            EVENT_PLAN_CREATED,
            stage_result,
            packet,
            progress,
            config.model,
            {
                "plan": stage_result.plan,
                "task_brief": self._task_brief_text(task_id),
            },
        )

    def run_step(self, task_id, on_chunk=None) -> TaskActionResult:
        """Run (or re-run) the current step and commit ``STEP_COMPLETED``."""
        task, blocked = self._guarded(task_id, ACTION_RUN_STEP)
        if blocked is not None:
            return blocked
        progress = self._repository.step_progress(task_id)
        packet, executor, config = self._prepare(task, task.stage, ACTION_RUN_STEP)
        stage_result = executor.run_step(
            packet.messages,
            stream=bool(config.stream) and on_chunk is not None,
            on_chunk=on_chunk,
        )
        return self._settle(
            task,
            ACTION_RUN_STEP,
            EVENT_STEP_COMPLETED,
            stage_result,
            packet,
            progress,
            config.model,
            {
                "step_index": task.current_step_index,
                "text": stage_result.text,
            },
        )

    def run_validation(self, task_id) -> TaskActionResult:
        """Validate the finished execution and commit the verdict."""
        task, blocked = self._guarded(task_id, ACTION_RUN_VALIDATION)
        if blocked is not None:
            return blocked
        progress = self._repository.step_progress(task_id)
        packet, executor, config = self._prepare(task, task.stage, ACTION_RUN_VALIDATION)
        plan = self._repository.load_plan(task_id) or {}
        stage_result = executor.run_validation(
            packet.messages, steps_count=len(plan_steps(plan))
        )
        verdict = stage_result.validation or {}
        event_type = (
            EVENT_VALIDATION_PASSED if verdict.get("passed") else EVENT_VALIDATION_FAILED
        )
        artifacts = self._repository.list_artifacts(task_id)
        payload = {
            "plan": plan,
            "executions": latest_executions(artifacts),
        }
        payload.update(
            {
                key: verdict[key]
                for key in ("passed", "defects", "notes")
                if key in verdict
            }
        )
        return self._settle(
            task,
            ACTION_RUN_VALIDATION,
            event_type,
            stage_result,
            packet,
            progress,
            config.model,
            payload,
        )

    # --- Actions without a model call --------------------------------------

    def accept_plan(self, task_id) -> TaskActionResult:
        """Accept the plan and move to execution (no provider call)."""
        return self._simple(task_id, ACTION_ACCEPT_PLAN, EVENT_PLAN_ACCEPTED)

    def reject_plan(self, task_id) -> TaskActionResult:
        """Reject the plan and expect a new planning run (no provider call)."""
        return self._simple(task_id, ACTION_REJECT_PLAN, EVENT_PLAN_REJECTED)

    def finish_execution(self, task_id) -> TaskActionResult:
        """Move to validation once every step is completed (no provider call)."""
        return self._simple(task_id, ACTION_FINISH_EXECUTION, EVENT_EXECUTION_FINISHED)

    def pause(self, task_id, reason=None) -> TaskActionResult:
        """Pause the task between actions (no provider call)."""
        return self._simple(task_id, ACTION_PAUSE, EVENT_PAUSE, {"reason": reason})

    def resume(self, task_id) -> TaskActionResult:
        """Resume a paused task on the same stage and step (no provider call)."""
        return self._simple(task_id, ACTION_RESUME, EVENT_RESUME)

    def block(self, task_id, reason, expected_action) -> TaskActionResult:
        """Block the task, recording why and what the user has to provide."""
        return self._simple(
            task_id,
            ACTION_BLOCK,
            EVENT_BLOCK,
            {"reason": reason, "expected_action_text": expected_action},
        )

    def unblock(self, task_id) -> TaskActionResult:
        """Unblock a blocked task and recompute its expected action."""
        task = self._repository.get_task(task_id)
        if task is None:
            return self._not_found(task_id, ACTION_UNBLOCK)
        payload = {}
        previous = self._last_block_expected_action(task_id)
        if previous is not None:
            payload["expected_action_type"] = previous
        return self._simple(task_id, ACTION_UNBLOCK, EVENT_UNBLOCK, payload)

    def cancel(self, task_id, confirmed) -> TaskActionResult:
        """Cancel the task; without confirmation nothing is written."""
        if not confirmed:
            task = self._repository.get_task(task_id)
            if task is None:
                return self._not_found(task_id, ACTION_CANCEL)
            return self._noop(task, ACTION_CANCEL, "CANCEL requires confirmed=true")
        return self._simple(task_id, ACTION_CANCEL, EVENT_CANCEL, {"confirmed": True})

    # --- Retry -------------------------------------------------------------

    def retry(self, task_id, on_chunk=None) -> TaskActionResult:
        """Repeat the failed action, after recording the ``RETRY`` event.

        ``retry`` is allowed only for an active task whose last journal event is
        ``API_ERROR`` (the FSM rule). The check runs before anything is written,
        so a paused or blocked task gets a ``noop`` without a misleading
        ``RETRY`` event. The ``RETRY`` event itself never changes the task; the
        repeated action then changes it exactly as a first attempt would.
        """
        task = self._repository.get_task(task_id)
        if task is None:
            return self._not_found(task_id, ACTION_RETRY)
        progress = self._repository.step_progress(task_id)
        last_event = self._repository.latest_event_type(task_id)
        if ACTION_RETRY not in can_apply(
            task, last_event_type=last_event, progress=progress
        ):
            return self._noop(
                task,
                ACTION_RETRY,
                "RETRY is allowed only for an active task whose last event "
                "is API_ERROR",
            )
        target = self._retry_target(task)
        if target is None:
            return self._noop(
                task,
                ACTION_RETRY,
                f"Nothing to retry for stage={task.stage}, "
                f"expected_action={task.expected_action_type}",
            )
        conflict = find_action_conflict(
            self.applicable_invariants(task_id), target
        )
        if conflict is not None:
            # The repeated action is forbidden: no RETRY event is written.
            self._record_conflict(task, conflict, PHASE_ACTION)
            return self._refused(task, ACTION_RETRY, conflict)
        try:
            self._repository.append_event(task_id, EVENT_RETRY, payload={})
        except (TaskNotFoundError, InvalidTransitionError) as exc:
            return self._error(task, ACTION_RETRY, ERROR_INVALID_TRANSITION, exc)
        except Exception as exc:  # pragma: no cover - defensive storage guard
            return self._error(task, ACTION_RETRY, ERROR_STORAGE, exc)

        if target == ACTION_RUN_PLANNING:
            repeated = self.run_planning(task_id)
        elif target == ACTION_RUN_STEP:
            repeated = self.run_step(task_id, on_chunk=on_chunk)
        else:
            repeated = self.run_validation(task_id)
        return TaskActionResult(
            task=repeated.task,
            action=ACTION_RETRY,
            status=repeated.status,
            error_kind=repeated.error_kind,
            error_message=repeated.error_message,
            stage_result=repeated.stage_result,
            packet=repeated.packet,
        )

    # --- Preview -----------------------------------------------------------

    def preview_packet(self, task_id):
        """Build the packet of the next model call without any provider call.

        A missing or terminal task yields an empty packet, so the diagnostics
        panel can render something meaningful for every selection.
        """
        task = self._repository.get_task(task_id)
        if task is None:
            return self._builder.build_context_packet(
                stage="", action=ACTION_NONE, task=None
            )
        stage, action = self._preview_target(task)
        if action is None:
            return self._builder.build_context_packet(
                stage=task.stage, action=ACTION_NONE, task=task
            )
        packet, _, _ = self._prepare(task, stage, action)
        return packet

    # --- Internals ---------------------------------------------------------

    def _preview_target(self, task) -> tuple:
        if task.stage == STAGE_DONE or task.status == STATUS_CANCELLED:
            return task.stage, None
        target = _PREVIEW_TARGETS.get(task.expected_action_type)
        if target is not None:
            return target
        return task.stage, _STAGE_LLM_ACTIONS.get(task.stage)

    def _retry_target(self, task):
        target = _PREVIEW_TARGETS.get(task.expected_action_type)
        if target is None or target[0] != task.stage:
            return None
        return target[1]

    def _session(self, chat_id):
        """Return ``(context_agent, executor)`` for the chat.

        The context agent is always rebuilt, so the packet reflects the stored
        configuration at call time. With an injected completer or executor the
        agent is built without a client: it is a read-only view of the chat.
        """
        if self._completer is not None or self._executor is not None:
            agent = ChatAgent(None, store=self._store, chat_id=chat_id)
        else:
            agent = ChatAgent(self._client, store=self._store, chat_id=chat_id)
        executor = self._executor
        if executor is None:
            executor = StageExecutor(
                self._completer if self._completer is not None else agent
            )
        return agent, executor

    def _prepare(self, task, stage, action):
        """Build the packet, the executor and the config snapshot for one call."""
        agent, executor = self._session(task.chat_id)
        packet = self._build_packet(task, agent, stage, action)
        return packet, executor, agent.config

    def _build_packet(self, task, agent, stage, action):
        config = agent.config
        summary = self._load_summary(task.chat_id, agent.active_branch_id)
        return self._builder.build_context_packet(
            stage=stage,
            action=action,
            task=task,
            workflow=self._workflow(task),
            plan=self._repository.load_plan(task.id),
            artifacts=self._repository.list_artifacts(task.id),
            progress=self._repository.step_progress(task.id),
            system_prompt=config.system_prompt,
            invariants=config.invariants,
            structural_invariants=self.applicable_invariants(task.id),
            profile_block=agent.active_profile,
            working_items=agent.working_memory,
            long_term_items=agent.long_term_memory,
            history_messages=agent.history[1:],
            summary_content=summary.content if summary is not None else None,
            covered_messages_count=(
                summary.covered_messages_count if summary is not None else 0
            ),
            strategy=config.context_strategy,
            sliding_window_messages=config.sliding_window_messages,
            facts_window_messages=config.facts_window_messages,
            facts=self._load_facts(task.chat_id),
            model=config.model,
        )

    def _workflow(self, task):
        name = task.workflow_name or DEFAULT_WORKFLOW_NAME
        return self._repository.get_workflow(name)

    def _load_summary(self, chat_id, branch_id):
        loader = getattr(self._store, "load_summary", None)
        if loader is None:
            return None
        return loader(chat_id, branch_id=branch_id)

    def _load_facts(self, chat_id):
        loader = getattr(self._store, "load_facts", None)
        return loader(chat_id) if loader is not None else []

    def _task_brief_text(self, task_id) -> str:
        artifacts = self._repository.list_artifacts(task_id, kind=ARTIFACT_TASK_BRIEF)
        if not artifacts:
            return ""
        content = artifacts[-1].content or {}
        return str(content.get("text") or "")

    def _last_block_expected_action(self, task_id):
        previous = None
        for event in self._repository.list_events(task_id):
            if event.event_type == EVENT_BLOCK:
                previous = (event.payload or {}).get("previous_expected_action_type")
        return previous

    def _guarded(self, task_id, action) -> tuple:
        """Return ``(task, rejection)`` for a model action.

        Exactly one element is set: on success a rejection of ``None``, on a
        missing task or a disallowed action the task (when known) and the
        ``noop`` result to return.
        """
        task = self._repository.get_task(task_id)
        if task is None:
            return None, self._not_found(task_id, action)
        progress = self._repository.step_progress(task_id)
        last_event = self._repository.latest_event_type(task_id)
        if action not in can_apply(task, last_event_type=last_event, progress=progress):
            return task, self._noop(
                task,
                action,
                f"{action} is not allowed in stage={task.stage}, "
                f"status={task.status}, expected_action={task.expected_action_type}",
            )
        conflict = find_action_conflict(
            self.applicable_invariants(task.id), action
        )
        if conflict is not None:
            self._record_conflict(task, conflict, PHASE_ACTION)
            return task, self._refused(task, action, conflict)
        return task, None

    def _simple(self, task_id, action, event_type, payload=None) -> TaskActionResult:
        """Run a no-model use case: guard, transition, commit."""
        task, blocked = self._guarded(task_id, action)
        if blocked is not None:
            return blocked
        progress = self._repository.step_progress(task_id)
        return self._commit(task, action, event_type, dict(payload or {}), progress)

    def _settle(
        self,
        task,
        action,
        event_type,
        stage_result,
        packet,
        progress,
        model,
        payload,
    ) -> TaskActionResult:
        """Record a failed model call or commit its transition."""
        if stage_result.error is not None:
            error_payload = self._error_payload(stage_result, model)
            try:
                self._repository.append_event(
                    task.id, EVENT_API_ERROR, payload=error_payload
                )
            except TaskNotFoundError as exc:
                return self._error(task, action, ERROR_NOT_FOUND, exc)
            except Exception as exc:  # pragma: no cover - defensive storage guard
                return self._error(task, action, ERROR_STORAGE, exc)
            return TaskActionResult(
                task=self._repository.get_task(task.id),
                action=action,
                status=STATUS_ERROR,
                error_kind=stage_result.error_kind,
                error_message=stage_result.error,
                stage_result=stage_result,
                packet=packet,
            )

        merged = dict(payload or {})
        merged.update(self._usage_payload(stage_result, model))
        result = self._commit(task, action, event_type, merged, progress)
        result.stage_result = stage_result
        result.packet = packet
        return result

    def _commit(
        self, task, action, event_type, payload, progress
    ) -> TaskActionResult:
        conflict = find_transition_conflict(
            self.applicable_invariants(task.id),
            event_type,
            context={
                "action": action,
                "event_type": event_type,
                "payload": payload,
                "task": task,
            },
        )
        if conflict is not None:
            # A hard invariant refused the transition: nothing is written to
            # tasks, task_artifacts or task_events.
            self._record_conflict(task, conflict, PHASE_COMMIT)
            return self._refused(task, action, conflict)
        last_event = self._repository.latest_event_type(task.id)
        try:
            transition = apply_transition(
                task,
                event_type,
                payload=payload,
                progress=progress,
                last_event_type=last_event,
            )
        except (InvalidTransitionError, TransitionPayloadError) as exc:
            return self._error(task, action, ERROR_INVALID_TRANSITION, exc)

        try:
            new_task = self._repository.commit_transition(
                task.id, task.version, transition
            )
        except TaskVersionConflictError as exc:
            return self._error(task, action, ERROR_VERSION_CONFLICT, exc)
        except TaskDuplicateEventError as exc:
            return self._error(task, action, ERROR_DUPLICATE_EVENT, exc)
        except TaskNotFoundError as exc:
            return self._error(task, action, ERROR_NOT_FOUND, exc)
        return TaskActionResult(task=new_task, action=action, status=STATUS_SUCCESS)

    def _usage_payload(self, stage_result, model) -> dict:
        """Usage accounting shared by success and error payloads (FR-31).

        Only known values are recorded, so a missing provider count is never
        replaced by a fabricated zero. ``attempts`` counts the executed calls,
        including the retry of a truncated or invalid reply.
        """
        stats = stage_result.stats
        if stats is None:
            stats = aggregate_stats(stage_result.attempts)
        stats = stats if isinstance(stats, TurnStats) else TurnStats()

        usage = {"model": model}
        for key, attribute in (
            ("prompt_tokens", "request_tokens"),
            ("completion_tokens", "response_tokens"),
            ("total_tokens", "total_tokens"),
            ("prompt_cache_hit_tokens", "prompt_cache_hit_tokens"),
            ("prompt_cache_miss_tokens", "prompt_cache_miss_tokens"),
            ("cost_usd", "cost_usd"),
        ):
            value = getattr(stats, attribute, None)
            if value is not None:
                usage[key] = value

        payload = {"usage": usage, "model": model}
        if stats.finish_reason is not None:
            payload["finish_reason"] = stats.finish_reason
        # Recorded even for a call that failed before producing statistics, so
        # the payload shape stays uniform and the UI can always show "attempts".
        payload["attempts"] = len(stage_result.attempts)
        return payload

    def _error_payload(self, stage_result, model) -> dict:
        """Classified, truncated error payload; never contains prompts/secrets."""
        payload = self._usage_payload(stage_result, model)
        payload["kind"] = stage_result.error_kind or API_ERROR_PROVIDER
        payload["message"] = validate_error_message(stage_result.error or "")
        return payload

    def _noop(self, task, action, message) -> TaskActionResult:
        return TaskActionResult(
            task=task,
            action=action,
            status=STATUS_NOOP,
            error_kind=ERROR_INVALID_TRANSITION,
            error_message=message,
        )

    def _refused(self, task, action, conflict) -> TaskActionResult:
        """A hard invariant refused the action; nothing was done."""
        return TaskActionResult(
            task=task,
            action=action,
            status=STATUS_REFUSED,
            error_kind=ERROR_INVARIANT_CONFLICT,
            error_message=format_conflict_message(conflict),
        )

    def _record_conflict(self, task, conflict, phase) -> None:
        """Append the refusal to the invariant journal; never break the action."""
        recorder = (
            getattr(self._invariants, "record_conflict", None)
            if self._invariants is not None
            else None
        )
        if recorder is None:
            return
        try:
            recorder(
                chat_id=getattr(task, "chat_id", None),
                task_id=getattr(task, "id", None),
                invariant_id=conflict.invariant.id,
                code=conflict.invariant.code,
                phase=phase,
                trigger=conflict.trigger,
                action=conflict.action,
                event=conflict.event_type,
                alternative=conflict.invariant.alternative,
            )
        except Exception:
            pass

    def _not_found(self, task_id, action) -> TaskActionResult:
        return TaskActionResult(
            task=None,
            action=action,
            status=STATUS_ERROR,
            error_kind=ERROR_NOT_FOUND,
            error_message=f"task {task_id} not found",
        )

    def _error(self, task, action, kind, exc) -> TaskActionResult:
        return TaskActionResult(
            task=task,
            action=action,
            status=STATUS_ERROR,
            error_kind=kind,
            error_message=str(exc),
        )
