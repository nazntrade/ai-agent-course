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
    build_facts_payload,
    build_payload,
    build_sliding_payload,
    build_summarization_messages,
    clamp_covered_messages,
    plan_compression,
    validate_summary,
)
from facts import (
    FACTS_MAX_TOKENS,
    FACTS_RETRY_MAX_TOKENS,
    FACTS_TEMPERATURE,
    FactsOutcome,
    build_facts_extraction_messages,
    parse_facts_response,
)
from memory import (
    MEMORY_SCOPE_LONG_TERM,
    MEMORY_SCOPE_WORKING,
    build_memory_blocks,
    format_invariants_block,
    insert_system_blocks,
)
from models import DEFAULT_MODEL, normalize_model
from pricing import estimate_cost
from profile import UserProfile, format_profile_block
from stats import AskResult, SummaryOutcome, TurnStats, aggregate_stats
from strategies import (
    DEFAULT_FACTS_WINDOW,
    DEFAULT_SLIDING_WINDOW,
    STRATEGY_FACTS,
    STRATEGY_FULL,
    STRATEGY_SLIDING,
    STRATEGY_SUMMARY,
    normalize_strategy,
    normalize_window,
    summary_compression_enabled,
)
from tokens import estimate_tokens

BASE_URL = "https://api.deepseek.com"
API_KEY_ENV = "DEEPSEEK_API_KEY"

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful assistant. Answer briefly and to the point."
)


def get_client():
    """Return a DeepSeek API client, or None if the key is not set."""
    load_dotenv()
    api_key = os.getenv(API_KEY_ENV)
    if not api_key:
        return None
    return OpenAI(api_key=api_key, base_url=BASE_URL)


@dataclass
class AgentConfig:
    """Agent configuration: model, generation and context-strategy parameters."""

    model: str = DEFAULT_MODEL
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    temperature: float = 0.2
    max_tokens: int = 1500
    stream: bool = True
    demo_context_limit: int | None = None
    summarize: bool = True
    keep_recent_turns: int = 3
    context_strategy: str | None = None
    sliding_window_messages: int = 6
    facts_window_messages: int = 6
    invariants: str = ""

    def __post_init__(self):
        """Normalize the model, the context strategy and the window sizes.

        ``context_strategy`` is the single source of truth for the payload
        mode. When it is missing, the legacy ``summarize`` flag maps onto
        full/summary. ``summarize`` itself is never rewritten from the
        strategy, so an explicitly disabled compression stays disabled.
        """
        self.model = normalize_model(self.model)
        if self.context_strategy is None:
            self.context_strategy = STRATEGY_SUMMARY if self.summarize else STRATEGY_FULL
        else:
            self.context_strategy = normalize_strategy(self.context_strategy)
        self.sliding_window_messages = normalize_window(
            self.sliding_window_messages, DEFAULT_SLIDING_WINDOW
        )
        self.facts_window_messages = normalize_window(
            self.facts_window_messages, DEFAULT_FACTS_WINDOW
        )


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
            "Demo context limit exceeded: estimated input "
            f"{estimated_input_tokens} tokens + requested output "
            f"{requested_output_tokens} tokens = "
            f"{estimated_input_tokens + requested_output_tokens} tokens, "
            f"which is above the limit of {limit_tokens} tokens. "
            "Shorten the history or the request, or raise the limit in the settings."
        )


class ApiContextOverflowError(Exception):
    """Raised when the provider rejects the request because the context is too long."""

    def __init__(self, original_message):
        self.original_message = original_message
        super().__init__(
            "The context exceeded the model limit on the provider side. "
            f"Provider message: {original_message}"
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

        self._active_branch_id = None
        get_active = getattr(store, "get_active_branch", None) if store is not None else None
        if get_active is not None and chat_id is not None:
            branch = get_active(chat_id)
            if branch is not None:
                self._active_branch_id = branch.id

        history = [{"role": "system", "content": self._config.system_prompt}]
        if store is not None and chat_id is not None:
            load_line = getattr(store, "load_branch_line", None)
            if load_line is not None:
                history.extend(
                    {"role": message.role, "content": message.content}
                    for message in load_line(chat_id)
                )
            else:
                history.extend(store.load_messages(chat_id))
        self._history = history

        self._summary_content = None
        self._covered_messages_count = 0
        self._facts = []
        self._facts_anchor = None
        self._reload_auxiliary_state()
        self._working_memory = []
        self._long_term_memory = []
        self._reload_memory()
        self._active_profile = None
        self._reload_profile()

    def _resolve_active_profile(self) -> UserProfile | None:
        """Return the profile manually assigned to this chat, if any.

        This is the single place where the active profile is chosen, so a future
        automatic router only has to change this method. The store access is
        duck-typed: a store without ``get_active_profile`` yields ``None``, and a
        missing or dangling assignment means No profile.
        """
        if self._store is None or self._chat_id is None:
            return None
        get_active = getattr(self._store, "get_active_profile", None)
        if get_active is None:
            return None
        return get_active(self._chat_id)

    def _reload_profile(self) -> None:
        """Re-read the active profile from the store; never calls the provider."""
        self._active_profile = self._resolve_active_profile()

    def reload_profile(self) -> None:
        """Public counterpart of ``_reload_profile`` used by the UI after edits."""
        self._reload_profile()

    def _reload_memory(self) -> None:
        """Load working and long-term memory for the active line without API calls.

        Duck-typed: a store that predates Day 11 has no ``list_memory_items``,
        so both layers simply stay empty instead of breaking the agent.
        """
        self._working_memory = []
        self._long_term_memory = []
        if self._store is None or self._chat_id is None:
            return
        list_items = getattr(self._store, "list_memory_items", None)
        if list_items is None:
            return
        self._working_memory = list_items(
            MEMORY_SCOPE_WORKING,
            chat_id=self._chat_id,
            branch_id=self._active_branch_id,
        )
        self._long_term_memory = list_items(MEMORY_SCOPE_LONG_TERM)

    def reload_memory(self) -> None:
        """Re-read both memory layers from the store; never calls the provider."""
        self._reload_memory()

    def _reload_auxiliary_state(self) -> None:
        """Load the summary and facts for the active line without API calls.

        Every accessor is duck-typed: a store that predates Day 10 simply
        leaves the auxiliary state empty instead of breaking.
        """
        if self._store is None or self._chat_id is None:
            return

        load_summary = getattr(self._store, "load_summary", None)
        if load_summary is not None:
            stored = load_summary(self._chat_id, branch_id=self._active_branch_id)
            if stored is not None:
                self._summary_content = stored.content
                self._covered_messages_count = clamp_covered_messages(
                    stored.covered_messages_count, len(self._history) - 1
                )
            else:
                self._summary_content = None
                self._covered_messages_count = 0

        load_facts = getattr(self._store, "load_facts", None)
        self._facts = load_facts(self._chat_id) if load_facts is not None else []
        get_anchor = getattr(self._store, "get_facts_anchor", None)
        self._facts_anchor = (
            get_anchor(self._chat_id) if get_anchor is not None else None
        )

    @property
    def history(self):
        """A copy of the history so external code cannot mutate it."""
        return [dict(message) for message in self._history]

    @property
    def config(self):
        """A copy of the config so external mutation cannot corrupt state."""
        return AgentConfig(**asdict(self._config))

    @property
    def active_branch_id(self):
        """The active branch id, or ``None`` for the main line."""
        return self._active_branch_id

    @property
    def working_memory(self):
        """A copy of the active line's working memory."""
        return list(self._working_memory)

    @property
    def long_term_memory(self):
        """A copy of the global long-term memory."""
        return list(self._long_term_memory)

    @property
    def active_profile(self):
        """The profile currently applied to requests, or ``None`` for No profile."""
        return self._active_profile

    def set_config(self, config: AgentConfig):
        """Replace the configuration, persisting it when a store is bound.

        A strategy change reloads the auxiliary summary/facts state for the
        active line; this is local and never triggers a paid call.
        """
        strategy_changed = config.context_strategy != self._config.context_strategy
        self._config = config
        self._history[0]["content"] = config.system_prompt
        if strategy_changed:
            self._reload_auxiliary_state()
        self._reload_memory()
        if self._store is not None:
            self._store.save_config(self._chat_id, config)

    def ask(
        self,
        user_message: str,
        on_chunk=None,
        on_summarizing=None,
        on_facts_updating=None,
    ) -> AskResult:
        """Send a message to the LLM and return the answer plus its statistics.

        Supports both streaming and non-streaming modes:
        - ``stream=True``: iterate chunks, invoke ``on_chunk`` with the
          accumulated text, and capture usage/finish reason from the chunks.
        - ``stream=False``: single response; ``on_chunk`` is not called.

        The payload is assembled from the configured context strategy. When the
        demo context limit is exceeded, only the summary strategy may attempt a
        fallback compression; every other strategy fails before any request or
        mutation so no paid call happens.

        Post-turn work depends on the strategy: ``summary`` may update the
        rolling summary, ``sticky_facts`` may extract facts, and
        ``full``/``sliding``/``branching`` never trigger an auxiliary call.
        Failures are reported through ``summary_error``/``facts_error`` and
        never break the already-saved turn.
        """
        if not user_message.strip():
            raise ValueError("Message must not be empty")

        strategy = self._config.context_strategy
        summary_error = None
        facts_error = None

        # Demo pre-check runs before the API call and before mutating history.
        payload_messages = self._build_payload(user_message)
        est_input = self._estimate_payload(payload_messages)
        if (
            self._config.demo_context_limit is not None
            and est_input + self._config.max_tokens > self._config.demo_context_limit
        ):
            if summary_compression_enabled(strategy, self._config.summarize):
                err = self._try_compress(on_summarizing, min_batch=1)
                summary_error = summary_error or err
                payload_messages = self._build_payload(user_message)
                est_input = self._estimate_payload(payload_messages)
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
                if self._active_branch_id is None:
                    self._store.save_turn(
                        self._chat_id, user_message, answer, stats=stats
                    )
                else:
                    self._store.save_turn(
                        self._chat_id,
                        user_message,
                        answer,
                        stats=stats,
                        branch_id=self._active_branch_id,
                    )
            except Exception:
                self._history.pop()
                self._history.pop()
                raise
            self._record_last_context(est_input, stats)

        if strategy == STRATEGY_SUMMARY:
            # Batch compression: the first summary is created eagerly, later
            # updates only after COMPRESSION_BATCH_TURNS more turns accumulate.
            min_batch = (
                COMPRESSION_BATCH_TURNS if self._covered_messages_count > 0 else 1
            )
            err = self._try_compress(on_summarizing, min_batch)
            summary_error = summary_error or err
        elif strategy == STRATEGY_FACTS:
            facts_error = self._try_update_facts(on_facts_updating)

        return AskResult(
            text=answer,
            stats=stats,
            summary_error=summary_error,
            facts_error=facts_error,
        )

    def complete(
        self,
        messages,
        *,
        max_tokens=None,
        temperature=None,
        stream=False,
        on_chunk=None,
    ):
        """Run one standalone completion without touching the chat state.

        Used by the task stages: the model comes from this agent's config, and
        ``max_tokens``/``temperature`` fall back to the config when omitted.
        Streaming only happens when both ``stream`` and ``on_chunk`` are given;
        then ``stream_options={"include_usage": True}`` is set and the usage is
        taken from the final chunk, exactly as in :meth:`ask`. The method never
        touches ``_history``, never saves a turn, and never triggers the summary
        or facts pipelines; provider exceptions are propagated to the caller,
        which classifies them as ``API_ERROR``.
        """
        streaming = bool(stream) and on_chunk is not None
        payload = {
            "model": self._config.model,
            "messages": list(messages),
            "temperature": (
                temperature if temperature is not None else self._config.temperature
            ),
            "max_tokens": (
                max_tokens if max_tokens is not None else self._config.max_tokens
            ),
            "stream": streaming,
        }

        usage_map: dict = {}
        finish_reason = None

        if streaming:
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
                    on_chunk("".join(collected))
                if getattr(choice, "finish_reason", None) is not None:
                    finish_reason = choice.finish_reason
            text = "".join(collected)
        else:
            response = self._client.chat.completions.create(**payload)
            text = response.choices[0].message.content or ""
            usage_map = _extract_usage(getattr(response, "usage", None))
            finish_reason = getattr(response.choices[0], "finish_reason", None)

        stats = TurnStats(
            request_tokens=usage_map.get("prompt_tokens"),
            response_tokens=usage_map.get("completion_tokens"),
            total_tokens=usage_map.get("total_tokens"),
            prompt_cache_hit_tokens=usage_map.get("prompt_cache_hit_tokens"),
            prompt_cache_miss_tokens=usage_map.get("prompt_cache_miss_tokens"),
            finish_reason=finish_reason,
            assistant_tokens_est=estimate_tokens(text),
        )
        cost = estimate_cost(self._config.model, stats, datetime.now(timezone.utc))
        stats.cost_usd = cost.cost_usd
        stats.cost_assumption = cost.assumption
        return text, stats

    @staticmethod
    def _estimate_payload(payload_messages) -> int:
        return sum(estimate_tokens(message["content"]) for message in payload_messages)

    def _record_last_context(self, est_input, stats) -> None:
        """Store the size of the last main payload for the statistics panel.

        The exact ``prompt_tokens`` is preferred; when the provider reported
        none, the local estimate of the sent payload is used. This is a
        display-only metric, so a failure must not fail the saved turn.
        """
        if self._store is None:
            return
        setter = getattr(self._store, "set_last_context_tokens", None)
        if setter is None:
            return
        tokens = stats.request_tokens if stats.request_tokens is not None else est_input
        try:
            setter(self._chat_id, tokens)
        except Exception:
            pass

    def _build_payload(self, user_message):
        """Assemble the messages for the next request by context strategy.

        After the strategy-specific payload is built, the explicit blocks are
        inserted in the fixed order profile, invariants, working, long-term. The
        profile is re-read from the store first, so deleting the active profile
        while a chat is open already drops its block from the next request. With
        no profile, invariants and memory the payload is the Day 9-11 one.
        """
        self._reload_profile()
        strategy = self._config.context_strategy
        history = self._history[1:]
        if strategy == STRATEGY_SLIDING:
            payload = build_sliding_payload(
                self._config.system_prompt,
                history,
                user_message,
                self._config.sliding_window_messages,
            )
        elif strategy == STRATEGY_FACTS:
            payload = build_facts_payload(
                self._config.system_prompt,
                self._facts,
                history,
                user_message,
                self._config.facts_window_messages,
            )
        elif strategy == STRATEGY_SUMMARY:
            payload = build_payload(
                self._config.system_prompt,
                history,
                user_message,
                self._summary_content,
                self._covered_messages_count,
                summary_compression_enabled(strategy, self._config.summarize),
            )
        else:
            # full and branching both send the uncompressed line history.
            payload = build_payload(
                self._config.system_prompt,
                history,
                user_message,
                summarize_enabled=False,
            )
        return self._with_memory_blocks(payload)

    def _with_memory_blocks(self, payload):
        """Insert the profile, invariants and memory blocks after the system prompt.

        The profile block is only added when ``format_profile_block`` returns a
        block, so No profile never contributes an empty system message. The
        order is profile, invariants, working, long-term; with no blocks the
        original payload is returned unchanged.
        """
        blocks = []
        profile_block = format_profile_block(self._active_profile)
        if profile_block is not None:
            blocks.append(profile_block)
        invariants_block = format_invariants_block(self._config.invariants)
        if invariants_block is not None:
            blocks.append(invariants_block)
        blocks.extend(
            build_memory_blocks(self._working_memory, self._long_term_memory)
        )
        return insert_system_blocks(payload, blocks)

    def _try_compress(self, on_summarizing, min_batch) -> str | None:
        """Compress old history into the summary when enough turns accumulated.

        Every executed summarisation attempt is billed, even when the summary
        itself is not updated: a failed run records the aggregate attempt cost
        without touching the summary or its coverage boundary, while a
        successful run stores the summary and the aggregate stats in one call.
        Returns a human-readable error string on failure, or ``None`` when
        there was nothing to compress or it succeeded.
        """
        if not summary_compression_enabled(
            self._config.context_strategy, self._config.summarize
        ):
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
                "Could not update the history summary "
                f"({exc}). The conversation was saved; compression will retry "
                "once enough old turns have accumulated."
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
                        f" Failed to save the attempt cost ({exc})."
                    )
            return (
                f"History summary not updated ({outcome.error}). "
                "The conversation and the previous summary were not changed."
                f"{accounting_note} "
                "Compression will retry once the next batch of old turns accumulates."
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
                    branch_id=self._active_branch_id,
                )
            self._summary_content = outcome.text
            self._covered_messages_count = plan.new_covered_messages_count
            return None
        except Exception as exc:
            return (
                "Could not update the history summary "
                f"({exc}). The conversation was saved; compression will retry "
                "once enough old turns have accumulated."
            )

    def _attempt_stats(self, response) -> TurnStats:
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
            attempts.append(self._attempt_stats(response))
            if attempts[-1].finish_reason in ("length", "max_tokens"):
                response = self._request_summary(messages, SUMMARY_RETRY_MAX_TOKENS)
                attempts.append(self._attempt_stats(response))
            if attempts[-1].finish_reason in ("length", "max_tokens"):
                return SummaryOutcome(
                    attempts=attempts,
                    error=(
                        "model response was truncated "
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

    def _try_update_facts(self, on_facts_updating) -> str | None:
        """Update sticky facts from the messages not yet covered by the anchor.

        Only runs for the ``sticky_facts`` strategy inside a user turn; opening
        a chat, changing settings or switching lines never calls the provider.
        A successful call (including a valid empty list) advances the anchor and
        records an ok bucket; interrupted, invalid or failed calls go to the
        fail bucket and leave facts and anchor untouched. Returns a warning
        string on failure, or ``None`` on success/no-op.
        """
        strategy = self._config.context_strategy
        if strategy != STRATEGY_FACTS or self._store is None or self._chat_id is None:
            return None
        load_after = getattr(self._store, "load_line_messages_after", None)
        save_facts = getattr(self._store, "save_facts", None)
        if load_after is None or save_facts is None:
            return None

        anchor = self._facts_anchor or 0
        pending = load_after(self._chat_id, anchor)
        if not pending:
            return None
        pending_messages = [
            {"role": message.role, "content": message.content} for message in pending
        ]
        new_anchor = max(
            message.id for message in pending if message.id is not None
        )

        try:
            cm = on_facts_updating() if on_facts_updating is not None else None
            with (cm or nullcontext()):
                outcome = self._run_facts_update(self._facts, pending_messages)
        except Exception as exc:
            return (
                f"Could not update facts ({exc}). The conversation was saved; "
                "previous memory was not changed."
            )

        record = getattr(self._store, "record_facts_attempt", None)
        if record is not None:
            for attempt_stats, ok in outcome.attempts:
                try:
                    record(self._chat_id, attempt_stats, not ok)
                except Exception:
                    # Accounting must never break an already-saved turn.
                    pass

        if outcome.operations is None:
            return (
                f"Facts not updated ({outcome.error}). "
                "The conversation and previous memory were not changed."
            )

        try:
            save_facts(self._chat_id, outcome.operations, new_anchor)
        except Exception as exc:
            return (
                f"Could not save facts ({exc}). "
                "The conversation and previous memory were not changed."
            )

        load_facts = getattr(self._store, "load_facts", None)
        if load_facts is not None:
            try:
                self._facts = load_facts(self._chat_id)
            except Exception:
                # The facts were saved; a reload failure must not fail the turn.
                pass
        self._facts_anchor = new_anchor
        return None

    def _run_facts_update(self, active_facts, pending_messages) -> FactsOutcome:
        """Run one or two facts-extraction attempts and return their outcome.

        A single retry is made only when the response is truncated
        (``length``/``max_tokens``), mirroring the summary pipeline. Every
        executed attempt is kept as a ``(stats, ok)`` pair so the caller can
        bill it into the correct bucket; provider and parse failures are
        reported through ``error`` instead of being raised.
        """
        messages = build_facts_extraction_messages(active_facts, pending_messages)
        attempts: list[tuple[TurnStats, bool]] = []
        try:
            response = self._request_facts(messages, FACTS_MAX_TOKENS)
            stats = self._attempt_stats(response)
            if stats.finish_reason in ("length", "max_tokens"):
                attempts.append((stats, False))
                response = self._request_facts(messages, FACTS_RETRY_MAX_TOKENS)
                stats = self._attempt_stats(response)
                if stats.finish_reason in ("length", "max_tokens"):
                    attempts.append((stats, False))
                    return FactsOutcome(
                        attempts=attempts,
                        error=(
                            "model response was truncated "
                            f"(finish_reason={stats.finish_reason})"
                        ),
                    )
            text = response.choices[0].message.content or ""
            try:
                operations = parse_facts_response(text)
            except ValueError:
                # The call was executed and must still be billed as a failure.
                attempts.append((stats, False))
                raise
            attempts.append((stats, True))
            return FactsOutcome(attempts=attempts, operations=operations)
        except Exception as exc:
            return FactsOutcome(attempts=attempts, error=str(exc))

    def _request_facts(self, messages, max_tokens):
        """Issue a single non-stream facts-extraction request."""
        return self._client.chat.completions.create(
            model=self._config.model,
            messages=messages,
            stream=False,
            temperature=FACTS_TEMPERATURE,
            max_tokens=max_tokens,
        )
