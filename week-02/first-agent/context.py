"""Pure context-compression logic for the chat agent.

Everything here operates on plain dicts and is deterministic, so the module
can be unit-tested in isolation. It performs no I/O and imports neither
Streamlit, nor storage, nor the provider client.
"""

from __future__ import annotations

from dataclasses import dataclass

from facts import format_facts_block

SUMMARY_FORMAT_VERSION = 1
# Reasoning models spend part of the output budget on hidden reasoning, so the
# visible summary needs headroom beyond the text itself. The base budget covers
# the common case; SUMMARY_RETRY_MAX_TOKENS is used for a single retry when the
# model still truncates.
SUMMARY_MAX_TOKENS = 1200
SUMMARY_RETRY_MAX_TOKENS = 2400
SUMMARY_TEMPERATURE = 0.2

# After the first summary exists, updates are batched: the model is only called
# again once at least this many additional old turns have accumulated, instead
# of summarising on every single reply.
COMPRESSION_BATCH_TURNS = 3

SUMMARY_SYSTEM_PROMPT = (
    "Ты ведёшь сжатую историю диалога. Объедини предыдущую сводку (если она есть) "
    "с новыми сообщениями в один краткий текст на русском языке. Сохраняй важные "
    "факты, ограничения и предпочтения пользователя, принятые решения, а также "
    "ошибки и их исправления. Если более позднее утверждение отменяет более "
    "раннее, оставь только актуальное. Не добавляй ничего, чего не было в "
    "диалоге."
)

_SUMMARY_PREFIX = "Сводка предыдущей части диалога:\n"
_PREVIOUS_SUMMARY_PREFIX = "Предыдущая сводка:\n"
_NEW_MESSAGES_PREFIX = "Новые сообщения диалога:\n"


@dataclass
class CompressionPlan:
    """Messages to fold into the summary and the resulting coverage anchor."""

    messages_to_merge: list[dict]
    turns_to_merge: int
    new_covered_messages_count: int


def clamp_covered_messages(covered_messages_count: int, pair_count: int) -> int:
    """Clamp a coverage anchor into ``[0, pair_count]`` and round down to even.

    The anchor counts whole messages, so an odd value would split a user/
    assistant pair; rounding down keeps the summary aligned to turn boundaries.
    """
    covered = max(int(covered_messages_count), 0)
    covered = min(covered, pair_count)
    if covered % 2 != 0:
        covered -= 1
    return covered


def build_payload(
    system_prompt,
    pairs,
    new_user_message,
    summary_content=None,
    covered_messages_count=0,
    summarize_enabled=True,
):
    """Assemble the message list sent to the model.

    With compression enabled and a summary present, covered messages are
    replaced by a ``system`` summary message; the rest of the history and the
    new user message follow. Otherwise the full history is sent unchanged.
    """
    messages = [{"role": "system", "content": system_prompt}]
    if summarize_enabled and summary_content:
        covered = clamp_covered_messages(covered_messages_count, len(pairs))
        messages.append(
            {"role": "system", "content": _SUMMARY_PREFIX + summary_content}
        )
        messages.extend(pairs[covered:])
    else:
        messages.extend(pairs)
    messages.append({"role": "user", "content": new_user_message})
    return messages


def plan_compression(
    pairs, covered_messages_count, keep_recent_turns, min_batch=1
) -> CompressionPlan | None:
    """Decide which uncovered messages should be folded into the summary.

    ``keep_recent_turns`` full turns are always left uncompressed. When the
    uncovered tail holds fewer than ``min_batch`` turns beyond that window,
    nothing is merged and ``None`` is returned; otherwise the whole excess is
    folded in one go. ``min_batch=1`` reproduces the original every-turn
    behaviour.
    """
    covered = clamp_covered_messages(covered_messages_count, len(pairs))
    uncovered = pairs[covered:]
    uncovered_turns = len(uncovered) // 2
    turns_to_merge = uncovered_turns - keep_recent_turns
    if turns_to_merge < min_batch:
        return None
    messages_to_merge = list(uncovered[: turns_to_merge * 2])
    return CompressionPlan(
        messages_to_merge=messages_to_merge,
        turns_to_merge=turns_to_merge,
        new_covered_messages_count=covered + len(messages_to_merge),
    )


def build_summarization_messages(previous_summary, messages_to_merge):
    """Build the prompt for producing/updating the summary."""
    messages = [{"role": "system", "content": SUMMARY_SYSTEM_PROMPT}]
    if previous_summary:
        messages.append(
            {"role": "user", "content": _PREVIOUS_SUMMARY_PREFIX + previous_summary}
        )
    lines = [
        f"[{message.get('role', 'unknown')}]: {message.get('content', '')}"
        for message in messages_to_merge
    ]
    messages.append(
        {"role": "user", "content": _NEW_MESSAGES_PREFIX + "\n".join(lines)}
    )
    return messages


def validate_summary(text) -> None:
    """Reject an empty or whitespace-only summary."""
    if text is None or not str(text).strip():
        raise ValueError("Summary must not be empty")


def build_sliding_payload(
    system_prompt, messages, new_user_message, window_messages
):
    """Assemble a payload with only the last ``window_messages`` messages.

    ``window_messages`` counts the total non-system messages including the new
    user message, so ``window_messages - 1`` history messages are kept. A window
    of 1 sends only the current request. The system prompt is never part of the
    window.
    """
    window = max(int(window_messages), 1)
    payload = [{"role": "system", "content": system_prompt}]
    if window > 1:
        payload.extend(list(messages)[-(window - 1):])
    payload.append({"role": "user", "content": new_user_message})
    return payload


def build_facts_payload(
    system_prompt, facts, messages, new_user_message, window_messages
):
    """Assemble a sliding payload plus a sticky-facts system block.

    The active-facts block is inserted right after the system prompt when at
    least one fact is active; the recent-message window follows the same rule
    as :func:`build_sliding_payload`.
    """
    window = max(int(window_messages), 1)
    payload = [{"role": "system", "content": system_prompt}]
    facts_block = format_facts_block(facts)
    if facts_block:
        payload.append({"role": "system", "content": facts_block})
    if window > 1:
        payload.extend(list(messages)[-(window - 1):])
    payload.append({"role": "user", "content": new_user_message})
    return payload
