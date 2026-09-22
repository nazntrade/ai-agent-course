"""Lifecycle of the harness-owned processes.

The harness starts the MCP server, the backend and an optional model stub as
child processes. It waits for readiness, captures their output into the run
directory and stops only its own process trees (``taskkill /F /T /PID`` of the
PID it started). A foreign process that owns a needed port is never touched:
that port is reported as a prerequisite error instead.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from harness.qa_bridge import qa_local_llm, qa_port_is_free

# Environment keys kept from the parent process. Everything else (in particular
# an unrelated provider key) is dropped before a child starts.
KEEP_ENV_KEYS = (
    "PATH",
    "PATHEXT",
    "SystemRoot",
    "SystemDrive",
    "ComSpec",
    "WINDIR",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "OS",
)

DEFAULT_READY_TIMEOUT_SECONDS = 60.0


class PrerequisiteError(RuntimeError):
    """A required port or process precondition is not met."""


def sanitized_env(extra: dict | None = None) -> dict:
    """Build a clean child environment from an explicit allow-list."""
    env = {key: os.environ[key] for key in KEEP_ENV_KEYS if key in os.environ}
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    for key, value in (extra or {}).items():
        if value is None:
            continue
        env[str(key)] = str(value)
    return env


def require_free_ports(ports, host: str = "127.0.0.1") -> list:
    """Fail early when any needed loopback port is occupied."""
    busy = [int(port) for port in ports if not qa_port_is_free(int(port), host)]
    if busy:
        raise PrerequisiteError(
            "port(s) already in use: "
            + ", ".join(str(port) for port in busy)
            + "; stop the owning process and try again"
        )
    return [int(port) for port in ports]


def wait_tcp(host: str, port: int, timeout=DEFAULT_READY_TIMEOUT_SECONDS, process=None) -> bool:
    """Wait until a TCP port accepts a connection."""
    deadline = time.monotonic() + max(float(timeout), 1.0)
    while time.monotonic() < deadline:
        if process is not None and not process.alive():
            return False
        if not qa_port_is_free(int(port), host):
            return True
        time.sleep(0.2)
    return not qa_port_is_free(int(port), host)


def http_ok(url: str, timeout: float = 3.0) -> bool:
    """Whether an HTTP GET returns 200."""
    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return int(getattr(response, "status", 200)) == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def wait_http(url: str, timeout=DEFAULT_READY_TIMEOUT_SECONDS, process=None) -> bool:
    """Wait until an HTTP endpoint answers 200."""
    deadline = time.monotonic() + max(float(timeout), 1.0)
    while time.monotonic() < deadline:
        if process is not None and not process.alive():
            return False
        if http_ok(url):
            return True
        time.sleep(0.25)
    return http_ok(url)


@dataclass
class ManagedProcess:
    """One child process started and stopped by the harness."""

    name: str
    args: list
    cwd: Path
    env: dict
    log_path: Path = None
    _process: object = field(default=None, init=False, repr=False)
    _handle: object = field(default=None, init=False, repr=False)

    @property
    def pid(self):
        return getattr(self._process, "pid", None)

    def alive(self) -> bool:
        """Whether the child is still running."""
        return self._process is not None and self._process.poll() is None

    def start(self) -> "ManagedProcess":
        """Spawn the child with captured output."""
        if self.log_path is not None:
            Path(self.log_path).parent.mkdir(parents=True, exist_ok=True)
            self._handle = open(self.log_path, "a", encoding="utf-8", errors="replace")
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
                subprocess, "CREATE_NO_WINDOW", 0
            )
        self._process = subprocess.Popen(
            [str(item) for item in self.args],
            cwd=str(self.cwd),
            env=self.env,
            stdout=self._handle or subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            shell=False,
        )
        return self

    def tail_log(self, limit: int = 4000) -> str:
        """Return the tail of the captured log."""
        if self.log_path is None or not Path(self.log_path).exists():
            return ""
        try:
            text = Path(self.log_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return text[-limit:]

    def stop(self, *, timeout: float = 20.0) -> dict:
        """Stop only this process tree; never a process we did not start."""
        result = {"name": self.name, "pid": self.pid, "stopped": False}
        process = self._process
        if process is None:
            return result
        if process.poll() is None:
            qa_local_llm.taskkill(process.pid)
            try:
                process.wait(timeout=timeout)
            except Exception:  # noqa: BLE001 - best effort
                try:
                    process.kill()
                    process.wait(timeout=timeout)
                except Exception:  # noqa: BLE001 - best effort
                    pass
        result["stopped"] = process.poll() is not None
        self._process = None
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None
        return result


def python_module(module: str, *args) -> list:
    """Build a ``python -m module`` command for the current interpreter."""
    return [sys.executable, "-m", str(module), *[str(arg) for arg in args]]
