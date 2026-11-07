"""Public browser submission status and manual input recovery contracts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import AttachmentMetadata, TurnOutcome
from .session_operations import (
    CompactOperation,
    MessageOperation,
    OperationKind,
    SessionOperation,
)


class SubmissionConflict(ValueError):
    code = "submission_conflict"


class InputRecoveryUnavailable(ValueError):
    code = "recovery_unavailable"


class InputAdmissionNotReady(ValueError):
    code = "service_not_ready"

    def __init__(self, phase: str):
        super().__init__(f"Service input admission is not ready ({phase}). Your input was not accepted; retry after initialization succeeds.")


class CompactUnavailable(ValueError):
    code = "compact_unavailable"


class CompactInProgress(ValueError):
    code = "compact_in_progress"

    def __init__(self, request_id: str, client_id: str):
        super().__init__("A compaction request is already pending or running in this conversation.")
        self.request_id, self.client_id = request_id, client_id


class ClientSubmissionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_request_id: str = Field(min_length=1, max_length=128)
    content: str = ""
    attachment_ids: list[str] = Field(default_factory=list)
    wiki_mode: Literal["auto", "read", "maintain"] | None = None
    recovery_of_client_id: str | None = Field(default=None, min_length=1, max_length=128)
    operation: SessionOperation | None = None

    @model_validator(mode="after")
    def validate_operation(self) -> "ClientSubmissionRequest":
        if self.operation is not None:
            if self.model_fields_set & {"content", "attachment_ids", "wiki_mode"}:
                raise ValueError("Supply either an operation or message fields")
            if self.operation.kind == "compact" and self.recovery_of_client_id is not None:
                raise ValueError("Compaction cannot replay a recovered input")
        elif "content" not in self.model_fields_set:
            raise ValueError("Supply an operation or message content")
        return self

    def session_operation(self) -> MessageOperation | CompactOperation:
        return self.operation or MessageOperation(kind="message", content=self.content,
            attachment_ids=self.attachment_ids, wiki_mode=self.wiki_mode)


class ClientSubmissionRefusal(BaseModel):
    session_id: str
    client_request_id: str
    accepted: Literal[False] = False
    message: str
    code: str | None = None
    disposition: str | None = None
    state: Literal["unknown"] | None = None
    canonical_request_id: str | None = None
    canonical_client_request_id: str | None = None


class SavedAttachment(AttachmentMetadata):
    sha256: str


class SubmissionInput(MessageOperation):
    attachments: list[SavedAttachment]


class NativeInputEvidence(BaseModel):
    """Protocol facts for one original input attempt, never proof of consumption."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    transport: Literal["codex-app-server"] = "codex-app-server"
    attempt_id: str = Field(min_length=1)
    input_id: str = Field(min_length=1)
    kind: Literal["start", "steer", "compact"] = "start"
    connection_generation: str | None = Field(default=None, min_length=1)
    target_turn_id: str | None = Field(default=None, min_length=1)
    rpc_id: str | None = Field(default=None, min_length=1)
    thread_id: str | None = Field(default=None, min_length=1)
    turn_id: str | None = Field(default=None, min_length=1)
    state: Literal["not_sent", "unknown", "accepted", "rejected"] = "not_sent"
    acceptance_source: Literal["start_response", "user_message", "steer_response", "compact_response", "compact_started"] | None = None
    # compact's empty ACK and its native root can arrive in either order.
    acknowledged: bool = False
    controller: Literal["not_needed", "unknown", "confirmed"] = "not_needed"

    @model_validator(mode="after")
    def require_evidence(self) -> "NativeInputEvidence":
        if self.kind == "steer" and (not self.connection_generation or not self.target_turn_id or not self.thread_id):
            raise ValueError("Native steer requires its original connection and target")
        if self.kind == "steer" and self.state == "accepted" and self.turn_id != self.target_turn_id:
            raise ValueError("Native steer acceptance must name its original target")
        if self.state == "not_sent" and self.rpc_id is not None:
            raise ValueError("An unsent native input cannot claim a submitted RPC")
        if self.state != "not_sent" and (self.rpc_id is None or self.thread_id is None):
            raise ValueError("A possibly submitted native input requires its RPC and thread identities")
        if self.kind == "compact":
            if self.state != "not_sent" and not self.connection_generation:
                raise ValueError("A submitted compaction requires its original connection")
            if self.target_turn_id is not None or self.controller != "not_needed":
                raise ValueError("Compaction cannot steer or inject message instructions")
            if self.state == "accepted":
                if self.acceptance_source not in {"compact_response", "compact_started"}:
                    raise ValueError("Compaction requires its own native acceptance proof")
                if self.acceptance_source == "compact_started" and self.turn_id is None:
                    raise ValueError("Compaction root proof requires the original turn")
                if self.acceptance_source == "compact_response" and not self.acknowledged:
                    raise ValueError("Compaction ACK proof requires its original response")
            elif self.turn_id is not None or self.acceptance_source is not None or self.acknowledged:
                raise ValueError("An unaccepted compaction cannot claim native execution")
            return self
        if self.acknowledged:
            raise ValueError("Separate ACK evidence is only used for compaction")
        if self.acceptance_source in {"compact_response", "compact_started"}:
            raise ValueError("Ordinary input cannot claim compaction acceptance")
        if self.state == "accepted":
            if self.turn_id is None or self.acceptance_source is None:
                raise ValueError("Native acceptance requires a correlated root and proof source")
        elif self.turn_id is not None or self.acceptance_source is not None:
            raise ValueError("An unaccepted native input cannot claim a root")
        return self


class SubmissionExecution(BaseModel):
    state: Literal["pending", "running", "done", "withdrawn", "unknown"]
    delivery_state: Literal["none", "confirmed", "unknown"] = "none"
    reason_code: str | None = None
    state_version: int
    updated_at: str | None = None
    execution_owner_request_id: str
    recoverable_input: bool
    native_inputs: list[NativeInputEvidence] = Field(default_factory=list)


class ClientSubmissionStatus(BaseModel):
    session_id: str
    client_request_id: str
    request_id: str
    operation: OperationKind
    accepted: bool
    disposition: str | None = None
    execution: SubmissionExecution
    input: SubmissionInput | CompactOperation | None = None
    outcome: TurnOutcome | None = None
    message: str | None = None
    code: str | None = None
    state: Literal["unknown"] | None = None
    recovery_of_client_id: str | None = None
    recovered_by_client_id: str | None = None


class ClientSubmissionSummary(BaseModel):
    client_request_id: str
    request_id: str
    operation: OperationKind
    execution: SubmissionExecution
    preview: str
    attachment_count: int


class SubmissionRecoveryPage(BaseModel):
    items: list[ClientSubmissionSummary]
    next_cursor: int | None = None
