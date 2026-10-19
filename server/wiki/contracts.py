"""Shared request shapes for authenticated HTTP and model tools."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

GuideModule = Literal["query", "retrieval", "entry", "worker", "maintenance", "article", "writing"]
ReadView = Literal["full", "compact"]


class Source(BaseModel):
    doc: str
    revision: str
    location: str


class ReadRequest(BaseModel):
    doc: str
    start: int = Field(default=0, ge=0)
    length: int = Field(default=16000, ge=1, le=24000)
    expected_revision: str | None = None


class ReviewObservation(BaseModel):
    """A comparison snapshot, never a document selection or write grant."""

    model_config = ConfigDict(extra="forbid")
    doc: str = Field(pattern=r"^[0-9a-f]{32}$")
    path: str
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    baseline_receipt_id: str | None
    dependency_generation: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_key: str


class MaintenanceSelectionRequest(BaseModel):
    """Observes candidates without selecting any files for a write grant."""

    model_config = ConfigDict(extra="forbid")
    directory: str = "Library"
    limit: int | None = Field(default=None, ge=1, le=200, strict=True)


class CreationRequest(BaseModel):
    """A bounded work proposal; only trusted admission can issue its grant."""

    model_config = ConfigDict(extra="forbid")
    purpose: Literal["requested_knowledge_page"]
    paths: list[str] | None = Field(default=None, max_length=16)


class GrantReadmissionRequest(BaseModel):
    """An owner explicitly narrows retained work; the original job is reused."""

    model_config = ConfigDict(extra="forbid")
    paths: list[str] = Field(default_factory=list, max_length=5000)
    creation: CreationRequest | None = None


class JobControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_attempt: int | None = Field(default=None, ge=0, strict=True)
    expected_vault_id: str | None = None


class JobProgress(BaseModel):
    total: int = Field(ge=0)
    completed: int = Field(ge=0)
    attention: int = Field(ge=0)
    failed: int = Field(ge=0)


class JobOutcome(BaseModel):
    doc_id: str | None
    path: str
    revision: str


class JobEffects(BaseModel):
    recorded: int = Field(default=0, ge=0)
    applied: int = Field(default=0, ge=0)
    index_pending: int = Field(default=0, ge=0)


class JobSnapshot(BaseModel):
    """A bounded notification; the job detail retains the complete evidence."""
    model_config = ConfigDict(extra="forbid")
    job_id: str
    state_version: int = Field(ge=1)
    attempt: int = Field(ge=0)
    status: str
    settlement_phase: Literal["none", "pending", "confirmed", "unconfirmed"]
    worker_session_id: str | None
    scope: str = Field(max_length=512)
    scope_truncated: bool
    progress: JobProgress
    outcome_refs: list[JobOutcome] = Field(max_length=10)
    outcome_count: int = Field(ge=0)
    outcome_refs_truncated: bool
    effects: JobEffects = Field(default_factory=JobEffects)
    recording_pending: int = Field(default=0, ge=0)
    error: str | None = Field(max_length=512)
    error_truncated: bool
    created_at: str
    updated_at: str


class JobCard(JobSnapshot):
    vault_id: str
    subscriber_id: str
    origin_request_id: str
    received_at: str


class ReadBatch(BaseModel):
    items: list[ReadRequest] = Field(min_length=1, max_length=20)
    budget_chars: int = Field(default=24000, ge=1, le=96000)
    view: ReadView = "full"


class RawEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    locator: str
    expected_block_hash: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    expected_text: str
    replacement: str
    kind: Literal["correction", "addition", "annotation"]


class RevisionBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    doc: str
    expected_revision: str
    reason: str = Field(min_length=1)
    item_doc: str | None = None


class PatchRevision(RevisionBase):
    operation: Literal["patch"]
    edits: list[RawEdit] = Field(min_length=1, max_length=100)
    frontmatter_patch: dict[str, Any] | None = None
    tags: list[str] | None = None
    aliases: list[str] | None = None
    sources: list[Source] | None = None


class SourceCheck(Source):
    resource: str


class ConfirmRevision(RevisionBase):
    operation: Literal["confirm_sources"]
    checks: list[SourceCheck] = Field(min_length=1, max_length=100)


class NoChangeRevision(RevisionBase):
    operation: Literal["no_change"]


Revision = Annotated[PatchRevision | ConfirmRevision | NoChangeRevision, Field(discriminator="operation")]
