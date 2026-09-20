"""Process control of the Streamlit application under test.

The application is started with a sanitized environment: the owner's DeepSeek
key is removed, the provider points at the loopback recording proxy and the
database points into the run directory. The process is started and stopped by
the runner, so the E2E can prove a real restart on the same port and database.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from lib.paths import APP_DIR, DEFAULT_APP_PORT

# Variables the child process must never inherit from the owner's session.
BLOCKED_ENV_PREFIXES = ("DEEPSEEK",)
DB_PATH_ENV = "MEMORY_AGENT_DB_PATH"
BASE_URL_ENV = "MEMORY_AGENT_LLM_BASE_URL"
API_KEY_ENV = "MEMORY_AGENT_LLM_API_KEY"
PLACEHOLDER_API_KEY = "local-e2e"


def build_child_env(*, base_url, db_path, api_key=PLACEHOLDER_API_KEY, base_env=None) -> dict:
    """Build the child environment of one app process.

    Every ``DEEPSEEK*`` variable is dropped, so the isolated run can never call
    the owner's paid provider, and the app is pointed at the loopback proxy and
    the private database.
    """
    env = dict(os.environ if base_env is None else base_env)
    for key in list(env):
        if any(key.upper().startswith(prefix) for prefix in BLOCKED_ENV_PREFIXES):
            env.pop(key, None)
    env[BASE_URL_ENV] = str(base_url)
    env[API_KEY_ENV] = str(api_key or PLACEHOLDER_API_KEY)
    env[DB_PATH_ENV] = str(db_path)
    env["STREAMLIT_SERVER_HEADLESS"] = "true"
    env["STREAMLIT_BROWSER_GATHER_USAGE_STATS"] = "false"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def port_is_free(port, host="127.0.0.1") -> bool:
    """Whether nothing is listening on the loopback port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, int(port))) != 0


class AppProcess:
    """Start, stop and restart the Streamlit app on a private port and database."""

    def __init__(
        self,
        *,
        port=DEFAULT_APP_PORT,
        db_path,
        base_url,
        api_key=PLACEHOLDER_API_KEY,
        log_path=None,
        app_dir=APP_DIR,
        python_exe=None,
        base_env=None,
    ):
        self.port = int(port)
        self.db_path = Path(db_path)
        self.base_url = str(base_url)
        self.api_key = str(api_key or PLACEHOLDER_API_KEY)
        self.log_path = Path(log_path) if log_path is not None else None
        self.app_dir = Path(app_dir)
        self.python_exe = python_exe or sys.executable
        self.base_env = base_env
        self._process = None
        self._log_handle = None

    @property
    def app_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def health_url(self) -> str:
        return f"{self.app_url}/_stcore/health"

    @property
    def command(self) -> list:
        """The exact command line the runner starts."""
        return [
            str(self.python_exe),
            "-m",
            "streamlit",
            "run",
            "app.py",
            "--server.port",
            str(self.port),
            "--server.address",
            "127.0.0.1",
            "--server.headless",
            "true",
            "--browser.gatherUsageStats",
            "false",
            "--server.fileWatcherType",
            "none",
        ]

    def is_alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self):
        """Spawn the app process with the sanitized environment."""
        if self._process is not None:
            raise RuntimeError("the app process is already started")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_handle = open(self.log_path, "a", encoding="utf-8")
        env = build_child_env(
            base_url=self.base_url,
            db_path=self.db_path,
            api_key=self.api_key,
            base_env=self.base_env,
        )
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
                subprocess, "CREATE_NO_WINDOW", 0
            )
        self._process = subprocess.Popen(
            self.command,
            cwd=str(self.app_dir),
            env=env,
            stdout=self._log_handle or subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        return self._process

    def wait_ready(self, timeout=240, interval=0.5) -> bool:
        """Poll the Streamlit health endpoint until the app answers."""
        deadline = time.monotonic() + max(int(timeout), 1)
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                return False
            try:
                with urllib.request.urlopen(self.health_url, timeout=3) as response:
                    if response.status == 200:
                        return True
            except Exception:
                pass
            time.sleep(interval)
        return False

    def stop(self, *, wait_port=True, timeout=20) -> None:
        """Stop the process tree and wait for the port to be released."""
        process = self._process
        if process is not None and process.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    capture_output=True,
                    check=False,
                )
            try:
                process.wait(timeout=timeout)
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=timeout)
                except Exception:
                    pass
        self._process = None
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except Exception:
                pass
            self._log_handle = None
        if wait_port:
            self._wait_for_free_port(timeout=timeout)

    def restart(self, *, timeout=240) -> bool:
        """Fully restart the process on the same port and database."""
        self.stop()
        self.start()
        return self.wait_ready(timeout=timeout)

    def _wait_for_free_port(self, timeout=20, interval=0.25) -> None:
        deadline = time.monotonic() + max(int(timeout), 1)
        while time.monotonic() < deadline:
            if port_is_free(self.port):
                return
            time.sleep(interval)

    def tail_log(self, limit=4000) -> str:
        """Return the tail of the app log for the failure report."""
        if self.log_path is None or not self.log_path.exists():
            return ""
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return text[-limit:]
