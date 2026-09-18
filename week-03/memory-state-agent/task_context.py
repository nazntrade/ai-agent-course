"""Per-stage context packets for the Day 13 task state machine.

A packet is the exact message list sent for one task action plus a
diagnostics view of its blocks: the block name, its role, the text and a
pre-flight estimate of its tokens and input cost. The module is pure: it
performs no I/O, opens no database and issues no provider request, so the same
builder serves both the real stage calls and the API-free preview in
``Diagnostics / Task``.

The block order is fixed by the specification (FR-27):

1. base system prompt;
2. invariants;
3. the active user profile;
4. workflow and current-stage instructions;
5. the task snapshot;
6. the required artifacts of previous stages;
7. relevant working/long-term memory items;
8. the part of the history selected by the current context strategy;
9. the current message/action.

Blocks 1-7 and 9 are single messages; block 8 is the strategy-selected slice
of the chat line, so it may contain several messages (including the summary or
facts block the strategy itself inserts). Empty blocks are skipped, exactly as
``memory.insert_system_blocks`` does. The full history is never sent on its
own: block 8 reuses ``context``'s payload builders, so the chat's context
strategy decides how much history a task call sees.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from context import build_facts_payload, build_payload, build_sliding_payload
from invariants import format_structural_invariants_block
from memory import (
    LONG_TERM_MEMORY_BLOCK_TITLE,
    WORKING_MEMORY_BLOCK_TITLE,
    format_invariants_block,
    format_memory_block,
)
from models import DEFAULT_MODEL
from pricing import estimate_tokens_cost
from profile import format_profile_block
from strategies import (
    DEFAULT_FACTS_WINDOW,
    DEFAULT_SLIDING_WINDOW,
    STRATEGY_FACTS,
    STRATEGY_SLIDING,
    STRATEGY_SUMMARY,
    normalize_strategy,
)
from task_prompts import (
    TASK_EXECUTION_SYSTEM_PROMPT,
    TASK_PLANNING_SYSTEM_PROMPT,
    TASK_VALIDATION_SYSTEM_PROMPT,
    build_plan_messages,
    build_step_messages,
    build_validation_messages,
)
from tasks import (
    ACTION_RUN_PLANNING,
    ACTION_RUN_STEP,
    ACTION_RUN_VALIDATION,
    ARTIFACT_EXECUTION_RESULT,
    ARTIFACT_PLAN,
    ARTIFACT_SPECIFICATION,
    ARTIFACT_TASK_BRIEF,
    ARTIFACT_VALIDATION_RESULT,
    STAGE_EXECUTION,
    STAGE_PLANNING,
    STAGE_VALIDATION,
    format_defects_block,
    format_snapshot_block,
    plan_steps,
)
from tokens import estimate_tokens

# Block names, in the packet order of FR-27.
BLOCK_SYSTEM_PROMPT = "system_prompt"
BLOCK_INVARIANTS = "invariants"
BLOCK_STRUCTURAL_INVARIANTS = "structural_invariants"
BLOCK_PROFILE = "profile"
BLOCK_WORKFLOW = "workflow"
BLOCK_TASK_SNAPSHOT = "task_snapshot"
BLOCK_TASK_BRIEF = "task_brief"
BLOCK_SPECIFICATION = "specification"
BLOCK_PLAN = "plan"
BLOCK_ACCEPTANCE_CRITERIA = "acceptance_criteria"
BLOCK_EXECUTION_RESULTS = "execution_results"
BLOCK_KNOWN_LIMITATIONS = "known_limitations"
BLOCK_DEFECTS = "defects"
BLOCK_WORKING_MEMORY = "working_memory"
BLOCK_LONG_TERM_MEMORY = "long_term_memory"
BLOCK_HISTORY = "history"
BLOCK_ACTION = "action"

BLOCK_ORDER = (
    BLOCK_SYSTEM_PROMPT,
    BLOCK_INVARIANTS,
    BLOCK_STRUCTURAL_INVARIANTS,
    BLOCK_PROFILE,
    BLOCK_WORKFLOW,
    BLOCK_TASK_SNAPSHOT,
    BLOCK_TASK_BRIEF,
    BLOCK_SPECIFICATION,
    BLOCK_PLAN,
    BLOCK_ACCEPTANCE_CRITERIA,
    BLOCK_EXECUTION_RESULTS,
    BLOCK_KNOWN_LIMITATIONS,
    BLOCK_DEFECTS,
    BLOCK_WORKING_MEMORY,
    BLOCK_LONG_TERM_MEMORY,
    BLOCK_HISTORY,
    BLOCK_ACTION,
)

# The stage system prompt owns the JSON contract of a stage; the workflow
# instruction comes from the repository and adds the profile-specific words.
_STAGE_INSTRUCTIONS = {
    STAGE_PLANNING: TASK_PLANNING_SYSTEM_PROMPT,
    STAGE_EXECUTION: TASK_EXECUTION_SYSTEM_PROMPT,
    STAGE_VALIDATION: TASK_VALIDATION_SYSTEM_PROMPT,
}


@dataclass(frozen=True)
class ContextBlock:
    """One packet block as shown in ``Diagnostics / Task``.

    ``tokens`` and ``cost_usd`` are estimates: ``tokens`` uses
    ``tokens.estimate_tokens`` and ``cost_usd`` prices the input at the
    cache-miss rate through ``pricing.estimate_tokens_cost``. The value is a
    preview, never a billed amount; the "estimate, not billing" caption is a UI
    concern.
    """

    name: str
    role: str
    content: str
    tokens: int
    cost_usd: float | None = None


@dataclass
class ContextPacket:
    """The messages of one task action plus the diagnostics view of its blocks."""

    blocks: list = field(default_factory=list)
    messages: list = field(default_factory=list)
    total_tokens: int = 0
    total_cost_usd: float | None = None
    stage: str = ""
    action: str = ""

    def block(self, name):
        """Return the block with ``name``, or ``None`` when it is absent."""
        for item in self.blocks:
            if item.name == name:
                return item
        return None


def _content_of(item) -> dict:
    """Return a mapping's content, accepting plain dicts and artifact objects."""
    if isinstance(item, dict):
        return item
    content = getattr(item, "content", None)
    return content if isinstance(content, dict) else {}


def _artifact_order(artifact) -> tuple:
    """Order artifacts by (revision, id); drafts without either sort first."""
    revision = getattr(artifact, "revision", None)
    artifact_id = getattr(artifact, "id", None)
    return (
        revision if isinstance(revision, int) else 0,
        artifact_id if isinstance(artifact_id, int) else 0,
    )


def _latest_artifact(artifacts, kind):
    """Return the newest artifact of one kind, or ``None``."""
    latest = None
    for artifact in artifacts or ():
        if getattr(artifact, "kind", None) != kind:
            continue
        if latest is None or _artifact_order(artifact) > _artifact_order(latest):
            latest = artifact
    return latest


def _latest_executions(artifacts) -> dict:
    """Return the newest ``execution_result`` per step index."""
    latest: dict = {}
    for artifact in artifacts or ():
        if getattr(artifact, "kind", None) != ARTIFACT_EXECUTION_RESULT:
            continue
        index = _content_of(artifact).get("step_index")
        if not isinstance(index, int) or isinstance(index, bool):
            continue
        current = latest.get(index)
        if current is None or _artifact_order(artifact) > _artifact_order(current):
            latest[index] = artifact
    return latest


def _join(*parts) -> str:
    return "\n\n".join(str(part).strip() for part in parts if str(part or "").strip())


def latest_executions(artifacts) -> list:
    """Return the newest ``execution_result`` of every step, ordered by step."""
    newest = _latest_executions(artifacts)
    return [newest[index] for index in sorted(newest)]


def _defect_list(artifact) -> list:
    """Return the defects of a verdict as ``(step_index, description)`` pairs."""
    result = []
    for defect in _content_of(artifact).get("defects") or []:
        if not isinstance(defect, dict):
            continue
        description = str(defect.get("description") or "").strip()
        if description:
            result.append((defect.get("step_index"), description))
    return result


def format_plan_block(plan, *, include_criteria=True) -> str:
    """Render a plan artifact as a readable block.

    ``include_criteria=False`` is used by the validation packet, which carries
    the acceptance criteria in their own block.
    """
    if not isinstance(plan, dict):
        return ""
    lines = []
    summary = str(plan.get("summary") or "").strip()
    if summary:
        lines.extend(["План (сводка):", summary, ""])
    steps = plan_steps(plan)
    if steps:
        lines.append("Шаги плана:")
        for step in steps:
            lines.append(f"{step['index']}. {step['title']}")
            if step["description"]:
                lines.append(f"   {step['description']}")
        lines.append("")
    if include_criteria:
        criteria = [
            str(criterion).strip()
            for criterion in plan.get("acceptance_criteria") or []
            if str(criterion).strip()
        ]
        if criteria:
            lines.append("Критерии приёмки:")
            lines.extend(f"- {criterion}" for criterion in criteria)
    return "\n".join(lines).strip()


def format_acceptance_criteria_block(plan) -> str:
    """Render the acceptance criteria of a plan as their own block."""
    if not isinstance(plan, dict):
        return ""
    criteria = [
        str(criterion).strip()
        for criterion in plan.get("acceptance_criteria") or []
        if str(criterion).strip()
    ]
    if not criteria:
        return ""
    return "\n".join(["Критерии приёмки:"] + [f"- {criterion}" for criterion in criteria])


def format_execution_results_block(executions) -> str:
    """Render the latest result of every step as one block."""
    ordered = sorted(
        (
            (index, artifact)
            for index, artifact in (executions or {}).items()
            if isinstance(index, int) and not isinstance(index, bool)
        ),
        key=lambda item: item[0],
    )
    rendered = []
    for index, artifact in ordered:
        content = _content_of(artifact)
        text = str(content.get("text") or "").strip()
        if not text:
            continue
        step_round = content.get("round")
        step_round = step_round if step_round is not None else "?"
        rendered.append(f"[шаг {index}, ревизия {step_round}]\n{text}")
    if not rendered:
        return ""
    return "\n\n".join(["Результаты выполнения шагов:"] + rendered)


def format_limitations_block(artifact) -> str:
    """Render the known limitations taken from a previous validation verdict.

    A validation result records what was already reported about the task, so
    the next validation round starts from the same knowledge instead of
    re-deriving it. An empty verdict yields an empty block.
    """
    if artifact is None:
        return ""
    content = _content_of(artifact)
    notes = str(content.get("notes") or "").strip()
    defects = _defect_list(artifact)
    if not notes and not defects:
        return ""
    lines = ["Известные ограничения (по предыдущей проверке):"]
    if notes:
        lines.append(notes)
    for step_index, description in defects:
        lines.append(f"- Шаг {step_index}: {description}")
    return "\n".join(lines)


def format_task_brief_block(artifact) -> str:
    """Render the latest ``task_brief`` artifact, or an empty string."""
    if artifact is None:
        return ""
    text = str(_content_of(artifact).get("text") or "").strip()
    return f"Бриф задачи:\n{text}" if text else ""


def _profile_text(profile_block) -> str:
    """Accept either a pre-formatted block or a profile object."""
    if profile_block is None:
        return ""
    if isinstance(profile_block, str):
        return profile_block
    return format_profile_block(profile_block) or ""


def _stage_instruction(stage) -> str:
    return _STAGE_INSTRUCTIONS.get(stage, "")


def _workflow_instruction(workflow, stage, action) -> str:
    """Return the workflow instruction of the stage, falling back to the action."""
    instructions = getattr(workflow, "instructions", None)
    if not isinstance(instructions, dict):
        return ""
    for key in (stage, action):
        text = instructions.get(key)
        if text:
            return str(text)
    return ""


def select_history(
    history_messages,
    *,
    strategy=None,
    summary_content=None,
    covered_messages_count=0,
    sliding_window_messages=DEFAULT_SLIDING_WINDOW,
    facts_window_messages=DEFAULT_FACTS_WINDOW,
    facts=None,
) -> list:
    """Return the history slice the current context strategy would send.

    The strategy payload builders of ``context`` are reused with an empty
    system prompt and an empty current message, so this function never
    duplicates their logic. The wrapping system prompt and the synthetic user
    message are dropped, leaving exactly the history part: the strategy's own
    summary or facts block plus the messages it keeps. ``full`` and
    ``branching`` keep the whole line, which is the user's explicit choice, not
    an automatic full-history send.
    """
    history = [dict(message) for message in (history_messages or [])]
    normalized = normalize_strategy(strategy)
    if normalized == STRATEGY_SLIDING:
        payload = build_sliding_payload("", history, "", sliding_window_messages)
    elif normalized == STRATEGY_FACTS:
        payload = build_facts_payload(
            "", facts or [], history, "", facts_window_messages
        )
    elif normalized == STRATEGY_SUMMARY:
        payload = build_payload(
            "",
            history,
            "",
            summary_content,
            covered_messages_count,
            True,
        )
    else:
        payload = build_payload("", history, "", summarize_enabled=False)
    return [dict(message) for message in payload[1:-1]]


def _history_preview(messages) -> str:
    return "\n\n".join(
        f"[{message.get('role', 'unknown')}] {message.get('content', '')}"
        for message in messages
    )


class StageContextBuilder:
    """Build the per-stage context packet without any I/O (FR-27).

    The builder is stateless: every input is an explicit keyword argument, so a
    caller can build a real call packet and an API-free preview with the same
    code. Artifacts are filtered here to the last revision of the kinds the
    stage actually needs; the caller passes everything it has.
    """

    def select_history(self, history_messages, **kwargs) -> list:
        """See :func:`select_history`."""
        return select_history(history_messages, **kwargs)

    def select_artifacts(self, stage, *, task=None, plan=None, artifacts=(), progress=()):
        """Return the ``(block_name, content)`` pairs required by the stage.

        planning: the task brief; execution: the specification, the plan and
        the rework defects; validation: the specification, the plan without its
        criteria, the acceptance criteria, the latest result of every step and
        the limitations reported by the previous validation.
        """
        selected = []

        def add(name, content):
            text = str(content or "").strip()
            if text:
                selected.append((name, text))

        if stage == STAGE_PLANNING:
            add(BLOCK_TASK_BRIEF, format_task_brief_block(
                _latest_artifact(artifacts, ARTIFACT_TASK_BRIEF)
            ))
            return selected

        if stage not in (STAGE_EXECUTION, STAGE_VALIDATION):
            return selected

        specification = _latest_artifact(artifacts, ARTIFACT_SPECIFICATION)
        if specification is not None:
            add(BLOCK_SPECIFICATION, _content_of(specification).get("markdown"))

        if stage == STAGE_EXECUTION:
            add(BLOCK_PLAN, format_plan_block(plan, include_criteria=True))
            add(BLOCK_DEFECTS, format_defects_block(progress))
            return selected

        if stage == STAGE_VALIDATION:
            add(BLOCK_PLAN, format_plan_block(plan, include_criteria=False))
            add(BLOCK_ACCEPTANCE_CRITERIA, format_acceptance_criteria_block(plan))
            add(
                BLOCK_EXECUTION_RESULTS,
                format_execution_results_block(_latest_executions(artifacts)),
            )
            add(
                BLOCK_KNOWN_LIMITATIONS,
                format_limitations_block(
                    _latest_artifact(artifacts, ARTIFACT_VALIDATION_RESULT)
                ),
            )
        return selected

    def build_action_message(
        self, stage, action, *, task, plan=None, artifacts=(), progress=()
    ) -> str:
        """Build the synthetic action message of the stage (block 9).

        The message comes from ``task_prompts`` so the packet and the stage
        call contract stay identical. ``run_step`` without a current step and a
        non-LLM action yield an empty string, which drops the block.
        """
        if action == ACTION_RUN_PLANNING:
            brief = str(
                _content_of(_latest_artifact(artifacts, ARTIFACT_TASK_BRIEF)).get("text")
                or ""
            ).strip()
            goal = getattr(task, "goal", "") or ""
            return build_plan_messages(goal, brief)[1]["content"]

        if action == ACTION_RUN_STEP:
            step = _current_step(task, plan)
            if step is None:
                return ""
            defects = _defects_for_step(progress, getattr(task, "current_step_index", None))
            return build_step_messages(step, defects)[1]["content"]

        if action == ACTION_RUN_VALIDATION:
            goal = getattr(task, "goal", "") or ""
            criteria = (plan or {}).get("acceptance_criteria") if isinstance(plan, dict) else None
            return build_validation_messages(
                goal,
                criteria,
                plan_steps(plan),
                latest_executions(artifacts),
            )[1]["content"]

        return ""

    def build_context_packet(
        self,
        *,
        stage,
        action,
        task=None,
        workflow=None,
        plan=None,
        artifacts=(),
        progress=(),
        system_prompt="",
        invariants="",
        structural_invariants=(),
        profile_block=None,
        working_items=(),
        long_term_items=(),
        history_messages=(),
        summary_content=None,
        covered_messages_count=0,
        strategy=None,
        sliding_window_messages=DEFAULT_SLIDING_WINDOW,
        facts_window_messages=DEFAULT_FACTS_WINDOW,
        facts=(),
        model=DEFAULT_MODEL,
        action_message=None,
        dt=None,
    ) -> ContextPacket:
        """Assemble the task packet in the fixed block order of FR-27."""
        moment = dt if dt is not None else datetime.now(timezone.utc)
        blocks: list = []
        messages: list = []

        def add(name, role, content):
            text = str(content or "").strip()
            if not text:
                return
            tokens = estimate_tokens(text)
            blocks.append(
                ContextBlock(
                    name=name,
                    role=role,
                    content=text,
                    tokens=tokens,
                    cost_usd=estimate_tokens_cost(model, tokens, moment),
                )
            )
            messages.append({"role": role, "content": text})

        # 1) base system prompt, 2) invariants, 3) structural invariants,
        # 4) active profile.
        add(BLOCK_SYSTEM_PROMPT, "system", system_prompt)
        add(BLOCK_INVARIANTS, "system", format_invariants_block(invariants))
        add(
            BLOCK_STRUCTURAL_INVARIANTS,
            "system",
            format_structural_invariants_block(structural_invariants),
        )
        add(BLOCK_PROFILE, "system", _profile_text(profile_block))

        # 5) workflow and stage instructions, taken from the same contract the
        # stage call uses, so the packet prompt never drifts from task_prompts.
        add(
            BLOCK_WORKFLOW,
            "system",
            _join(_workflow_instruction(workflow, stage, action), _stage_instruction(stage)),
        )

        # 6) task snapshot.
        add(BLOCK_TASK_SNAPSHOT, "system", format_snapshot_block(task) if task is not None else "")

        # 7) required artifacts of the earlier stages.
        for name, content in self.select_artifacts(
            stage, task=task, plan=plan, artifacts=artifacts, progress=progress
        ):
            add(name, "system", content)

        # 8) memory items, working first.
        add(BLOCK_WORKING_MEMORY, "system", format_memory_block(working_items, WORKING_MEMORY_BLOCK_TITLE))
        add(BLOCK_LONG_TERM_MEMORY, "system", format_memory_block(long_term_items, LONG_TERM_MEMORY_BLOCK_TITLE))

        # 9) the strategy-selected history slice: several messages, not one
        # system block, because the strategy decides their roles.
        selected = select_history(
            history_messages,
            strategy=strategy,
            summary_content=summary_content,
            covered_messages_count=covered_messages_count,
            sliding_window_messages=sliding_window_messages,
            facts_window_messages=facts_window_messages,
            facts=facts,
        )
        if selected:
            tokens = sum(estimate_tokens(message.get("content") or "") for message in selected)
            blocks.append(
                ContextBlock(
                    name=BLOCK_HISTORY,
                    role="history",
                    content=_history_preview(selected),
                    tokens=tokens,
                    cost_usd=estimate_tokens_cost(model, tokens, moment),
                )
            )
            messages.extend(selected)

        # 10) the current action.
        text = action_message
        if text is None:
            text = self.build_action_message(
                stage,
                action,
                task=task,
                plan=plan,
                artifacts=artifacts,
                progress=progress,
            )
        add(BLOCK_ACTION, "user", text)

        total_tokens = sum(block.tokens for block in blocks)
        costs = [block.cost_usd for block in blocks]
        total_cost = sum(costs) if costs and all(cost is not None for cost in costs) else None
        return ContextPacket(
            blocks=blocks,
            messages=messages,
            total_tokens=total_tokens,
            total_cost_usd=total_cost,
            stage=stage,
            action=action,
        )


def _current_step(task, plan):
    """Return the plan step the task points at, or ``None``."""
    index = getattr(task, "current_step_index", None)
    for step in plan_steps(plan):
        if step["index"] == index:
            return dict(step)
    return None


def _defects_for_step(progress, step_index) -> list:
    for step in progress or ():
        if getattr(step, "step_index", None) == step_index:
            return list(getattr(step, "defects", ()) or ())
    return []


def build_context_packet(**kwargs) -> ContextPacket:
    """See :meth:`StageContextBuilder.build_context_packet`."""
    return StageContextBuilder().build_context_packet(**kwargs)
