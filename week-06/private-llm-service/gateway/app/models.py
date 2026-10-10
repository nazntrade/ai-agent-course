"""OpenAPI source models; clients cannot select provider or owner."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid",strict=True)


class CreateConversation(StrictModel):
    title: str = Field(min_length=1, max_length=80)


class RenameConversation(CreateConversation):
    expected_revision: int = Field(ge=1)


class SubmitRequest(StrictModel):
    text: str = Field(min_length=1, max_length=32000)
    expected_revision: int = Field(ge=1)


class Conversation(StrictModel):
    id: str
    title: str
    revision: int
    active_job_id: str | None
    created_at: float
    updated_at: float


class ConversationPage(StrictModel):
    items: list[Conversation]
    next_offset: int | None


class Message(StrictModel):
    id: int
    role: Literal["user", "assistant"]
    text: str
    job_id: str
    completion_status: str
    created_at: float


class MessagePage(StrictModel):
    items: list[Message]
    next_offset: int | None


class Snapshot(StrictModel):
    id: str
    conversation_id: str
    state: str
    state_version: int
    partial_content: str
    error_code: str | None
    finish_reason: str | None
    history_truncated: bool
    prompt_tokens: int | None
    prompt_budget: int | None
    queue_position: int | None
    created_at: float
    updated_at: float


class Identity(StrictModel):
    owner_id: str
    device_id: str


class ServiceStatus(StrictModel):
    accepting: bool
    mode: str
    waiting_jobs: int
    active_jobs: int
    queue_capacity: int
    rate_burst: float
    rate_refill_per_minute: float
    output_cap: int
    prompt_cap: int
    pairing_cleanup_required: bool


class ErrorDetail(StrictModel):
    code: str


class ErrorResponse(StrictModel):
    error: ErrorDetail
