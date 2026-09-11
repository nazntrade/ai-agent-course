"""First agent: encapsulates the LLM interaction and message history.

The module does not depend on Streamlit and can be tested in isolation.
"""

from __future__ import annotations

import os
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from dotenv import load_dotenv
from openai import OpenAI

from context import (
    COMPRESSION_BATCH_TURNS,
    SUMMARY_MAX_TOKENS,
    SUMMARY_RETRY_MAX_TOKENS,
    SUMMARY_TEMPERATURE,
    build_payload,
    build_summarization_messages,
    clamp_covered_messages,
    plan_compression,
    validate_summary,
)
from models import DEFAULT_MODEL, normalize_model
from pricing import estimate_cost
from stats import AskResult, SummaryOutcome, TurnStats, aggregate_stats
from tokens import estimate_tokens

BASE_URL = "https://api.deepseek.com"
API_KEY_ENV = "DEEPSEEK_API_KEY"

DEFAULT_SYSTEM_PROMPT = "Ты — полезный ассистент. Отвечай кратко и по делу."


def get_client():
    """Return a DeepSeek API client, or None if the key is not set."""
    load_dotenv()
    api_key = os.getenv(API_KEY_ENV)
    if not api_key:
        return None
    return OpenAI(api_key=api_key, base_url=BASE_URL)


@dataclass
class AgentConfig:
    """Agent configuration: model and generation parameters."""

    model: str = DEFAULT_MODEL
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    temperature: float = 0.2
    max_tokens: int = 1500
    stream: bool = True
    demo_context_limit: int | None = None
    summarize: bool = True
    keep_recent_turns: int = 3

    def __post_init__(self):
        """Normalize a legacy model ID to its canonical form.

        Normalizing here means any config source (constructor, restored store
        row, UI input) always reaches the API under the canonical ID.
        """
        self.model = normalize_model(self.model)


class ContextLimitError(Exception):
    """Raised before the API call when the demo context limit is exceeded.

    The demo pre-check is a local estimate: it never reaches the provider and
    leaves both the in-memory history and the database untouched.
    """

    def __init__(self, estimated_input_tokens, limit_tokens, requested_output_tokens):
        self.estimated_input_tokens = estimated_input_tokens
        self.limit_tokens = limit_tokens
        self.requested_output_tokens = requested_output_tokens
        super().__init__(
            "Превышен демо-лимит контекста: оценка входа "
            f"{estimated_input_tokens} ток. + запрошенный ответ "
            f"{requested_output_tokens} ток. = "
            f"{estimated_input_tokens + requested_output_tokens} ток., "
            f"что больше лимита {limit_tokens} ток. "
            "Сократите историю или запрос, либо увеличьте лимит в настройках."
        )


class ApiContextOverflowError(Exception):
    """Raised when the provider rejects the request because the context is too long."""

    def __init__(self, original_message):
        self.original_message = original_message
        super().__init__(
            "Контекст превысил лимит модели на стороне провайдера. "
            f"Сообщение провайдера: {original_message}"
        )


def _extract_usage(usage_obj) -> dict:
    """Normalize a usage object (or dict) into a flat dict of token counts.

    Supports both attribute and dict access, plus the
    ``prompt_tokens_details.cached_tokens`` fallback some providers use.
    Missing fields are omitted rather than invented.
    """
    if usage_obj is None:
        return {}

    def field(name):
        if isinstance(usage_obj, dict):
            return usage_obj.get(name)
        return getattr(usage_obj, name, None)

    def to_int(value):
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    prompt = to_int(field("prompt_tokens"))
    completion = to_int(field("completion_tokens"))
    total = to_int(field("total_tokens"))
    hit = to_int(field("prompt_cache_hit_tokens"))
    miss = to_int(field("prompt_cache_miss_tokens"))

    details = field("prompt_tokens_details")
    cached = None
    if details is not None:
        if isinstance(details, dict):
            cached = to_int(details.get("cached_tokens"))
        else:
            cached = to_int(getattr(details, "cached_tokens", None))

    if hit is None and cached is not None:
        hit = cached
        if miss is None and prompt is not None:
            miss = prompt - cached

    result = {}
    if prompt is not None:
        result["prompt_tokens"] = prompt
    if completion is not None:
        result["completion_tokens"] = completion
    if total is not None:
        result["total_tokens"] = total
    if hit is not None:
        result["prompt_cache_hit_tokens"] = hit
    if miss is not None:
        result["prompt_cache_miss_tokens"] = miss

    return result


def _is_context_overflow(exc) -> bool:
    """Detect a provider context-length error by duck typing only.

    Avoids importing ``openai.BadRequestError`` so tests can pass a plain fake
    exception carrying ``status_code``.
    """
    return (
        getattr(exc, "status_code", None) == 400
        and "context" in str(exc).lower()
    )


class ChatAgent:
    """Agent that stores the dialog history and talks to the LLM via a client.

    When ``store`` is provided the agent is bound to ``chat_id`` and persists
    history/configuration per chat. ``store`` is duck-typed and expected to
    expose::

        load_config(chat_id) -> dict
        save_config(chat_id, config) -> None
        load_messages(chat_id) -> list[{"role", "content"}]
        save_turn(chat_id, user_text, assistant_text, stats=None) -> None

    When ``store`` is None the agent keeps a pure in-memory history.
    """

    def __init__(self, client, store=None, chat_id=None, config=None):
        self._client = client
        self._store = store
        self._chat_id = chat_id
        if config is None and store is not None:
            config = AgentConfig(**store.load_config(chat_id))
        self._config = config if config is not None else AgentConfig()

        history = [{"role": "system", "content": self._config.system_prompt}]
        if store is not None and chat_id is not None:
            history.extend(store.load_messages(chat_id))
        self._history = history

        self._summary_content = None
        self._covered_messages_count = 0
        load_summary = getattr(store, "load_summary", None) if store is not None else None
        if load_summary is not None and chat_id is not None:
            stored = load_summary(chat_id)
            if stored is not None:
                self._summary_content = stored.content
                self._covered_messages_count = clamp_covered_messages(
                    stored.covered_messages_count, len(history) - 1
                )

    @property
    def history(self):
        """A copy of the history so external code cannot mutate it."""
        return [dict(message) for message in self._history]

    @property
    def config(self):
        """A copy of the config so external mutation cannot corrupt state."""
        return AgentConfig(**asdict(self._config))

    def set_config(self, config: AgentConfig):
        """Replace the configuration, persisting it when a store is bound."""
        self._config = config
        self._history[0]["content"] = config.system_prompt
        if self._store is not None:
            self._store.save_config(self._chat_id, config)

    def ask(self, user_message: str, on_chunk=None, on_summarizing=None) -> AskResult:
        """Send a message to the LLM and return the answer plus its statistics.

        Supports both streaming and non-streaming modes:
        - ``stream=True``: iterate chunks, invoke ``on_chunk`` with the
          accumulated text, and capture usage/finish reason from the chunks.
        - ``stream=False``: single response; ``on_chunk`` is not called.

        When the demo context limit is exceeded, compression is attempted first
        (if enabled) so a long chat can still produce a reply; only when the
        compressed payload still does not fit (or compression is disabled) is
        :class:`ContextLimitError` raised, before any history/database mutation.

        After a successful turn, compression runs in batches: the first summary
        is created as soon as one old turn accumulates, later updates only once
        enough additional turns accumulate. A compression failure never breaks
        the turn: it is reported through ``AskResult.summary_error``.
        """
        if not user_message.strip():
            raise ValueError("Message must not be empty")

        summary_error = None

        # Demo pre-check runs before the API call and before mutating history.
        payload_messages = self._build_payload(user_message)
        est_input = sum(
            estimate_tokens(message["content"]) for message in payload_messages
        )
        if (
            self._config.demo_context_limit is not None
            and est_input + self._config.max_tokens > self._config.demo_context_limit
        ):
            if self._config.summarize:
                err = self._try_compress(on_summarizing, min_batch=1)
                summary_error = summary_error or err
                payload_messages = self._build_payload(user_message)
                est_input = sum(
                    estimate_tokens(message["content"]) for message in payload_messages
                )
            if est_input + self._config.max_tokens > self._config.demo_context_limit:
                raise ContextLimitError(
                    estimated_input_tokens=est_input,
                    limit_tokens=self._config.demo_context_limit,
                    requested_output_tokens=self._config.max_tokens,
                )

        self._history.append({"role": "user", "content": user_message})

        payload = {
            "model": self._config.model,
            "messages": payload_messages,
            "temperature": self._config.temperature,
            "max_tokens": self._config.max_tokens,
            "stream": self._config.stream,
        }

        usage_map: dict = {}
        finish_reason = None

        try:
            if self._config.stream:
                payload["stream_options"] = {"include_usage": True}
                response = self._client.chat.completions.create(**payload)
                collected = []
                for chunk in response:
                    chunk_usage = getattr(chunk, "usage", None)
                    if chunk_usage is not None:
                        usage_map = _extract_usage(chunk_usage)
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    if choice.delta and choice.delta.content:
                        collected.append(choice.delta.content)
                        if on_chunk is not None:
                            on_chunk("".join(collected))
                    if getattr(choice, "finish_reason", None) is not None:
                        finish_reason = choice.finish_reason
                answer = "".join(collected)
            else:
                response = self._client.chat.completions.create(**payload)
                answer = response.choices[0].message.content or ""
                usage_map = _extract_usage(getattr(response, "usage", None))
                finish_reason = getattr(response.choices[0], "finish_reason", None)
        except Exception as exc:
            self._history.pop()
            if _is_context_overflow(exc):
                raise ApiContextOverflowError(str(exc)) from exc
            raise

        stats = TurnStats(
            user_message_tokens_est=estimate_tokens(user_message),
            request_tokens=usage_map.get("prompt_tokens"),
            response_tokens=usage_map.get("completion_tokens"),
            total_tokens=usage_map.get("total_tokens"),
            prompt_cache_hit_tokens=usage_map.get("prompt_cache_hit_tokens"),
            prompt_cache_miss_tokens=usage_map.get("prompt_cache_miss_tokens"),
            finish_reason=finish_reason,
            assistant_tokens_est=estimate_tokens(answer),
        )

        cost = estimate_cost(self._config.model, stats, datetime.now(timezone.utc))
        stats.cost_usd = cost.cost_usd
        stats.cost_assumption = cost.assumption

        self._history.append({"role": "assistant", "content": answer})
        if self._store is not None:
            try:
                self._store.save_turn(self._chat_id, user_message, answer, stats=stats)
            except Exception:
                self._history.pop()
                self._history.pop()
                raise

        # Batch compression: the first summary is created eagerly, subsequent
        # updates only after COMPRESSION_BATCH_TURNS more turns accumulate.
        min_batch = COMPRESSION_BATCH_TURNS if self._covered_messages_count > 0 else 1
        err = self._try_compress(on_summarizing, min_batch)
        summary_error = summary_error or err

        return AskResult(text=answer, stats=stats, summary_error=summary_error)

    def _build_payload(self, user_message):
        """Assemble the messages for the next request, applying the summary."""
        return build_payload(
            self._config.system_prompt,
            self._history[1:],
            user_message,
            self._summary_content,
            self._covered_messages_count,
            self._config.summarize,
        )

    def _try_compress(self, on_summarizing, min_batch) -> str | None:
        """Compress old history into the summary when enough turns accumulated.

        Every executed summarisation attempt is billed, even when the summary
        itself is not updated: a failed run records the aggregate attempt cost
        without touching the summary or its coverage boundary, while a
        successful run stores the summary and the aggregate stats in one call.
        Returns a human-readable error string on failure, or ``None`` when
        there was nothing to compress or it succeeded.
        """
        if not self._config.summarize:
            return None
        plan = plan_compression(
            self._history[1:],
            self._covered_messages_count,
            self._config.keep_recent_turns,
            min_batch,
        )
        if plan is None:
            return None

        try:
            cm = on_summarizing() if on_summarizing is not None else None
            with (cm or nullcontext()):
                outcome = self._run_summarization(plan.messages_to_merge)
        except Exception as exc:
            return (
                "Не удалось обновить сводку истории "
                f"({exc}). Переписка сохранена; сжатие повторится, "
                "когда накопится достаточно старых ходов."
            )

        aggregate = aggregate_stats(outcome.attempts)

        if outcome.text is None:
            accounting_note = ""
            recorder = (
                getattr(self._store, "record_summary_attempt", None)
                if self._store is not None
                else None
            )
            if outcome.attempts and recorder is not None:
                try:
                    recorder(self._chat_id, stats=aggregate)
                except Exception as exc:
                    accounting_note = (
                        f" Сохранить расход попыток не удалось ({exc})."
                    )
            return (
                f"Сводка истории не обновлена ({outcome.error}). "
                "Переписка и прежняя сводка не изменены."
                f"{accounting_note} "
                "Сжатие повторится, когда накопится следующая порция старых ходов."
            )

        try:
            save_summary = (
                getattr(self._store, "save_summary", None)
                if self._store is not None
                else None
            )
            if save_summary is not None:
                save_summary(
                    self._chat_id,
                    outcome.text,
                    plan.new_covered_messages_count,
                    stats=aggregate,
                )
            self._summary_content = outcome.text
            self._covered_messages_count = plan.new_covered_messages_count
            return None
        except Exception as exc:
            return (
                "Не удалось обновить сводку истории "
                f"({exc}). Переписка сохранена; сжатие повторится, "
                "когда накопится достаточно старых ходов."
            )

    def _summary_attempt_stats(self, response) -> TurnStats:
        """Extract usage/finish reason of one summarisation attempt and price it."""
        finish_reason = getattr(response.choices[0], "finish_reason", None)
        usage_map = _extract_usage(getattr(response, "usage", None))
        stats = TurnStats(
            request_tokens=usage_map.get("prompt_tokens"),
            response_tokens=usage_map.get("completion_tokens"),
            total_tokens=usage_map.get("total_tokens"),
            prompt_cache_hit_tokens=usage_map.get("prompt_cache_hit_tokens"),
            prompt_cache_miss_tokens=usage_map.get("prompt_cache_miss_tokens"),
            finish_reason=finish_reason,
        )
        cost = estimate_cost(self._config.model, stats, datetime.now(timezone.utc))
        stats.cost_usd = cost.cost_usd
        stats.cost_assumption = cost.assumption
        return stats

    def _run_summarization(self, messages_to_merge) -> SummaryOutcome:
        """Run one or two summarisation attempts and return their outcome.

        Provider and validation failures are never raised: every executed
        attempt is kept in ``attempts`` so it can be billed, and the failure is
        reported through ``error``. Only the statistics of the executed calls
        and the parsed text/error are returned.
        """
        messages = build_summarization_messages(
            self._summary_content, messages_to_merge
        )
        attempts = []
        try:
            response = self._request_summary(messages, SUMMARY_MAX_TOKENS)
            attempts.append(self._summary_attempt_stats(response))
            if attempts[-1].finish_reason in ("length", "max_tokens"):
                response = self._request_summary(messages, SUMMARY_RETRY_MAX_TOKENS)
                attempts.append(self._summary_attempt_stats(response))
            if attempts[-1].finish_reason in ("length", "max_tokens"):
                return SummaryOutcome(
                    attempts=attempts,
                    error=(
                        "ответ модели обрезан "
                        f"(finish_reason={attempts[-1].finish_reason})"
                    ),
                )
            text = response.choices[0].message.content or ""
            validate_summary(text)
            return SummaryOutcome(attempts=attempts, text=text)
        except Exception as exc:
            return SummaryOutcome(attempts=attempts, error=str(exc))

    def _request_summary(self, messages, max_tokens):
        """Issue a single non-stream summarisation request."""
        return self._client.chat.completions.create(
            model=self._config.model,
            messages=messages,
            stream=False,
            temperature=SUMMARY_TEMPERATURE,
            max_tokens=max_tokens,
        )
