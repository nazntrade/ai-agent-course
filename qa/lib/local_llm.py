"""Readiness probe and process launcher of the local LLM server.

Only the stdlib is used here: the runtime must not import the application's
provider client to decide whether a local server is up. The endpoint is always a
loopback OpenAI-compatible ``/v1`` root.

A server that existed before the run is never stopped. A server the launcher
itself spawned is stopped through ``taskkill /F /T /PID``; when only an indirect
listener remains, its process start time decides whether it provably belongs to
this run, otherwise it is left running and reported as unproven.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from lib.config import is_loopback_url

KEEP_SERVER_ENV = "QA_LOCAL_LLM_KEEP_SERVER"

# Environment variables exported to the child launcher process, so an external
# launcher that reads its key from the environment uses the same key the
# readiness probe and the application use. The value is the configured
# ``api_key`` and is never logged, printed or written to a report.
LAUNCHER_API_KEY_ENV = "LOCAL_LLM_API_KEY"
LAUNCHER_QA_API_KEY_ENV = "QA_LOCAL_LLM_API_KEY"

OWNERSHIP_OWN_LIVE = "own-live"
OWNERSHIP_OWN_DETACHED = "own-detached"
OWNERSHIP_UNPROVEN = "unproven"
OWNERSHIP_NOT_STARTED = "not-started"

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_FILETIME_TO_UNIX_EPOCH = 116444736000000000
_OWNERSHIP_TOLERANCE_SECONDS = 1.0


def models_url(base_url) -> str:
    """Return the ``/v1/models`` URL of an OpenAI-compatible root."""
    return str(base_url or "").rstrip("/") + "/models"


def auth_headers(api_key) -> dict:
    """Return the ``Authorization`` header for a configured key, or ``{}``.

    A server that requires an API key answers ``401`` to an unauthenticated
    readiness request, so the configured key is sent as a bearer token. The key
    only ever travels in the request header: it is never logged, printed or put
    into a URL or a report.
    """
    key = str(api_key or "").strip()
    if not key:
        return {}
    return {"Authorization": f"Bearer {key}"}


def fetch_models(base_url, timeout: float = 2.0, api_key=None):
    """Return the model ids of a reachable server, or ``None``.

    ``None`` means "not reachable or not an OpenAI-compatible root"; an empty
    list means "reachable but no models advertised". When ``api_key`` is set the
    request is authenticated, otherwise the server would answer ``401``.
    """
    if not is_loopback_url(base_url):
        return None
    request = urllib.request.Request(
        models_url(base_url), method="GET", headers=auth_headers(api_key)
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except Exception:
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None
    return [
        str(item.get("id"))
        for item in data
        if isinstance(item, dict) and item.get("id")
    ]


def probe(base_url, timeout: float = 2.0, api_key=None):
    """Return the advertised model ids when the server answers, else ``None``."""
    return fetch_models(base_url, timeout=timeout, api_key=api_key)


def parse_netstat_listeners(text, port) -> list:
    """Parse ``netstat -ano`` output and return the listening PIDs of a port."""
    wanted = str(port)
    pids = []
    for line in str(text or "").splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[-2].upper() != "LISTENING":
            continue
        local = parts[1]
        _, _, local_port = local.rpartition(":")
        if local_port != wanted:
            continue
        try:
            pids.append(int(parts[-1]))
        except (TypeError, ValueError):
            continue
    return sorted(set(pids))


def _run_netstat() -> str:
    try:
        completed = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout or ""


def listener_pids(port, *, runner=None) -> list:
    """Return the PIDs listening on a loopback port, best-effort."""
    run = runner or _run_netstat
    return parse_netstat_listeners(run(), port)


def process_start_time(pid):
    """Return the process creation time in Unix seconds, or ``None``.

    Best-effort: on a non-Windows host or when the process cannot be queried the
    ownership of the listener stays unproven.
    """
    try:
        kernel32 = ctypes.windll.kernel32
    except (AttributeError, OSError):
        return None
    try:
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [
            ctypes.c_uint32,
            ctypes.c_int,
            ctypes.c_uint32,
        ]
        kernel32.GetProcessTimes.restype = ctypes.c_int
        kernel32.GetProcessTimes.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
        ]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    except (AttributeError, OSError):
        return None
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        creation = ctypes.c_ulonglong()
        exit_time = ctypes.c_ulonglong()
        kernel_time = ctypes.c_ulonglong()
        user_time = ctypes.c_ulonglong()
        ok = kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        )
        if not ok:
            return None
        return (creation.value - _FILETIME_TO_UNIX_EPOCH) / 1e7
    except (AttributeError, OSError):
        return None
    finally:
        try:
            kernel32.CloseHandle(handle)
        except (AttributeError, OSError):
            pass


def taskkill(pid) -> bool:
    """Force-kill a process tree through ``taskkill /F /T /PID``."""
    try:
        completed = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(int(pid))],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


class LocalLlmLauncher:
    """Start and stop the locally configured server command.

    The command comes from the untracked local configuration or from discovery;
    the launcher only reports whether the server became ready, and the caller
    decides what to do when it did not.
    """

    def __init__(
        self,
        config,
        *,
        log_path=None,
        probe_fn=probe,
        popen=None,
        kill_fn=None,
        listeners_fn=None,
        start_time_fn=None,
    ):
        self._config = config
        self._log_path = Path(log_path) if log_path is not None else None
        self._probe = probe_fn
        self._popen = popen or subprocess.Popen
        self._kill = kill_fn or taskkill
        self._listeners = listeners_fn or listener_pids
        self._start_time = start_time_fn or process_start_time
        # The readiness probe authenticates with the same key the application
        # and the child launcher use; the key is never logged.
        self._api_key = str(getattr(config, "api_key", "") or "")
        self._process = None
        self._log_handle = None
        self._started_at = None

    @property
    def process(self):
        """The spawned process, or ``None``."""
        return self._process

    @property
    def spawned(self) -> bool:
        """Whether this launcher spawned a process at all."""
        return self._process is not None

    def probe(self, base_url=None, timeout: float = 2.0):
        """Probe the endpoint with the configured ``api_key``."""
        target = base_url or str(getattr(self._config, "base_url", "") or "")
        return self._probe(target, timeout=timeout, api_key=self._api_key)

    def claim_ownership(self) -> str:
        """Classify the spawned server: live, detached, unproven or not started.

        A still-running child is ours. An exited launcher may have left a
        detached listener behind; it is only labelled ``own-detached`` when its
        start time provably belongs to this run, otherwise ``unproven``.
        """
        if self._process is None:
            return OWNERSHIP_NOT_STARTED
        if self._process.poll() is None:
            return OWNERSHIP_OWN_LIVE
        for pid in self._listeners(self._port()):
            if self._owns_listener(pid):
                return OWNERSHIP_OWN_DETACHED
            return OWNERSHIP_UNPROVEN
        return OWNERSHIP_OWN_DETACHED

    def start(self) -> bool:
        """Spawn the configured command; return whether it started at all."""
        command = str(getattr(self._config, "launch_command", "") or "").strip()
        if not command:
            return False
        cwd = str(getattr(self._config, "launch_cwd", "") or "").strip() or None
        if self._log_path is not None:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_handle = open(self._log_path, "a", encoding="utf-8")
        child_env = None
        if self._api_key:
            child_env = dict(os.environ)
            child_env[LAUNCHER_API_KEY_ENV] = self._api_key
            child_env[LAUNCHER_QA_API_KEY_ENV] = self._api_key
        try:
            self._process = self._popen(
                command,
                shell=True,
                cwd=cwd,
                env=child_env,
                stdout=self._log_handle or subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
            )
        except OSError:
            return False
        self._started_at = time.time()
        return True

    def wait_ready(self, timeout=None, interval: float = 1.0) -> bool:
        """Poll the endpoint until it answers or the timeout elapses."""
        limit = (
            timeout
            if timeout is not None
            else getattr(self._config, "readiness_timeout_seconds", 90)
        )
        deadline = time.monotonic() + max(int(limit), 1)
        while time.monotonic() < deadline:
            if self.probe() is not None:
                return True
            time.sleep(interval)
        return self.probe() is not None

    def stop(self, *, keep=False, env=None) -> dict:
        """Stop a spawned server; never touch a pre-existing one.

        Returns ``{"stopped", "left_running", "reason"}``. ``keep=True`` or
        ``QA_LOCAL_LLM_KEEP_SERVER=1`` leaves our own server running.
        """
        environment = os.environ if env is None else env
        if not keep:
            keep = str(environment.get(KEEP_SERVER_ENV) or "").strip() == "1"

        process = self._process
        ownership = self.claim_ownership()
        result = {
            "stopped": False,
            "left_running": False,
            "reason": "nothing to stop",
        }
        if process is None:
            self._close_log()
            return result
        if keep:
            self._print_stop("left running (QA_LOCAL_LLM_KEEP_SERVER=1)")
            result = {
                "stopped": False,
                "left_running": True,
                "reason": "kept running by QA_LOCAL_LLM_KEEP_SERVER",
            }
        elif ownership == OWNERSHIP_OWN_LIVE:
            result = self._stop_live(process)
        elif ownership == OWNERSHIP_OWN_DETACHED:
            result = self._stop_detached()
        elif ownership == OWNERSHIP_UNPROVEN:
            self._print_stop("left running (ownership unproven)")
            result = {
                "stopped": False,
                "left_running": True,
                "reason": "left running (ownership unproven)",
            }
        self._close_log()
        self._process = None
        return result

    def _stop_live(self, process) -> dict:
        pid = process.pid
        if self._kill(pid):
            return {
                "stopped": True,
                "left_running": False,
                "reason": f"stopped process {pid}",
            }
        try:
            process.terminate()
            process.wait(timeout=10)
        except Exception:
            return {
                "stopped": False,
                "left_running": True,
                "reason": f"could not stop process {pid}",
            }
        return {
            "stopped": True,
            "left_running": False,
            "reason": f"terminated process {pid}",
        }

    def _owns_listener(self, pid) -> bool:
        """Whether a listener process provably started with this run."""
        started = self._start_time(pid)
        return (
            started is not None
            and self._started_at is not None
            and started >= self._started_at - _OWNERSHIP_TOLERANCE_SECONDS
        )

    def _stop_detached(self) -> dict:
        port = self._port()
        stopped_any = False
        left_running = False
        for pid in self._listeners(port):
            if not self._owns_listener(pid):
                left_running = True
                continue
            if self._kill(pid):
                stopped_any = True
            else:
                left_running = True
        if stopped_any:
            return {
                "stopped": True,
                "left_running": left_running,
                "reason": "stopped the detached server process",
            }
        if left_running:
            self._print_stop("left running (ownership unproven)")
            return {
                "stopped": False,
                "left_running": True,
                "reason": "left running (ownership unproven)",
            }
        return {
            "stopped": False,
            "left_running": False,
            "reason": "no listener to stop",
        }

    def _port(self) -> int:
        try:
            split = urlsplit(str(getattr(self._config, "base_url", "") or ""))
            return split.port or 0
        except ValueError:
            return 0

    def _print_stop(self, message) -> None:
        print(f"LOCAL_LLM_STOP: {message}", flush=True)

    def _close_log(self) -> None:
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except Exception:
                pass
            self._log_handle = None
