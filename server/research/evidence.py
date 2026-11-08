"""Bounded research material, shared by synthesis, saved reports and cards.

These are observations, not job/delivery/retirement states. The manager owns
their file; SQLite continues to own whether the job committed a report.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

MAX_EVIDENCE_BYTES = 256 * 1024
SourceUrl = Annotated[str, Field(max_length=512)]
ClaimStatus = Literal["verified", "refuted", "unverified", "failed"]


def claim_status(support: int, refute: int, failed: int, total: int) -> ClaimStatus:
    majority = total // 2 + 1
    return ("refuted" if refute >= majority else "verified" if support >= majority else
            "failed" if failed == total else "unverified")


class EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    @model_validator(mode="before")
    @classmethod
    def require_stored_fields(cls, value: Any, info: ValidationInfo) -> Any:
        # Construction defaults describe observations not supplied by a leaf.
        # Reading a saved v1 record must never fill holes in damaged evidence.
        if info.context == "stored":
            if not isinstance(value, dict) or cls.model_fields.keys() - value.keys():
                raise ValueError("Stored evidence must contain every v1 field")
            if "version" in value and type(value["version"]) is not int:
                raise ValueError("Stored evidence must have an explicit integer version")
        return value


class SearchEvidence(EvidenceModel):
    angle: str = Field(max_length=256)
    status: Literal["findings", "empty", "failed"]
    findings: int = Field(ge=0)
    reason: str = Field(default="", max_length=256)


class VoteEvidence(EvidenceModel):
    status: Literal["support", "refute", "unverified", "failed"]
    note: str = Field(max_length=256)
    conditions: str = Field(default="", max_length=256)
    sources: list[SourceUrl] = Field(default_factory=list, max_length=3)


class ClaimEvidence(EvidenceModel):
    claim: str = Field(max_length=512)
    url: str = Field(max_length=512)
    angle: str = Field(max_length=256)
    status: ClaimStatus
    support: int = Field(ge=0)
    refute: int = Field(ge=0)
    unverified: int = Field(ge=0)
    failed: int = Field(ge=0)
    votes: list[VoteEvidence] = Field(max_length=8)


class ResearchEvidence(EvidenceModel):
    version: Literal[1] = 1
    searches: list[SearchEvidence] = Field(default_factory=list, max_length=5)
    claims: list[ClaimEvidence] = Field(default_factory=list, max_length=12)
    votes_per_claim: int = Field(default=2, ge=0)
    scope_note: str = Field(default="", max_length=256)
    omitted: bool = False
    notices: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def derive_notices(self) -> ResearchEvidence:
        for claim in self.claims:
            if claim.support + claim.refute + claim.unverified + claim.failed != self.votes_per_claim:
                raise ValueError("Vote counts must retain the original denominator")
            status = claim_status(claim.support, claim.refute, claim.failed, self.votes_per_claim)
            if claim.status != status or len(claim.votes) > self.votes_per_claim:
                raise ValueError("Claim disposition must match the original votes")
            for name, recorded in (("support", claim.support), ("refute", claim.refute),
                                   ("unverified", claim.unverified), ("failed", claim.failed)):
                if sum(v.status == name for v in claim.votes) > recorded:
                    raise ValueError("Vote details exceed their recorded count")
        object.__setattr__(self, "notices", self._notices())
        return self

    def _notices(self) -> list[str]:
        notices = []
        failed = sum(item.status == "failed" for item in self.searches)
        if not self.searches:
            notices.append("Evidence unknown: no search observations recorded.")
        elif failed == len(self.searches):
            notices.append("Insufficient evidence: All searches failed.")
        elif failed:
            notices.append(f"Evidence gaps: {failed}/{len(self.searches)} searches failed.")
        if self.searches and not self.claims:
            notices.append("Insufficient evidence: no usable claims were found.")
        elif self.claims and not any(claim.status == "verified" for claim in self.claims):
            notices.append("Insufficient evidence: no claim has positive majority support.")
        if any(claim.status == "refuted" for claim in self.claims):
            notices.append("Refuted claims are excluded from recommendations; counterevidence is retained below.")
        if any(vote.status in {"support", "refute"} and not vote.sources for claim in self.claims for vote in claim.votes):
            notices.append("Evidence gaps: some checks did not provide their supporting or contradicting source URLs.")
        if self.omitted:
            notices.append("Some evidence details were shortened or omitted to keep this summary bounded.")
        return notices

    def markdown(self) -> str:
        lines = ["## Evidence and gaps", *[f"> {note}" for note in self.notices]]
        if self.scope_note:
            lines.append(f"Planning: {self.scope_note}")
        lines += ["", "### Search outcomes"]
        for search in self.searches:
            lines.append(f"- [{search.status}] {search.angle}: {search.findings} finding(s). {search.reason}")
        lines += ["", "### Claim checks",
                  f"Strict majority uses all {self.votes_per_claim} scheduled votes, including failures. "
                  "Votes and search angles are not independent sources."]
        for claim in self.claims:
            lines.append(f"- [{claim.status}] {claim.claim} (claim source: {claim.url or 'not provided'}). "
                         f"Support {claim.support}; refute {claim.refute}; unverified {claim.unverified}; failed {claim.failed}.")
            for vote in claim.votes:
                lines.append(f"  - [{vote.status}] {vote.note} Conditions: {vote.conditions or 'not provided'}. "
                             f"Check sources: {', '.join(vote.sources) or 'not provided'}")
        return "\n".join(lines)


class EvidenceBounds:
    """One construction pass clips external leaf text before it is retained."""

    def __init__(self) -> None:
        self.omitted = False

    def text(self, value: str, size: int) -> str:
        encoded = value.encode("utf-8")
        if len(encoded) <= size:
            return value
        self.omitted = True
        return encoded[:size - 3].decode("utf-8", errors="ignore") + "…"

    def url(self, value: str) -> str:
        if len(value.encode("utf-8")) <= 512:
            return value
        # A shortened URL is a different identity, not a usable citation.
        self.omitted = True
        return ""
