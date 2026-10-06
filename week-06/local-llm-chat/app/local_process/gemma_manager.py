"""Local Gemma process ownership (SPEC 5.2, R4, R6.1b).

A single owner of the ``llama-server.exe`` process. It:
- starts the process only when not already running (idempotent),
- declares ``ready`` only after the server itself confirms availability,
- stops only the PID it started,
- refuses to touch foreign processes and never mass-kills by image name.
"""

from __future__ import annotations

import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from ..errors import LocalStartTimeout, PortInUse, RuntimeMissing

STATE_UNLOADED = "unloaded"
STATE_LOADING = "loading"
STATE_READY = "ready"
STATE_GENERATING = "generating"
STATE_UNLOADING = "unloading"
STATE_ERROR = "error"


class GemmaProcessManager:
    """Owns one llama-server process on a loopback port."""

    def __init__(
        self,
        *,
        runtime_path: str,
        gguf_path: str,
        model_id: str,
        host: str = "127.0.0.1",
        port: int = 8791,
        context_tokens: int = 8192,
        load_timeout: float = 300.0,
        extra_args: str = "",
        popen=None,
        ready_probe=None,
    ) -> None:
        self.runtime_path = runtime_path
        self.gguf_path = gguf_path
        self.model_id = model_id
        self.host = host
        self.port = port
        self.context_tokens = context_tokens
        self.load_timeout = load_timeout
        self.extra_args = extra_args
        self._popen = popen or subprocess.Popen
        self._ready_probe = ready_probe or self._probe_ready
        self._process = None
        self._state = STATE_UNLOADED
        self._error: str | None = None
        self._lock = threading.RLock()

    # -- state ------------------------------------------------------------
    @property
    def state(self) -> str:
        with self._lock:
            if self._state in (STATE_READY, STATE_GENERATING) and not self._is_alive():
                # The process died behind our back: report it honestly.
                self._state = STATE_ERROR
                self._error = "the local server process is no longer running"
            return self._state

    def state_snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self.state,
                "pid": self._process.pid if self._process is not None and self._process.poll() is None else None,
                "port": self.port,
                "model": self.model_id,
                "error": self._error,
            }

    def set_generating(self, value: bool) -> None:
        with self._lock:
            if value and self._state == STATE_READY:
                self._state = STATE_GENERATING
            elif not value and self._state == STATE_GENERATING:
                self._state = STATE_READY

    # -- readiness --------------------------------------------------------
    def port_is_busy(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(1.0)
            return probe.connect_ex((self.host, self.port)) == 0

    def _probe_ready(self) -> bool:
        url = f"http://{self.host}:{self.port}/v1/models"
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                return response.status == 200
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def _is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    # -- lifecycle --------------------------------------------------------
    def ensure_started(self) -> dict:
        """Idempotent start; returns the state snapshot."""
        with self._lock:
            if self._state in (STATE_READY, STATE_GENERATING) and self._is_alive():
                return self.state_snapshot()
            if self._state == STATE_LOADING and self._is_alive():
                # Second call while loading must not spawn another process.
                return self.state_snapshot()
            return self._start_locked()

    def _start_locked(self) -> dict:
        runtime = Path(self.runtime_path)
        gguf = Path(self.gguf_path)
        if not runtime.is_file() or not gguf.is_file():
            self._state = STATE_ERROR
            self._error = "runtime or model file is missing"
            raise RuntimeMissing(
                "The local runtime or model file is missing.",
                details={"runtime": runtime.name, "model": gguf.name},
            )
        # A busy port means a foreign/unknown process we must never stop.
        if self.port_is_busy():
            self._state = STATE_ERROR
            self._error = f"loopback port {self.port} is already in use"
            raise PortInUse(
                "The local server port is already in use; a foreign process will not be stopped.",
                details={"port": self.port},
            )

        self._state = STATE_LOADING
        self._error = None
        args = [
            str(runtime), "-m", str(gguf),
            "--alias", self.model_id,
            "--host", self.host, "--port", str(self.port),
            "-c", str(self.context_tokens),
        ]
        if self.extra_args.strip():
            args.extend(self.extra_args.split())
        try:
            self._process = self._popen(
                args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=str(runtime.parent),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            self._state = STATE_ERROR
            self._error = "the local runtime failed to start"
            raise RuntimeMissing("The local runtime failed to start.") from None

        if not self._wait_ready():
            reason = "process exited during startup" if self._process.poll() is not None else "readiness timeout"
            self.stop()
            self._state = STATE_ERROR
            self._error = reason
            raise LocalStartTimeout(f"The local server did not become ready ({reason}).")
        self._state = STATE_READY
        return self.state_snapshot()

    def _wait_ready(self) -> bool:
        deadline = time.monotonic() + self.load_timeout
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                return False
            if self._ready_probe():
                return True
            time.sleep(0.5)
        return False

    def stop(self) -> dict:
        """Stop only the process this manager started (by descriptor)."""
        with self._lock:
            return self._stop_locked()

    def _stop_locked(self) -> dict:
        if self._process is None or self._process.poll() is not None:
            self._process = None
            self._state = STATE_UNLOADED
            return self.state_snapshot()
        self._state = STATE_UNLOADING
        process = self._process
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=15)
        self._process = None
        self._state = STATE_UNLOADED
        self._error = None
        return self.state_snapshot()

    def unload(self) -> dict:
        return self.stop()

    def shutdown(self) -> None:
        try:
            self.stop()
        except Exception:  # noqa: BLE001 - shutdown must never mask the original error
            pass
