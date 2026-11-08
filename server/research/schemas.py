"""Prompt builders + tolerant JSON parsing for the research pipeline
(native-deep-research.md §3). Pure functions — no I/O, no model — so the whole
shape is unit-testable without a backend.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from .evidence import EvidenceBounds, ResearchEvidence, VoteEvidence

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _extract_json(text: str) -> Any:
    """Pull the first JSON array or object out of a model reply, tolerating
    ```json fences and surrounding prose. Returns the parsed value or None."""
    if not text:
        return None
    candidates: list[str] = []
    m = _FENCE_RE.search(text)
    if m:
        candidates.append(m.group(1).strip())
    candidates.append(text.strip())
    # Also try the widest [...] / {...} span.
    for open_c, close_c in (("[", "]"), ("{", "}")):
        i, j = text.find(open_c), text.rfind(close_c)
        if 0 <= i < j:
            candidates.append(text[i : j + 1])
    for c in candidates:
        try:
            return json.loads(c)
        except (json.JSONDecodeError, TypeError):
            continue
    return None


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
    val = _extract_json(text)
    angles: list[str] = []
    if isinstance(val, list):
        for item in val:
            if isinstance(item, str) and item.strip():
                angles.append(item.strip())
            elif isinstance(item, dict):
                s = item.get("angle") or item.get("query") or item.get("q")
                if isinstance(s, str) and s.strip():
                    angles.append(s.strip())
    angles = angles[:max_angles]
    return angles or [question]


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
    val = _extract_json(text)
    if not isinstance(val, list):
        return None
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
    val = _extract_json(text)
    refuted = val.get("refuted") if isinstance(val, dict) else None
    if isinstance(refuted, str):
        refuted = {"true": True, "yes": True, "1": True,
                   "false": False, "no": False, "0": False}.get(refuted.strip().lower())
    if error or not isinstance(refuted, bool) or not isinstance(val, dict):
        return VoteEvidence(status="failed", note=bounds.text(error or "Invalid verification output", 256))
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
