"""Prompt builders + tolerant JSON parsing for the research pipeline
(native-deep-research.md §3). Pure functions — no I/O, no model — so the whole
shape is unit-testable without a backend.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from .evidence import EvidenceBounds, ResearchEvidence, VoteEvidence

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _outer_json(text: str) -> Iterator[tuple[int, int]]:
    """Locate outer containers, including those eligible for fence priority."""
    stack: list[str] = []
    start = 0
    quoted = escaped = False
    for index, char in enumerate(text):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            if not stack:
                start = index
            stack.append("]" if char == "[" else "}")
        elif stack and char == stack[-1]:
            stack.pop()
            if not stack:
                yield start, index + 1


def _extract_json[T](
    text: str,
    *,
    expected: type[list[Any]] | type[dict[str, Any]],
    parse: Callable[[Any], T | None],
) -> T | None:
    """Select one schema-valid value, preferring naked JSON, then full fences.

    Schema parsing must be pure: discarded or ambiguous candidates cannot
    change evidence bounds. None alone denotes an unusable candidate.
    """
    def decode(candidate: str) -> T | None:
        try:
            value = json.loads(candidate)
        except (ValueError, RecursionError):
            # Decoder syntax, integer-size and nesting refusals make this
            # candidate unusable; schema parsing remains outside this boundary.
            return None
        return parse(value) if isinstance(value, expected) else None

    try:
        whole = json.loads(text.strip())
    except (ValueError, RecursionError):
        pass
    else:
        # A complete value owns its whole tree even when its schema is wrong.
        return parse(whole) if isinstance(whole, expected) else None

    candidates = {(start, end): value for start, end in _outer_json(text)
                  if (value := decode(text[start:end])) is not None}
    fenced = []
    for match in _FENCE_RE.finditer(text):
        body = match.group(1)
        start = match.start(1) + len(body) - len(body.lstrip())
        end = match.end(1) - len(body) + len(body.rstrip())
        if (start, end) in candidates:
            fenced.append(candidates[start, end])
    if fenced:
        return fenced[0] if len(fenced) == 1 else None
    return next(iter(candidates.values())) if len(candidates) == 1 else None


@dataclass
class Finding:
    claim: str
    url: str
    angle: str = ""
    verification: str = "unverified"


# ------------------------------------------------------------------ scope


def scope_prompt(question: str, max_angles: int) -> str:
    return (
        "You are planning web research. Decompose the QUESTION into at most "
        f"{max_angles} distinct, specific search angles that together cover it. "
        "Return ONLY a JSON array of short search-query strings (no prose, no "
        "markdown fences).\n\nQUESTION: " + question
    )


def parse_angles(text: str, *, question: str, max_angles: int) -> list[str]:
    """Parse the scope reply into a list of angle strings; fall back to the
    raw question if the model didn't return usable JSON."""
    def angles_from(val: list[Any]) -> list[str] | None:
        angles: list[str] = []
        for item in val:
            if isinstance(item, str) and item.strip():
                angles.append(item.strip())
            elif isinstance(item, dict):
                s = item.get("angle") or item.get("query") or item.get("q")
                if isinstance(s, str) and s.strip():
                    angles.append(s.strip())
        return angles or None

    angles = _extract_json(text, expected=list, parse=angles_from)
    if angles is None:
        return [question]
    return angles[:max_angles] or [question]


# ------------------------------------------------------------------ search


def search_prompt(angle: str, question: str, max_findings: int) -> str:
    return (
        "Use your web tools to research the ANGLE below (it serves the overall "
        "QUESTION). Find authoritative sources and extract up to "
        f"{max_findings} specific, verifiable factual claims, each with the "
        "source URL you found it in. Return ONLY a JSON array of objects with "
        'keys "claim" and "url" (no prose, no markdown fences).\n\n'
        f"ANGLE: {angle}\nQUESTION: {question}"
    )


def parse_search(text: str, *, angle: str, max_findings: int) -> tuple[list[Finding], bool] | None:
    def findings_from(val: list[Any]) -> tuple[list[Finding], bool] | None:
        out: list[Finding] = []
        for item in val:
            if not isinstance(item, dict):
                continue
            claim = item.get("claim")
            url = item.get("url") or item.get("source") or ""
            if isinstance(claim, str) and claim.strip():
                out.append(
                    Finding(
                        claim=claim.strip(),
                        url=(url.strip() if isinstance(url, str) else ""),
                        angle=angle,
                    )
                )
        # Only an actual empty array means a successful search without material.
        return (out[:max_findings], len(out) > max_findings) if out or not val else None

    return _extract_json(text, expected=list, parse=findings_from)


# ------------------------------------------------------------------ dedup + rank


def _norm_claim(claim: str) -> str:
    return re.sub(r"\s+", " ", claim.lower()).strip(" .")


def dedup_and_rank(findings: list[Finding], *, max_claims: int) -> list[Finding]:
    """Drop near-duplicate claims (normalized text), keeping the first; rank so
    claims corroborated across MORE angles come first, then cap. Pure."""
    by_key: dict[str, Finding] = {}
    angles_for: dict[str, set[str]] = {}
    for f in findings:
        if not f.claim:
            continue
        k = _norm_claim(f.claim)
        if not k:
            continue
        if k not in by_key:
            by_key[k] = f
            angles_for[k] = set()
        if f.angle:
            angles_for[k].add(f.angle)
    ranked = sorted(
        by_key.items(), key=lambda kv: len(angles_for[kv[0]]), reverse=True
    )
    return [f for _, f in ranked[:max_claims]]


# ------------------------------------------------------------------ verify


def verify_prompt(claim: str, url: str, question: str) -> str:
    return (
        "Fact-check the CLAIM using your web tools. Actively search for evidence "
        "that CONTRADICTS it. Return ONLY a JSON object with keys "
        '"refuted" (true/false), "verified" (true/false), "note" (reason), '
        '"conditions" (limits or exceptions), and "sources" (array of URLs for the '
        'supporting or contradicting evidence actually found, empty if none). Mark refuted '
        "true ONLY if you find credible contradicting evidence; if it checks "
        "out or is merely unverifiable, refuted is false. Mark verified true "
        "ONLY when credible evidence positively supports the claim; an absence "
        "of contradiction alone is not verification.\n\n"
        f"CLAIM: {claim}\nSOURCE: {url}\nQUESTION: {question}"
    )


def parse_vote(text: str, error: str | None, bounds: EvidenceBounds) -> VoteEvidence:
    def verdict_from(val: dict[str, Any]) -> dict[str, Any] | None:
        refuted = val.get("refuted")
        if isinstance(refuted, str):
            refuted = {"true": True, "yes": True, "1": True,
                       "false": False, "no": False, "0": False}.get(refuted.strip().lower())
        return {**val, "refuted": refuted} if isinstance(refuted, bool) else None

    val = None if error else _extract_json(text, expected=dict, parse=verdict_from)
    if val is None:
        return VoteEvidence(status="failed", note=bounds.text(error or "Invalid verification output", 256))
    refuted = val["refuted"]
    urls = val.get("sources")
    sources = [url.strip() for url in urls if isinstance(url, str) and url.startswith(("https://", "http://"))] if isinstance(urls, list) else []
    if len(sources) > 3:
        bounds.omitted = True
    note, conditions = val.get("note"), val.get("conditions")
    return VoteEvidence(status="refute" if refuted else "support" if val.get("verified") is True else "unverified",
        note=bounds.text(note if isinstance(note, str) else "Reason not provided", 256),
        conditions=bounds.text(conditions if isinstance(conditions, str) else "", 256),
        sources=[bounded for url in sources[:3] if (bounded := bounds.url(url))])


# ------------------------------------------------------------------ synthesize


def synthesize_prompt(question: str, evidence: ResearchEvidence) -> str:
    return (
        "Write a concise, well-structured research report that answers the "
        "QUESTION using ONLY the EVIDENCE below. Each claim has a verification "
        "status: explicitly qualify unverified findings; failed verification or "
        "absence of refutation is not supporting evidence. Cite sources inline "
        "as [url]. If the findings are insufficient, say so plainly. Use "
        "Markdown. Refuted claims must NOT be recommendations: explain their "
        "counterevidence, conditions and disposition. Preserve search failures "
        "and evidence gaps. Counts of votes or angles do not prove independent sources.\n\n"
        f"QUESTION: {question}\n\nEVIDENCE:\n{evidence.markdown()}"
    )
