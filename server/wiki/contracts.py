"""Shared request shapes for authenticated HTTP and model tools."""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

GuideModule = Literal["query", "retrieval", "entry", "maintenance", "article", "writing"]
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
