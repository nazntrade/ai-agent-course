"""Cross-invocation lifecycle of the local live session.

A ``SESSION_START`` invocation may start the local model once and keep it
running; later ``LOCAL``/``AUTO`` runs join the same process without reloading
the model, and a ``SESSION_STOP`` invocation releases only the process tree this
runner owns.

Ownership is always proven from the recorded process id and its start time, so a
recycled PID is never killed: a recorded PID whose current start time does not
match is treated as an unrelated process. Every external effect (launcher,
readiness probe, listener lookup, process start time, killer, clock) is injected,
so the lifecycle is unit-testable offline without any port or process.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from lib import local_llm
from lib.paths import SESSION_STATE_PATH

SCHEMA = 1

OWNER_RUNNER = "runner"
OWNER_EXTERNAL = "external"
OWNER_UNPROVEN = "unproven"
OWNERS = (OWNER_RUNNER, OWNER_EXTERNAL, OWNER_UNPROVEN)

STAGE_STARTING = "starting"
STAGE_ACTIVE = "active"
STAGE_KEPT = "kept"
STAGE_ORPHAN_UNPROVEN = "orphan-unproven"

OWNERSHIP_OWN_LIVE = "own-live"
OWNERSHIP_OWN_DETACHED = "own-detached"
OWNERSHIP_UNPROVEN = "unproven"
OWNERSHIP_NONE = "none"

OWNERSHIP_TOLERANCE_SECONDS = 1.0
SILENCE_ATTEMPTS = 3
SILENCE_INTERVAL_SECONDS = 0.1


def port_from_base_url(base_url) -> int:
    """Return the loopback port of an endpoint URL, or ``0``."""
    try:
        return urlsplit(str(base_url or "")).port or 0
    except ValueError:
        return 0


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


@dataclass
class SessionState:
    """Persisted state of one live session."""

    schema: int = SCHEMA
    session_id: str = ""
    stage: str = STAGE_ACTIVE
    started_at: float = 0.0
    updated_at: float = 0.0
    port: int = 0
    model: str = ""
    pid: int | None = None
    pid_start_time: float | None = None
    listener_pids: list = field(default_factory=list)
    endpoint_owner: str = OWNER_RUNNER
    model_loads: int = 0
    live_runs: int = 0
    runs: list = field(default_factory=list)
    keep: bool = False

    def to_dict(self) -> dict:
        return {
            "schema": int(self.schema),
            "session_id": str(self.session_id),
            "stage": str(self.stage),
            "started_at": float(self.started_at),
            "updated_at": float(self.updated_at),
            "port": int(self.port),
            "model": str(self.model),
            "pid": self.pid,
            "pid_start_time": self.pid_start_time,
            "listener_pids": [int(pid) for pid in (self.listener_pids or [])],
            "endpoint_owner": str(self.endpoint_owner),
            "model_loads": int(self.model_loads),
            "live_runs": int(self.live_runs),
            "runs": [dict(run) for run in (self.runs or []) if isinstance(run, dict)],
            "keep": bool(self.keep),
        }

    @classmethod
    def from_dict(cls, data) -> "SessionState":
        if not isinstance(data, dict):
            raise ValueError("session state must be a JSON object")
        if _int(data.get("schema")) != SCHEMA:
            raise ValueError("unknown session-state schema")
        session_id = str(data.get("session_id") or "")
        if not session_id:
            raise ValueError("session state has no session id")
        started_at = _number(data.get("started_at"))
        if started_at is None:
            raise ValueError("session state has no start time")
        pid = data.get("pid")
        pid = int(pid) if isinstance(pid, int) and not isinstance(pid, bool) else None
        return cls(
            schema=SCHEMA,
            session_id=session_id,
            stage=str(data.get("stage") or STAGE_ACTIVE),
            started_at=started_at,
            updated_at=_number(data.get("updated_at")) or started_at,
            port=_int(data.get("port")),
            model=str(data.get("model") or ""),
            pid=pid,
            pid_start_time=_number(data.get("pid_start_time")),
            listener_pids=[
                int(item)
                for item in (data.get("listener_pids") or [])
                if isinstance(item, int) and not isinstance(item, bool)
            ],
            endpoint_owner=str(data.get("endpoint_owner") or OWNER_UNPROVEN),
            model_loads=_int(data.get("model_loads")),
            live_runs=_int(data.get("live_runs")),
            runs=[dict(run) for run in (data.get("runs") or []) if isinstance(run, dict)],
            keep=bool(data.get("keep")),
        )


def _resolve(path):
    return Path(path) if path is not None else Path(SESSION_STATE_PATH)


def read_state(path=None):
    """Read the session state; a missing or broken file yields ``None``."""
    target = _resolve(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    try:
        return SessionState.from_dict(data)
    except (TypeError, ValueError, KeyError):
        return None


def write_state(state, path=None) -> None:
    """Atomically write the session state (temporary file + ``os.replace``)."""
    target = _resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n"
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        suffix=".tmp",
        prefix="live-session-",
        dir=target.parent,
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
        os.replace(temporary, target)
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def clear_state(path=None) -> None:
    """Remove the session state file; a missing file is not an error."""
    try:
        _resolve(path).unlink()
    except FileNotFoundError:
        return
    except OSError:
        return


def _listener_is_ours(pid, state, start_time) -> bool:
    if state is None or state.started_at is None:
        return False
    started = start_time(pid)
    return (
        started is not None
        and started >= float(state.started_at) - OWNERSHIP_TOLERANCE_SECONDS
    )


def _proven_listeners(state, *, listeners_fn=None, start_time_fn=None) -> list:
    start_time = start_time_fn or local_llm.process_start_time
    listeners = listeners_fn or local_llm.listener_pids
    candidates = list(state.listener_pids or [])
    try:
        candidates += list(listeners(state.port) or [])
    except Exception:
        pass
    proven = []
    seen = set()
    for pid in candidates:
        if pid in seen:
            continue
        seen.add(pid)
        if _listener_is_ours(pid, state, start_time):
            proven.append(pid)
    return proven


def prove_ownership(state, *, listeners_fn=None, start_time_fn=None) -> str:
    """Classify who owns the session endpoint.

    ``own-live`` when the recorded PID is alive and its start time matches (the
    PID-reuse guard); ``own-detached`` when a listener provably started with the
    session; ``unproven`` when a listener exists but cannot be attributed;
    ``none`` when nothing is left.
    """
    if state is None:
        return OWNERSHIP_NONE
    start_time = start_time_fn or local_llm.process_start_time
    listeners = listeners_fn or local_llm.listener_pids
    pid = state.pid
    record_start = state.pid_start_time
    if pid is not None and record_start is not None:
        actual = start_time(pid)
        if (
            actual is not None
            and abs(actual - record_start) <= OWNERSHIP_TOLERANCE_SECONDS
        ):
            return OWNERSHIP_OWN_LIVE
    try:
        current = list(listeners(state.port) or [])
    except Exception:
        current = []
    candidates = list(state.listener_pids or []) + current
    if not candidates:
        return OWNERSHIP_NONE
    if any(_listener_is_ours(item, state, start_time) for item in candidates):
        return OWNERSHIP_OWN_DETACHED
    return OWNERSHIP_UNPROVEN


def _new_session_id(now, session_id_fn=None) -> str:
    if session_id_fn is not None:
        return str(session_id_fn())
    return f"{int(now())}-{os.getpid()}"


@dataclass
class SessionStartResult:
    """Outcome of a ``SESSION_START`` invocation."""

    state: "SessionState | None" = None
    exit_code: int = 0
    started: bool = False
    adopted: bool = False
    external: bool = False
    reason: str = ""


def start_session(
    *,
    path=None,
    state=None,
    base_url="",
    port=0,
    model="",
    launcher,
    probe,
    listeners_fn=None,
    start_time_fn=None,
    now_fn=None,
    session_id_fn=None,
) -> SessionStartResult:
    """Start the local model once, or adopt an endpoint that already answers.

    The endpoint is probed first: a model started by this invocation is kept
    running (``launcher.stop(keep=True)``), so later runs join it without a
    second load. A silent endpoint with a provably live runner-owned PID is a
    hard prerequisite error instead of a second model on the same port.
    """
    listeners = listeners_fn or local_llm.listener_pids
    start_time = start_time_fn or local_llm.process_start_time
    now = now_fn or time.time
    if state is None:
        state = read_state(path)

    if probe() is not None:
        if state is None:
            new = SessionState(
                session_id=_new_session_id(now, session_id_fn),
                stage=STAGE_ACTIVE,
                started_at=now(),
                updated_at=now(),
                port=_int(port),
                model=str(model or ""),
                listener_pids=list(listeners(port) or []),
                endpoint_owner=OWNER_EXTERNAL,
                model_loads=0,
            )
            write_state(new, path)
            return SessionStartResult(
                state=new,
                external=True,
                reason="the endpoint was already running; ownership was not claimed",
            )
        if state.endpoint_owner == OWNER_EXTERNAL:
            state.stage = STAGE_ACTIVE
            state.updated_at = now()
            state.listener_pids = list(listeners(state.port or port) or [])
            write_state(state, path)
            return SessionStartResult(
                state=state,
                external=True,
                reason="the endpoint belongs to an external process",
            )
        ownership = prove_ownership(
            state, listeners_fn=listeners_fn, start_time_fn=start_time_fn
        )
        state.stage = STAGE_ACTIVE
        state.updated_at = now()
        if ownership in (OWNERSHIP_OWN_LIVE, OWNERSHIP_OWN_DETACHED):
            state.listener_pids = list(listeners(state.port or port) or [])
            write_state(state, path)
            return SessionStartResult(
                state=state,
                adopted=True,
                reason="adopted the runner-owned live session",
            )
        state.endpoint_owner = OWNER_UNPROVEN
        write_state(state, path)
        return SessionStartResult(
            state=state,
            reason="the endpoint answers but its ownership is not provable",
        )

    # The endpoint is silent.
    if state is not None:
        ownership = prove_ownership(
            state, listeners_fn=listeners_fn, start_time_fn=start_time_fn
        )
        if ownership != OWNERSHIP_NONE:
            return SessionStartResult(
                state=state,
                exit_code=2,
                reason=(
                    "a runner-owned process is alive or the session port is "
                    "still occupied but the endpoint does not answer; run "
                    "SESSION_STOP before starting a new model"
                ),
            )
        clear_state(path)
        state = None

    starting = SessionState(
        session_id=_new_session_id(now, session_id_fn),
        stage=STAGE_STARTING,
        started_at=now(),
        updated_at=now(),
        port=_int(port),
        model=str(model or ""),
        endpoint_owner=OWNER_RUNNER,
        model_loads=0,
    )
    write_state(starting, path)
    try:
        ready = bool(launcher.start()) and bool(launcher.wait_ready())
    except Exception:
        ready = False
    if not ready:
        try:
            launcher.stop()
        except Exception:
            pass
        clear_state(path)
        return SessionStartResult(
            exit_code=2,
            reason="the local model did not become ready",
        )

    process = getattr(launcher, "process", None)
    pid = getattr(process, "pid", None)
    pid_start = start_time(pid) if pid is not None else None
    active = SessionState(
        session_id=starting.session_id,
        stage=STAGE_ACTIVE,
        started_at=starting.started_at,
        updated_at=now(),
        port=_int(port),
        model=str(model or ""),
        pid=pid,
        pid_start_time=pid_start,
        listener_pids=list(listeners(port) or []),
        endpoint_owner=OWNER_RUNNER,
        model_loads=1,
    )
    write_state(active, path)
    try:
        launcher.stop(keep=True)
    except Exception:
        pass
    return SessionStartResult(
        state=active,
        started=True,
        reason="started and kept the local model for the session",
    )


@dataclass
class SessionStopResult:
    """Outcome of a ``SESSION_STOP`` invocation."""

    exit_code: int = 0
    stopped: bool = False
    left_running: bool = False
    reason: str = ""
    state: "SessionState | None" = None


def _wait_silent(probe, port) -> bool:
    if probe is None:
        return True
    for _ in range(SILENCE_ATTEMPTS):
        try:
            if not probe(port):
                return True
        except Exception:
            return True
        time.sleep(SILENCE_INTERVAL_SECONDS)
    return False


def stop_session(
    *,
    path=None,
    state=None,
    keep=False,
    env=None,
    listeners_fn=None,
    start_time_fn=None,
    kill_fn=None,
    probe=None,
    now_fn=None,
) -> SessionStopResult:
    """Release the process tree this runner owns; never touch a foreign one.

    A missing or broken state is idempotent success. An external endpoint is left
    running and its state is removed. ``keep`` (or
    ``QA_LOCAL_LLM_KEEP_SERVER=1``) leaves the model running and records the
    ``kept`` stage. Unproven ownership is never killed.
    """
    kill = kill_fn or local_llm.taskkill
    now = now_fn or time.time
    environment = os.environ if env is None else env
    try:
        if state is None:
            state = read_state(path)
        if state is None:
            return SessionStopResult(
                exit_code=0,
                stopped=False,
                left_running=False,
                reason="no live session state",
            )
        if not keep:
            keep = (
                str(environment.get(local_llm.KEEP_SERVER_ENV) or "").strip() == "1"
            )

        if state.endpoint_owner == OWNER_EXTERNAL:
            clear_state(path)
            return SessionStopResult(
                exit_code=0,
                stopped=False,
                left_running=True,
                reason="the endpoint belongs to an external process and is left running",
                state=state,
            )
        if keep:
            state.stage = STAGE_KEPT
            state.keep = True
            state.updated_at = now()
            write_state(state, path)
            return SessionStopResult(
                exit_code=0,
                stopped=False,
                left_running=True,
                reason="kept running by request",
                state=state,
            )

        ownership = prove_ownership(
            state, listeners_fn=listeners_fn, start_time_fn=start_time_fn
        )
        if ownership == OWNERSHIP_UNPROVEN:
            state.stage = STAGE_ORPHAN_UNPROVEN
            state.updated_at = now()
            write_state(state, path)
            return SessionStopResult(
                exit_code=0,
                stopped=False,
                left_running=True,
                reason="ownership is unproven; the process was left running",
                state=state,
            )
        if ownership == OWNERSHIP_NONE:
            clear_state(path)
            return SessionStopResult(
                exit_code=0,
                stopped=True,
                left_running=False,
                reason="no owned process was found",
                state=state,
            )

        if ownership == OWNERSHIP_OWN_LIVE and state.pid is not None:
            kill(state.pid)
        for pid in _proven_listeners(
            state, listeners_fn=listeners_fn, start_time_fn=start_time_fn
        ):
            if pid != state.pid:
                kill(pid)
        silent = _wait_silent(probe, state.port)
        clear_state(path)
        return SessionStopResult(
            exit_code=0,
            stopped=True,
            left_running=not silent,
            reason=(
                "stopped the runner-owned process tree"
                if ownership == OWNERSHIP_OWN_LIVE
                else "stopped the proven detached listener(s)"
            ),
            state=state,
        )
    finally:
        pass


def begin_run(state, mode, *, path=None, now_fn=None):
    """Count a live run that joins the session and record its start."""
    if state is None:
        return None
    now = now_fn or time.time
    state.live_runs = _int(state.live_runs) + 1
    runs = list(state.runs or [])
    runs.append({"mode": str(mode or "").upper(), "status": "running", "at": now()})
    state.runs = runs
    state.updated_at = now()
    if path is not None:
        write_state(state, path)
    return state


def finish_run(state, mode, status, *, path=None, now_fn=None):
    """Record the final status of the last joined run (best effort)."""
    if state is None:
        return None
    now = now_fn or time.time
    wanted = str(mode or "").upper()
    runs = list(state.runs or [])
    for run in reversed(runs):
        if run.get("mode") == wanted and run.get("status") == "running":
            run["status"] = str(status)
            run["at"] = now()
            break
    else:
        runs.append({"mode": wanted, "status": str(status), "at": now()})
    state.runs = runs
    state.updated_at = now()
    if path is not None:
        write_state(state, path)
    return state


def _started_at_label(state):
    if state is None or not state.started_at:
        return None
    try:
        return datetime.fromtimestamp(float(state.started_at)).isoformat(
            timespec="seconds"
        )
    except (OverflowError, OSError, ValueError):
        return str(state.started_at)


def report_block(
    state,
    *,
    started=False,
    stopped=False,
    adopted=False,
    left_running=False,
    mode=None,
) -> dict:
    """Return the safe, path-free session block of a report.

    The block never carries a path, a command, an API key or a full base URL.
    """
    if mode is None:
        mode = "session" if state is not None else "none"
    return {
        "mode": str(mode),
        "session_id": state.session_id if state is not None else None,
        "session_started": bool(started),
        "session_stopped": bool(stopped),
        "adopted": bool(adopted),
        "endpoint_owner": state.endpoint_owner if state is not None else None,
        "model_loads": _int(state.model_loads) if state is not None else 0,
        "live_runs": _int(state.live_runs) if state is not None else 0,
        "left_running": bool(left_running),
        "stage": state.stage if state is not None else None,
        "started_at": _started_at_label(state),
    }
