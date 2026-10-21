"""Public browser submission status and manual input recovery contracts."""

from typing import Literal

from pydantic import BaseModel

from .models import AttachmentMetadata, TurnOutcome


class SavedAttachment(AttachmentMetadata):
    sha256: str


class SubmissionInput(BaseModel):
    content: str
    attachment_ids: list[str]
    attachments: list[SavedAttachment]
    wiki_mode: str | None = None


class SubmissionExecution(BaseModel):
    state: Literal["reserved", "queued", "claimed", "completed", "succeeded", "failed", "interrupted", "withdrawn", "unknown", "rejected"]
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
