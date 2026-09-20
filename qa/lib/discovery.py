"""Narrow auto-discovery of the owner's local LLM launcher.

This is the only module that knows machine-specific details, and it keeps the
discovered launcher path out of every reportable object: the machine-specific
command and working directory are handed to the caller through a separate
``LauncherHandoff``, while ``DiscoveryResult`` never carries an absolute path.

The discovery order is:

1. ``QA_LOCAL_LLM_*`` environment variables (already resolved ``has_local_config``);
2. an existing ``qa/local.llm.local.json``;
3. a loopback endpoint that already answers ``/v1/models``;
4. an explicit narrow launcher source, either ``QA_LOCAL_LLM_LAUNCHER`` (a direct
   launcher path) or ``QA_LOCAL_LLM_SEARCH_ROOTS`` (an opt-in list of roots).

Disks, user folders and any default roots are never scanned: without one of
those explicit sources the launcher is not searched at all. A configuration file
is auto-created only after a local model started by an explicit narrow launcher
source became live, and only when writing is allowed; a running endpoint alone
never creates it, and an existing (even broken) file is never overwritten.
"""

from __future__ import annotations

import fnmatch
import json
import os
import socket
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit

from lib import local_llm
from lib.config import DEFAULT_API_KEY, DEFAULT_LOCAL_MODEL, LocalLlmConfig
from lib.paths import RUNS_DIR

DISCOVERY_ENV = "QA_LOCAL_LLM_DISCOVERY"
LAUNCHER_ENV = "QA_LOCAL_LLM_LAUNCHER"
SEARCH_ROOTS_ENV = "QA_LOCAL_LLM_SEARCH_ROOTS"

SOURCE_ENV = "env"
SOURCE_LOCAL_CONFIG = "local-config"
SOURCE_RUNNING_ENDPOINT = "running-endpoint"
SOURCE_LAUNCHER_ENV = "launcher-env"
SOURCE_LAUNCHER_SEARCH = "launcher-search"
SOURCE_NONE = "none"
SOURCE_DISABLED = "disabled"

LAUNCHER_CONFIGURED = "configured"
LAUNCHER_AUTO = "auto-discovered"
LAUNCHER_NONE = "none"

MODEL_SOURCE_CONFIG = "config"
MODEL_SOURCE_ENDPOINT = "endpoint"
MODEL_SOURCE_DEFAULT = "default"

DEFAULT_DEADLINE_SECONDS = 5.0
DEFAULT_MAX_ENTRIES_PER_ROOT = 200
DEFAULT_MAX_FILES = 50
DEFAULT_MAX_CANDIDATES = 50

_OFF_VALUES = ("off", "0", "false", "no")

# Name patterns per tier, most specific first. ``start*.bat`` alone never
# matches: a candidate must name Qwen, llama.cpp or an llm launcher.
_TIER_PATTERNS = (
    ("start*qwen*", "*qwen*start*", "run*qwen*"),
    ("start*llama*", "*llama*serve*", "*llama-server*", "start*llm*"),
)


@dataclass
class LauncherHandoff:
    """Machine-specific launcher details kept out of ``DiscoveryResult``.

    The caller owns this object; tests may inspect it, but it is never rendered
    into a report.
    """

    launch_command: str = ""
    launch_cwd: str = ""

    @property
    def found(self) -> bool:
        return bool(self.launch_command)


@dataclass(frozen=True)
class SearchOutcome:
    """Result of one bounded launcher search.

    ``candidates`` holds absolute paths, so the outcome is an internal value and
    must not be embedded into a report.
    """

    candidates: tuple = ()
    considered: int = 0
    elapsed_ms: int = 0
    warnings: tuple = ()


@dataclass(frozen=True)
class DiscoveryResult:
    """Discoverable facts of one run, free of machine-specific paths.

    ``config`` is the sanitized effective configuration (never carrying a launch
    command); the actual launcher, when one was found, travels through
    ``LauncherHandoff``.
    """

    config: LocalLlmConfig
    source: str
    launcher: str
    endpoint_running: bool
    pending_config: dict | None
    write_allowed: bool
    candidates_considered: int
    elapsed_ms: int
    model_source: str = MODEL_SOURCE_DEFAULT
    warnings: tuple = ()

    def as_report_block(self, *, config_written, config_path) -> dict:
        """Return the safe, path-free discovery block of a report."""
        return {
            "source": self.source,
            "launcher": self.launcher,
            "config_written": config_written,
            "config_path": config_path,
            "endpoint_running_before_run": self.endpoint_running,
            "model_source": self.model_source,
            "candidates_considered": self.candidates_considered,
            "search_elapsed_ms": self.elapsed_ms,
            "warnings": list(self.warnings),
        }


def _norm(path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _dedupe(paths) -> list:
    seen = set()
    result = []
    for path in paths:
        key = _norm(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(str(path))
    return result


def explicit_launcher(env=None) -> str:
    """Return the launcher path given by ``QA_LOCAL_LLM_LAUNCHER``, or ``""``.

    The value is a direct path the user set explicitly; optional surrounding
    quotes are stripped so a quoted path with spaces still works.
    """
    environment = os.environ if env is None else env
    value = str(environment.get(LAUNCHER_ENV) or "").strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        value = value[1:-1].strip()
    return value


def search_roots(*, env=None) -> list:
    """Return the explicit search roots from ``QA_LOCAL_LLM_SEARCH_ROOTS``.

    There is no default root: without the opt-in variable the result is empty,
    which means the launcher must not be searched at all.
    """
    environment = os.environ if env is None else env
    configured = str(environment.get(SEARCH_ROOTS_ENV) or "")
    roots = [part for part in configured.split(os.pathsep) if part.strip()]
    return _dedupe(roots)


def launcher_tier(name):
    """Return the name-pattern tier of a launcher file, or ``None``."""
    lowered = str(name).lower()
    for tier, patterns in enumerate(_TIER_PATTERNS):
        if any(fnmatch.fnmatch(lowered, pattern) for pattern in patterns):
            return tier
    return None


def search_launchers(
    *,
    roots=(),
    clock=None,
    max_entries_per_root=DEFAULT_MAX_ENTRIES_PER_ROOT,
    max_files=DEFAULT_MAX_FILES,
    max_candidates=DEFAULT_MAX_CANDIDATES,
    deadline_seconds=DEFAULT_DEADLINE_SECONDS,
    scandir=None,
) -> SearchOutcome:
    """Scan only the explicitly given roots for launcher-like ``.bat``/``.cmd``.

    Only the top level of each root is scanned and no file content is read. An
    empty root list scans nothing. The scan is bounded by a deadline, a per-root
    entry limit, a total file limit and a candidate limit. Ranking is
    deterministic: tier, then ``.bat`` before ``.cmd``, then root order, then the
    lower-case file name.
    """
    timer = clock or time.monotonic
    scan = scandir or os.scandir
    started = timer()
    deadline = started + deadline_seconds
    root_list = list(roots or ())
    found = []
    considered = 0
    files_scanned = 0
    warnings: list = []
    for root_index, root in enumerate(root_list):
        if len(found) >= max_candidates:
            warnings.append("the launcher search reached its candidate limit")
            break
        if timer() >= deadline:
            warnings.append("the launcher search reached its time limit")
            break
        try:
            with scan(root) as entries:
                for position, entry in enumerate(entries):
                    if position >= max_entries_per_root:
                        break
                    if timer() >= deadline:
                        warnings.append("the launcher search reached its time limit")
                        break
                    try:
                        is_file = entry.is_file()
                    except OSError:
                        continue
                    if not is_file:
                        continue
                    name = entry.name
                    extension = os.path.splitext(name)[1].lower()
                    if extension not in (".bat", ".cmd"):
                        continue
                    files_scanned += 1
                    if files_scanned > max_files:
                        warnings.append("the launcher search reached its file limit")
                        break
                    tier = launcher_tier(name)
                    if tier is None:
                        continue
                    if len(found) >= max_candidates:
                        warnings.append("the launcher search reached its candidate limit")
                        break
                    considered += 1
                    extension_rank = 0 if extension == ".bat" else 1
                    found.append(
                        (tier, extension_rank, root_index, name.lower(), entry.path)
                    )
        except OSError:
            continue
    found.sort(key=lambda item: item[:4])
    return SearchOutcome(
        candidates=tuple(item[4] for item in found),
        considered=considered,
        elapsed_ms=int((timer() - started) * 1000),
        warnings=tuple(_dedupe(warnings)),
    )


def build_endpoint_payload(base_url, model, *, api_key=DEFAULT_API_KEY) -> dict:
    """Build the config payload for an endpoint that is already running."""
    payload = {"base_url": str(base_url)}
    if str(model or "").strip():
        payload["model"] = str(model).strip()
    payload["api_key"] = str(api_key or DEFAULT_API_KEY)
    return payload


def build_launcher_payload(
    base_url, model, launch_command, launch_cwd="", *, api_key=DEFAULT_API_KEY
) -> dict:
    """Build the config payload for a launcher that actually started the server.

    The command is quoted so a path with spaces works through ``shell=True``.
    """
    payload = build_endpoint_payload(base_url, model, api_key=api_key)
    command = str(launch_command or "").strip()
    if command and not command.startswith('"'):
        command = f'"{command}"'
    payload["launch_command"] = command
    if str(launch_cwd or "").strip():
        payload["launch_cwd"] = str(launch_cwd).strip()
    return payload


def write_local_config(path, payload, *, tmp_dir=RUNS_DIR) -> bool:
    """Create the untracked local config once; never overwrite an existing file.

    The file is written to a temporary file inside the git-ignored run directory
    and then moved into place atomically. A missing payload, an existing file
    (valid or broken) or any filesystem error returns ``False``.
    """
    if not payload:
        return False
    target = Path(path)
    if target.exists():
        return False
    temporary = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        directory = Path(tmp_dir)
        directory.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            suffix=".tmp",
            prefix="local-config-",
            dir=directory,
            delete=False,
        )
        temporary = Path(handle.name)
        with handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        if target.exists():
            return False
        os.replace(temporary, target)
        return True
    except OSError:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
        return False


def _sanitize(config) -> LocalLlmConfig:
    """Return a copy of the config without the machine-specific launcher."""
    try:
        return replace(config, launch_command="", launch_cwd="")
    except (TypeError, ValueError):
        return config


def _model_choice(config, models) -> tuple:
    configured = str(getattr(config, "model", "") or "").strip()
    if configured:
        return configured, MODEL_SOURCE_CONFIG
    first = ""
    if isinstance(models, (list, tuple)):
        for item in models:
            if item:
                first = str(item)
                break
    if first:
        return first, MODEL_SOURCE_ENDPOINT
    return DEFAULT_LOCAL_MODEL, MODEL_SOURCE_DEFAULT


def _endpoint_host_port(base_url) -> tuple:
    try:
        split = urlsplit(str(base_url or ""))
    except ValueError:
        return "", 0
    host = split.hostname or "127.0.0.1"
    try:
        port = split.port
    except ValueError:
        port = None
    return host, port or 0


def _port_in_use(host, port, timeout=0.5) -> bool:
    try:
        with socket.create_connection((host or "127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _elapsed_ms(started, timer) -> int:
    return max(int((timer() - started) * 1000), 0)


def _launcher_result(
    config,
    base_url,
    launcher_path,
    *,
    source,
    launcher,
    write_allowed,
    handoff,
    candidates,
    elapsed_ms,
    warnings=(),
) -> DiscoveryResult:
    """Fill the handoff with one explicit launcher and build a safe result."""
    directory = os.path.dirname(launcher_path)
    handoff.launch_cwd = directory
    # Quoted so a path with spaces survives ``shell=True``.
    handoff.launch_command = f'"{launcher_path}"'
    model, model_source = _model_choice(config, None)
    pending = build_endpoint_payload(base_url, model) if write_allowed else None
    return DiscoveryResult(
        config=replace(_sanitize(config), has_local_config=True),
        source=source,
        launcher=launcher,
        endpoint_running=False,
        pending_config=pending,
        write_allowed=write_allowed,
        candidates_considered=candidates,
        elapsed_ms=elapsed_ms,
        model_source=model_source,
        warnings=tuple(warnings),
    )


def discover(
    config,
    *,
    env=None,
    probe=None,
    disabled=False,
    write_allowed=True,
    launcher_handoff=None,
    clock=None,
    search=None,
    port_in_use=None,
    file_exists=None,
) -> DiscoveryResult:
    """Resolve the local configuration without writing anything machine-specific.

    The caller runs discovery only when no configuration exists yet. The result
    is always free of absolute paths; a discovered launcher is written into
    ``launcher_handoff`` and must be applied by the caller to its own config.

    The launcher is only searched when the user set an explicit narrow source
    (``QA_LOCAL_LLM_LAUNCHER`` or ``QA_LOCAL_LLM_SEARCH_ROOTS``); otherwise the
    result is ``none`` and nothing on disk is scanned.
    """
    environment = os.environ if env is None else env
    timer = clock or time.monotonic
    started = timer()
    handoff = launcher_handoff if launcher_handoff is not None else LauncherHandoff()

    if disabled or str(environment.get(DISCOVERY_ENV) or "").strip().lower() in _OFF_VALUES:
        return DiscoveryResult(
            config=_sanitize(config),
            source=SOURCE_DISABLED,
            launcher=LAUNCHER_NONE,
            endpoint_running=False,
            pending_config=None,
            write_allowed=False,
            candidates_considered=0,
            elapsed_ms=_elapsed_ms(started, timer),
            warnings=("discovery is disabled",),
        )

    if getattr(config, "has_local_config", False):
        source = (
            SOURCE_ENV
            if str(getattr(config, "source", "") or "") == "env"
            else SOURCE_LOCAL_CONFIG
        )
        launcher = (
            LAUNCHER_CONFIGURED
            if getattr(config, "can_launch", False)
            else LAUNCHER_NONE
        )
        _, model_source = _model_choice(config, None)
        return DiscoveryResult(
            config=_sanitize(config),
            source=source,
            launcher=launcher,
            endpoint_running=False,
            pending_config=None,
            write_allowed=write_allowed,
            candidates_considered=0,
            elapsed_ms=_elapsed_ms(started, timer),
            model_source=model_source,
        )

    prober = probe or local_llm.probe
    base_url = str(getattr(config, "base_url", "") or "")
    try:
        models = prober(base_url)
    except Exception:
        models = None

    if models is not None:
        # An already running endpoint is used for this run, but it is not a
        # launcher source: it never auto-creates the untracked config.
        _, model_source = _model_choice(config, models)
        return DiscoveryResult(
            config=replace(_sanitize(config), has_local_config=True),
            source=SOURCE_RUNNING_ENDPOINT,
            launcher=LAUNCHER_NONE,
            endpoint_running=True,
            pending_config=None,
            write_allowed=write_allowed,
            candidates_considered=0,
            elapsed_ms=_elapsed_ms(started, timer),
            model_source=model_source,
        )

    host, port = _endpoint_host_port(base_url)
    port_check = port_in_use or _port_in_use
    if port and port_check(host, port):
        return DiscoveryResult(
            config=_sanitize(config),
            source=SOURCE_NONE,
            launcher=LAUNCHER_NONE,
            endpoint_running=False,
            pending_config=None,
            write_allowed=write_allowed,
            candidates_considered=0,
            elapsed_ms=_elapsed_ms(started, timer),
            warnings=(
                "the local port is already in use by a process that does not "
                "answer /v1/models; no launcher was started",
            ),
        )

    chosen_launcher = explicit_launcher(environment)
    if chosen_launcher:
        checker = file_exists or os.path.isfile
        if not checker(chosen_launcher):
            return DiscoveryResult(
                config=_sanitize(config),
                source=SOURCE_NONE,
                launcher=LAUNCHER_NONE,
                endpoint_running=False,
                pending_config=None,
                write_allowed=write_allowed,
                candidates_considered=0,
                elapsed_ms=_elapsed_ms(started, timer),
                warnings=(
                    "the launcher given by QA_LOCAL_LLM_LAUNCHER does not exist",
                ),
            )
        return _launcher_result(
            config,
            base_url,
            os.path.abspath(chosen_launcher),
            source=SOURCE_LAUNCHER_ENV,
            launcher=LAUNCHER_CONFIGURED,
            write_allowed=write_allowed,
            handoff=handoff,
            candidates=1,
            elapsed_ms=_elapsed_ms(started, timer),
        )

    roots = search_roots(env=environment)
    if not roots:
        return DiscoveryResult(
            config=_sanitize(config),
            source=SOURCE_NONE,
            launcher=LAUNCHER_NONE,
            endpoint_running=False,
            pending_config=None,
            write_allowed=write_allowed,
            candidates_considered=0,
            elapsed_ms=_elapsed_ms(started, timer),
            warnings=(
                "no explicit launcher source is configured "
                "(QA_LOCAL_LLM_LAUNCHER or QA_LOCAL_LLM_SEARCH_ROOTS); "
                "the launcher was not searched",
            ),
        )

    outcome = (search or search_launchers)(roots=roots, clock=timer)
    if outcome.candidates:
        return _launcher_result(
            config,
            base_url,
            os.path.abspath(outcome.candidates[0]),
            source=SOURCE_LAUNCHER_SEARCH,
            launcher=LAUNCHER_AUTO,
            write_allowed=write_allowed,
            handoff=handoff,
            candidates=outcome.considered,
            elapsed_ms=outcome.elapsed_ms or _elapsed_ms(started, timer),
            warnings=tuple(outcome.warnings),
        )

    return DiscoveryResult(
        config=_sanitize(config),
        source=SOURCE_NONE,
        launcher=LAUNCHER_NONE,
        endpoint_running=False,
        pending_config=None,
        write_allowed=write_allowed,
        candidates_considered=outcome.considered,
        elapsed_ms=outcome.elapsed_ms or _elapsed_ms(started, timer),
        warnings=tuple(outcome.warnings),
    )


def report_block(result, *, config_written, config_path) -> dict:
    """Return the safe, path-free discovery block of a report."""
    return result.as_report_block(config_written=config_written, config_path=config_path)
