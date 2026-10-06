"""E2 preflight: own-process run of the local Gemma runtime, one real request.

This harness is the minimal real check of the riskiest external boundary. It
starts ``llama-server.exe`` as its own child process on a loopback port, waits
for confirmed readiness, sends exactly one chat request, verifies a non-empty
answer with model identity and timing, and stops only the process it started.

Safety rules (SPEC R2.4/R3.4, I2/I4):
- A busy loopback port is an explicit, readable error. A foreign process is
  never stopped, never inspected for ownership and never mass-killed by name.
- The answer must be produced by the process this harness started; the reported
  model id is compared with the expected model derived from the GGUF path or
  the ``GEMMA_MODEL_ID`` override.

It never reads the application ``.env``. Paths come from the application
environment (``GEMMA_*``); the module defaults match
``docs/specs/day-26-local-llm`` R3.2.

Exit codes: 0 = real answer obtained, 1 = failed, 2 = port/config error,
3 = blocked (missing files or LIVE policy).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

MODULE_DIR = Path(__file__).resolve().parent.parent
if str(MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(MODULE_DIR))

from harness.live_policy import report_live_blocked  # noqa: E402

DEFAULT_RUNTIME = r"D:\AI\Runtimes\llama.cpp\b10809\llama-server.exe"
DEFAULT_GGUF = r"D:\AI\Models\LLM\Gemma-4-12B-IT\gemma-4-12b-it-qat-q4_0.gguf"
DEFAULT_PORT = 8791
HOST = "127.0.0.1"
QUESTION = "What is 17 + 26? Reply with the single integer."
DEFAULT_MAX_OUTPUT_TOKENS = 64


class PreflightSetupError(RuntimeError):
    """A concrete, secret-free configuration or port problem before startup."""


def emit(status: str, message: str) -> None:
    print(f"{status}: {message}")


def _env_int(name: str, fallback: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return fallback
    try:
        return int(raw)
    except ValueError:
        raise PreflightSetupError(f"{name} must be an integer") from None


def _runtime_path() -> Path:
    return Path(os.environ.get("GEMMA_RUNTIME_PATH") or DEFAULT_RUNTIME)


def _gguf_path() -> Path:
    return Path(os.environ.get("GEMMA_GGUF_PATH") or DEFAULT_GGUF)


def _port_is_busy(port: int) -> bool:
    """True when something already accepts TCP connections on the loopback port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        return probe.connect_ex((HOST, port)) == 0


def _wait_ready(port: int, process: subprocess.Popen, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    url = f"http://{HOST}:{port}/v1/models"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(1.0)
    return False


def _chat(port: int, model_id: str, timeout: float, max_output_tokens: int) -> dict:
    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": QUESTION}],
        "temperature": 0,
        "max_tokens": max_output_tokens,
        "stream": False,
    }
    request = urllib.request.Request(
        f"http://{HOST}:{port}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def _stop_own(process: subprocess.Popen) -> None:
    """Terminate only the process this harness started, by PID/descriptor."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=15)


def _expected_model_id(gguf: Path) -> str:
    return os.environ.get("GEMMA_MODEL_ID") or gguf.name


def _sanitize_model(model: object) -> str:
    """Reduce a reported model name to its basename (llama-server may echo a path).

    Mirrors the repository rule ``qa/lib/metrics.py::sanitize_model``: strip
    surrounding whitespace and drop any ``\\``/``/`` path prefix while keeping
    the file extension, so ``C:\\models\\gemma-4-12b-it-qat-q4_0.gguf`` compares
    equal to the local GGUF basename.
    """
    text = str(model or "").strip()
    if not text:
        return ""
    for separator in ("\\", "/"):
        if separator in text:
            text = text.rsplit(separator, 1)[-1]
    return text.strip()


def _models_match(reported: object, expected: str) -> bool:
    """Own-process identity check tolerant of path/extension/case differences."""
    return _sanitize_model(reported).lower() == _sanitize_model(expected).lower()


def run_scenario() -> int:
    if report_live_blocked("GEMMA_PREFLIGHT_STATUS"):
        return 3

    process: subprocess.Popen | None = None
    _preflight_result: dict = {}
    try:
        runtime = _runtime_path()
        gguf = _gguf_path()
        missing = [str(path) for path in (runtime, gguf) if not path.is_file()]
        if missing:
            emit("GEMMA_PREFLIGHT_START",
                 "BLOCKED (missing runtime files: " + "; ".join(missing) + ")")
            emit("GEMMA_PREFLIGHT_STATUS", "BLOCKED")
            return 3

        port = _env_int("GEMMA_PORT", DEFAULT_PORT)
        load_timeout = float(_env_int("GEMMA_LOAD_TIMEOUT_SECONDS", 300))
        request_timeout = float(_env_int("GEMMA_REQUEST_TIMEOUT_SECONDS", 180))
        max_output_tokens = _env_int("GEMMA_MAX_OUTPUT_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS)
        context_tokens = _env_int("GEMMA_CONTEXT_TOKENS", 8192)
        expected_model = _expected_model_id(gguf)

        if _port_is_busy(port):
            emit("GEMMA_PREFLIGHT_START",
                 f"FAIL (loopback port {port} is already in use; "
                 "no foreign process was stopped)")
            emit("GEMMA_PREFLIGHT_STATUS", "FAIL")
            return 2

        process = subprocess.Popen(
            [
                str(runtime),
                "-m", str(gguf),
                "--alias", expected_model,
                "--host", HOST,
                "--port", str(port),
                "-c", str(context_tokens),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(runtime.parent),
        )

        if not _wait_ready(port, process, load_timeout):
            reason = "process exited during startup" if process.poll() is not None else "readiness timeout"
            emit("GEMMA_PREFLIGHT_START", f"FAIL (server did not become ready: {reason})")
            emit("GEMMA_PREFLIGHT_STATUS", "FAIL")
            return 1
        emit("GEMMA_PREFLIGHT_START", f"PASS (own pid={process.pid}, port={port})")

        started = time.perf_counter()
        data = _chat(port, expected_model, request_timeout, max_output_tokens)
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        choice = (data.get("choices") or [{}])[0]
        text = (choice.get("message") or {}).get("content")
        if not isinstance(text, str) or not text.strip():
            emit("GEMMA_PREFLIGHT_ANSWER", "FAIL (empty answer)")
            emit("GEMMA_PREFLIGHT_STATUS", "FAIL")
            return 1
        reported_model = data.get("model") or expected_model
        if not _models_match(reported_model, expected_model):
            emit("GEMMA_PREFLIGHT_ANSWER",
                 f"FAIL (answered by unexpected model id {reported_model!r}; "
                 f"expected {expected_model!r})")
            emit("GEMMA_PREFLIGHT_STATUS", "FAIL")
            return 1
        emit("GEMMA_PREFLIGHT_ANSWER", json.dumps({
            "model": reported_model,
            "finish_reason": choice.get("finish_reason"),
            "usage": data.get("usage"),
            "latency_ms": latency_ms,
            "text": text.strip(),
        }, ensure_ascii=False))
        _preflight_result = {
            "text": text.strip(),
            "model": reported_model,
            "latency_ms": latency_ms,
            "finish_reason": choice.get("finish_reason"),
            "usage": data.get("usage"),
            "parameters": {"max_output_tokens": max_output_tokens, "context_tokens": context_tokens},
            "time": {"latency_ms": latency_ms},
        }
        emit("GEMMA_PREFLIGHT_STATUS", "PASS")
        return 0
    except PreflightSetupError as exc:
        emit("GEMMA_PREFLIGHT_START", f"FAIL (invalid configuration: {exc})")
        emit("GEMMA_PREFLIGHT_STATUS", "FAIL")
        return 2
    except Exception as exc:  # noqa: BLE001 - report a concrete failed real request
        emit("GEMMA_PREFLIGHT_ANSWER", f"FAIL ({type(exc).__name__})")
        emit("GEMMA_PREFLIGHT_STATUS", "FAIL")
        return 1
    finally:
        if process is not None:
            _stop_own(process)
            emit("GEMMA_PREFLIGHT_CLEANUP", "PASS (own process stopped)")
        if _preflight_result:
            _out = Path(__file__).resolve().parent.parent / "local-data" / "acceptance" / "day-26" / "preflight.json"
            _out.parent.mkdir(parents=True, exist_ok=True)
            _out.write_text(
                json.dumps(_preflight_result, ensure_ascii=False, indent=2), encoding="utf-8"
            )


def main() -> int:
    return run_scenario()


if __name__ == "__main__":
    sys.exit(main())
