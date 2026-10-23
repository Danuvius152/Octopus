"""Public browser submission status and manual input recovery contracts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .models import AttachmentMetadata, TurnOutcome


class SubmissionConflict(ValueError):
    code = "submission_conflict"


class InputRecoveryUnavailable(ValueError):
    code = "recovery_unavailable"


class ClientSubmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_request_id: str = Field(min_length=1, max_length=128)
    content: str
    attachment_ids: list[str] = Field(default_factory=list)
    wiki_mode: Literal["auto", "read", "maintain", "off"] | None = None
    recovery_of_client_id: str | None = Field(default=None, min_length=1, max_length=128)


class ClientSubmissionRefusal(BaseModel):
    session_id: str
    client_request_id: str
    accepted: Literal[False] = False
    message: str
    code: str | None = None
    disposition: str | None = None
    state: Literal["unknown"] | None = None


class SavedAttachment(AttachmentMetadata):
    sha256: str


class SubmissionInput(BaseModel):
    content: str
    attachment_ids: list[str]
    attachments: list[SavedAttachment]
    wiki_mode: str | None = None


class SubmissionExecution(BaseModel):
    state: Literal["pending", "running", "done", "withdrawn", "unknown"]
    delivery_state: Literal["none", "confirmed", "unknown"] = "none"
    reason_code: str | None = None
    state_version: int
    updated_at: str | None = None
    execution_owner_request_id: str
    recoverable_input: bool


class ClientSubmissionStatus(BaseModel):
    session_id: str
    client_request_id: str
    request_id: str
    accepted: bool
    disposition: str | None = None
    execution: SubmissionExecution
    input: SubmissionInput | None = None
    outcome: TurnOutcome | None = None
    message: str | None = None
    code: str | None = None
    state: Literal["unknown"] | None = None
    recovery_of_client_id: str | None = None
    recovered_by_client_id: str | None = None


class ClientSubmissionSummary(BaseModel):
    client_request_id: str
    request_id: str
    execution: SubmissionExecution
    preview: str
    attachment_count: int


class SubmissionRecoveryPage(BaseModel):
    items: list[ClientSubmissionSummary]
    next_cursor: int | None = None
