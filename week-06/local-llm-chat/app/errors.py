"""Typed error taxonomy with stable public codes and HTTP status mapping.

Messages never include keys, headers or environment values (SPEC I11/R3.5).
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base error carrying a stable ``code`` and the HTTP status to return."""

    code = "internal_error"
    http_status = 500

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details or {}

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        return payload


class InvalidRequest(AppError):
    code = "invalid_request"
    http_status = 422


class ProviderError(AppError):
    """Base for answer-provider errors (HTTP 503)."""

    code = "provider_error"
    http_status = 503


class ProviderUnavailable(ProviderError):
    code = "provider_unavailable"


class ProviderTimeout(ProviderError):
    code = "provider_timeout"


class ProviderInvalidResponse(ProviderError):
    """Empty answer, broken JSON or transport error (SPEC R7.3)."""

    code = "provider_invalid_response"


class ProviderLengthError(ProviderError):
    code = "provider_length_error"


class ProviderNotConfigured(ProviderError):
    """Network provider selected without a complete own configuration."""

    code = "provider_not_configured"
    http_status = 503


class LocalProcessError(AppError):
    code = "local_process_error"
    http_status = 503


class PortInUse(LocalProcessError):
    """The loopback port is already taken; foreign processes are never stopped."""

    code = "port_in_use"
    http_status = 409


class RuntimeMissing(LocalProcessError):
    code = "runtime_missing"
    http_status = 503


class LocalStartTimeout(LocalProcessError):
    code = "local_start_timeout"


class DialogueError(AppError):
    code = "dialogue_error"
    http_status = 500


class DialogueNotFound(DialogueError):
    code = "dialogue_not_found"
    http_status = 404


class MemoryConflict(DialogueError):
    code = "memory_conflict"
    http_status = 409


class StorageSchemaUnsupported(DialogueError):
    code = "storage_schema_unsupported"
    http_status = 500
