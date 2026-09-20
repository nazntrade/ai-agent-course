"""Provider resolution of the four E2E modes.

``AUTO`` prefers a live local model and falls back to the loopback mock only
when no local runtime is available; ``LOCAL`` requires the local model and never
falls back; ``MOCK`` never touches a real server; ``NETWORK`` is an explicit
opt-in that this task refuses before any network access happens.

The module is pure decision logic: the probe and the launch are injected, so the
resolved plan is testable without any network or process.
"""

from __future__ import annotations

from dataclasses import dataclass

from lib.config import is_loopback_url

MODE_AUTO = "AUTO"
MODE_LOCAL = "LOCAL"
MODE_MOCK = "MOCK"
MODE_NETWORK = "NETWORK"
# Lifecycle commands of the cross-invocation live session. ``SESSION_START``
# starts the local model once and keeps it running; ``SESSION_STOP`` releases the
# process tree this runner owns.
MODE_SESSION_START = "SESSION_START"
MODE_SESSION_STOP = "SESSION_STOP"
MODES = (
    MODE_AUTO,
    MODE_LOCAL,
    MODE_MOCK,
    MODE_NETWORK,
    MODE_SESSION_START,
    MODE_SESSION_STOP,
)

KIND_LOCAL = "local"
KIND_MOCK = "mock"

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_PREREQUISITE = 2
EXIT_NETWORK_REFUSED = 3


class PrerequisiteError(RuntimeError):
    """A required local prerequisite (configuration, server, port) is missing."""


class NetworkRefused(RuntimeError):
    """The explicit network mode was requested and refused before any call."""


@dataclass(frozen=True)
class ProviderPlan:
    """The provider the run will use and why.

    ``kind`` is ``local`` or ``mock``. ``base_url`` is the loopback upstream the
    recording proxy forwards to. ``live_local_llm`` is the value reported as
    ``LIVE_LOCAL_LLM``; a mock run never claims a real local model.
    """

    mode: str
    kind: str
    base_url: str = ""
    live_local_llm: bool = False
    reason: str = ""
    spawned: bool = False
    model: str = ""


def normalize_mode(mode) -> str:
    """Normalize a user-supplied mode name; unknown names are rejected."""
    normalized = str(mode or MODE_AUTO).strip().upper()
    if normalized not in MODES:
        raise PrerequisiteError(
            f"Unknown mode {mode!r}; expected one of {', '.join(MODES)}"
        )
    return normalized


def is_session_mode(mode) -> bool:
    """Whether a mode name is one of the live-session lifecycle commands."""
    return str(mode or "").strip().upper() in (MODE_SESSION_START, MODE_SESSION_STOP)


def _first_model(models) -> str:
    if isinstance(models, (list, tuple)) and models:
        return str(models[0])
    return ""


def resolve_provider(
    mode,
    config,
    *,
    probe,
    launch,
    default_base_url="http://127.0.0.1:8080/v1",
) -> ProviderPlan:
    """Resolve the provider plan for one run.

    ``probe(base_url)`` returns the advertised model ids (truthy when reachable)
    or ``None``. ``launch()`` starts the configured local server and returns
    whether it became ready. Both are injected so the decision table is unit
    testable.
    """
    normalized = normalize_mode(mode)

    if normalized == MODE_NETWORK:
        raise NetworkRefused(
            "NETWORK mode is an explicit opt-in and is refused by this task; "
            "no network call was made"
        )

    if normalized == MODE_MOCK:
        return ProviderPlan(
            mode=normalized,
            kind=KIND_MOCK,
            base_url="",
            live_local_llm=False,
            reason="MOCK mode: loopback stub provider, no real local model",
        )

    base_url = str(getattr(config, "base_url", "") or default_base_url)
    if not is_loopback_url(base_url):
        raise PrerequisiteError(
            f"the configured local endpoint must be loopback, got {base_url!r}"
        )

    def local_plan(models, spawned=False, reason=""):
        model = str(getattr(config, "model", "") or "") or _first_model(models)
        return ProviderPlan(
            mode=normalized,
            kind=KIND_LOCAL,
            base_url=base_url,
            live_local_llm=True,
            reason=reason,
            spawned=spawned,
            model=model,
        )

    if normalized == MODE_LOCAL:
        if not getattr(config, "has_local_config", False):
            raise PrerequisiteError(
                "LOCAL mode requires the untracked local configuration "
                "(qa/local.llm.local.json or QA_LOCAL_LLM_* variables); "
                "discovery found neither a configuration nor a local launcher"
            )
        models = probe(base_url)
        if models is not None:
            return local_plan(models)
        if not getattr(config, "can_launch", False):
            raise PrerequisiteError(
                f"no local model is reachable at {base_url} and no launch "
                "command is configured"
            )
        if launch():
            return local_plan(probe(base_url), spawned=True)
        raise PrerequisiteError(
            f"the local model at {base_url} did not become ready"
        )

    # AUTO: prefer a live local model, never fall back to a remote provider.
    models = probe(base_url)
    if models is not None:
        return local_plan(models)
    if getattr(config, "can_launch", False) and launch():
        return local_plan(probe(base_url), spawned=True)
    if not getattr(config, "has_local_config", False):
        reason = (
            f"no local configuration and no local model at {base_url}"
        )
    else:
        reason = f"the configured local model at {base_url} is not reachable"
    return ProviderPlan(
        mode=normalized,
        kind=KIND_MOCK,
        base_url="",
        live_local_llm=False,
        reason=reason,
    )
