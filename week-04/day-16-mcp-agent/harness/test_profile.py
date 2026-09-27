"""Fail-closed model selection for local test runs only (LTP-1/LTP-2)."""

from __future__ import annotations

import hashlib
import ntpath
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

from harness.qa_bridge import qa_local_llm
from harness.processes import PrerequisiteError

MODEL_ENV_KEYS = (
    "AI_TEST_MODEL_KIND",
    "AI_TEST_MODEL_BASE_URL",
    "AI_TEST_MODEL_NAME",
    "AI_TEST_MODEL_API_KEY",
)
LOCAL_ID_ENV_KEYS = ("AI_TEST_MODEL_ID", "AI_TEST_MODEL_PATH")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
SAFE_REQUEST_ERRORS = frozenset(
    {
        "ConnectError",
        "ConnectTimeout",
        "ReadError",
        "ReadTimeout",
        "WriteError",
        "WriteTimeout",
        "PoolTimeout",
        "ProxyError",
        "RemoteProtocolError",
        "LocalProtocolError",
        "UnsupportedProtocol",
        "DecodingError",
        "TooManyRedirects",
    }
)


@dataclass(frozen=True)
class TestModelProfile:
    kind: str
    base_url: str
    name: str
    api_key: str = field(repr=False)
    model_id: str = ""
    model_path: str = field(default="", repr=False)

    def public_details(self) -> dict:
        details = {"kind": self.kind, "base_url": self.base_url, "name": self.name}
        if self.model_id:
            details["id"] = self.model_id
        return details

    def child_environment(self) -> dict:
        values = {
            "AI_TEST_MODEL_KIND": self.kind,
            "AI_TEST_MODEL_BASE_URL": self.base_url,
            "AI_TEST_MODEL_NAME": self.name,
            "AI_TEST_MODEL_API_KEY": self.api_key,
        }
        if self.kind == "local":
            values.update(
                {"AI_TEST_MODEL_ID": self.model_id, "AI_TEST_MODEL_PATH": self.model_path}
            )
        return values


def _normalized_model_path(value: str) -> str:
    """Normalize a Windows model path without touching the filesystem."""
    path = ntpath.normpath(str(value or "").strip())
    drive, _tail = ntpath.splitdrive(path)
    if not ntpath.isabs(path) or len(drive) != 2 or drive[1] != ":":
        raise PrerequisiteError("AI_TEST_MODEL_PATH must be an absolute drive path")
    if drive.upper() in ("E:", "F:"):
        raise PrerequisiteError("AI_TEST_MODEL_PATH uses a forbidden drive")
    return path.lower()


def local_model_id(path: str) -> str:
    """Match the panel's SHA-256 id of a lowercased resolved model path."""
    return hashlib.sha256(_normalized_model_path(path).encode("utf-8")).hexdigest()[:16]


def load_model_profile(environment) -> TestModelProfile | None:
    """Reject every partial profile instead of falling back to a local model."""
    if not any(key in environment for key in (*MODEL_ENV_KEYS, *LOCAL_ID_ENV_KEYS)):
        return None
    values = {key: str(environment.get(key) or "").strip() for key in MODEL_ENV_KEYS}
    if any(not value for value in values.values()):
        raise PrerequisiteError("AI_TEST_MODEL_* profile is incomplete")

    kind = values["AI_TEST_MODEL_KIND"].lower()
    base_url = values["AI_TEST_MODEL_BASE_URL"].rstrip("/")
    if kind not in ("local", "remote"):
        raise PrerequisiteError("AI_TEST_MODEL_KIND must be local or remote")
    try:
        parsed = urlsplit(base_url)
        _port = parsed.port
    except ValueError as exc:
        raise PrerequisiteError("AI_TEST_MODEL_BASE_URL is invalid") from exc
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise PrerequisiteError("AI_TEST_MODEL_BASE_URL is invalid")
    loopback = parsed.hostname.lower() in LOOPBACK_HOSTS
    if kind == "local" and (parsed.scheme != "http" or not loopback):
        raise PrerequisiteError("local test model URL must use HTTP loopback")
    if kind == "remote" and parsed.scheme != "https" and not (
        parsed.scheme == "http" and loopback
    ):
        raise PrerequisiteError("remote test model URL must use HTTPS or loopback")
    model_id = ""
    model_path = ""
    if kind == "local":
        model_id = str(environment.get("AI_TEST_MODEL_ID") or "").strip().lower()
        model_path = str(environment.get("AI_TEST_MODEL_PATH") or "").strip()
        if not model_id or not model_path:
            raise PrerequisiteError("local test model identity is incomplete")
        if model_id != local_model_id(model_path):
            raise PrerequisiteError("local test model id does not match selected path")
    return TestModelProfile(
        kind=kind,
        base_url=base_url,
        name=values["AI_TEST_MODEL_NAME"],
        api_key=values["AI_TEST_MODEL_API_KEY"],
        model_id=model_id,
        model_path=model_path,
    )


def _remote_probe_blocked(reason: str) -> bool:
    """Expose a fixed diagnostic code, never provider text or credentials."""
    print(f"TEST_MODEL_PREFLIGHT: BLOCKED - {reason}")
    return False


def _safe_status(response) -> str:
    status = getattr(response, "status_code", None)
    return str(status) if isinstance(status, int) and 100 <= status <= 599 else "unknown"


def probe_model(profile: TestModelProfile) -> bool:
    """Check the selected endpoint without accepting a different local model."""
    if profile.kind == "local":
        models = qa_local_llm.probe(profile.base_url, api_key=profile.api_key)
        if models is None or profile.name not in models:
            return False
        if not profile.base_url.endswith("/v1"):
            return False
        try:
            with httpx.Client(timeout=8.0) as client:
                response = client.get(
                    profile.base_url[:-3] + "/props",
                    headers={"Authorization": f"Bearer {profile.api_key}"},
                )
                response.raise_for_status()
                payload = response.json()
            actual_path = payload.get("model_path") if isinstance(payload, dict) else None
            return isinstance(actual_path, str) and (
                _normalized_model_path(actual_path)
                == _normalized_model_path(profile.model_path)
            )
        except (httpx.HTTPError, ValueError, PrerequisiteError):
            return False
    try:
        with httpx.Client(timeout=60.0) as client:
            response = client.post(
                profile.base_url + "/chat/completions",
                headers={"Authorization": f"Bearer {profile.api_key}"},
                json={
                    "model": profile.name,
                    "messages": [{"role": "user", "content": "Reply OK."}],
                    "max_tokens": 32,
                    "stream": False,
                },
            )
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        return _remote_probe_blocked(f"http_status={_safe_status(exc.response)}")
    except httpx.RequestError as exc:
        error_name = type(exc).__name__
        if error_name not in SAFE_REQUEST_ERRORS:
            error_name = "RequestError"
        return _remote_probe_blocked(f"network_error={error_name}")
    except httpx.HTTPError:
        return _remote_probe_blocked("http_error=other")
    try:
        payload = response.json()
    except ValueError:
        return _remote_probe_blocked(
            f"http_status={_safe_status(response)}; response_shape=invalid_json"
        )
    status = f"http_status={_safe_status(response)}; response_shape="
    if not isinstance(payload, dict):
        return _remote_probe_blocked(status + "not_object")
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return _remote_probe_blocked(status + "choices_not_list")
    if not choices:
        return _remote_probe_blocked(status + "choices_empty")
    if not isinstance(choices[0], dict):
        return _remote_probe_blocked(status + "choice_not_object")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return _remote_probe_blocked(status + "message_not_object")
    if any(
        isinstance(message.get(field), str) and message[field].strip()
        for field in ("content", "reasoning_content")
    ):
        return True
    finish_reason = choices[0].get("finish_reason")
    if finish_reason not in ("stop", "length", "tool_calls"):
        finish_reason = "other"
    return _remote_probe_blocked(
        status + f"message_empty; finish_reason={finish_reason}"
    )


def tavily_opt_in(environment) -> bool:
    value = str(environment.get("AI_TEST_TAVILY_ENABLED") or "").strip()
    if value not in ("", "0", "1"):
        raise PrerequisiteError("AI_TEST_TAVILY_ENABLED must be 0 or 1")
    if value == "1" and not str(environment.get("AI_TEST_TAVILY_API_KEY") or "").strip():
        raise PrerequisiteError("AI_TEST_TAVILY_API_KEY is required when enabled")
    return value == "1"


def redact_report(value, secret_values):
    """Remove credentials from nested run-report data before serialization."""
    secrets = sorted({str(item) for item in secret_values if item}, key=len, reverse=True)

    def scrub(item):
        if isinstance(item, str):
            for secret in secrets:
                item = item.replace(secret, "[redacted]")
            return item
        if isinstance(item, dict):
            return {scrub(key): scrub(entry) for key, entry in item.items()}
        if isinstance(item, list):
            return [scrub(entry) for entry in item]
        if isinstance(item, tuple):
            return tuple(scrub(entry) for entry in item)
        return item

    return scrub(value)
