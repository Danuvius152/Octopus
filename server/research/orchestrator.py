"""The deep-research pipeline (native-deep-research.md §3), orchestrated in
Python: scope → parallel angle-search → dedup/rank → adversarial verify →
synthesize. Every model call goes through an injectable async callable
(`search` for web leaves, `reason` for tool-free oneshots) — defaulting to the
real leaf executors — so the whole pipeline is unit-testable with fakes (the
`runner=`-injection pattern schedule_ai uses). One semaphore bounds ALL
concurrent leaves so a job never spawns the ~75-subprocess storm.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .evidence import (
    ClaimEvidence,
    EvidenceBounds,
    ResearchEvidence,
    SearchEvidence,
    claim_status,
)
from .leaf import LeafResult, run_reason_leaf, run_web_leaf
from .schemas import (
    Finding,
    dedup_and_rank,
    parse_angles,
    parse_search,
    parse_vote,
    scope_prompt,
    search_prompt,
    synthesize_prompt,
    verify_prompt,
)

logger = logging.getLogger(__name__)

# A leaf callable takes a prompt and returns a LeafResult. Two flavors: `search`
# (a web sub-turn) and `reason` (a tool-free oneshot).
LeafCallable = Callable[[str], Awaitable[LeafResult]]
ProgressCallback = Callable[["ResearchProgress"], Awaitable[None]]


@dataclass
class ResearchLimits:
    max_angles: int = 5
    max_findings_per_angle: int = 6
    max_claims: int = 12
    votes_per_claim: int = 2          # kill a claim only on a strict-majority refute
    concurrency: int = 4              # cap on simultaneous leaves within a job
    leaf_timeout: float = 150.0       # per web sub-turn
    reason_timeout: float = 90.0      # per oneshot


@dataclass
class ResearchProgress:
    phase: str                        # scope | search | verify | synthesize | done
    detail: str
    counts: dict[str, int] = field(default_factory=dict)


@dataclass
class ResearchReport:
    question: str
    report: str
    findings: list[Finding]
    sources: list[str]
    angles: list[str]
    claims_examined: int
    # Best-effort USD: the sum of per-leaf costs the backend reports. Only
    # WEB leaves report cost (Claude `result.total_cost_usd`); the tool-free
    # reasoning leaves go through `run_oneshot`, which returns only text, so
    # scope/synthesis cost is NOT included. Codex reports no USD at all → often
    # None. Treat as a partial lower bound, not a total (Vera review).
    cost: float | None = None
    evidence: ResearchEvidence = field(default_factory=ResearchEvidence)


class ResearchSynthesisError(Exception):
    """No final report was produced; retain gathered evidence for diagnosis."""

    def __init__(self, message: str, partial: ResearchReport):
        super().__init__(message)
        self.partial = partial


async def run_research(
    question: str,
    *,
    harness: Any = None,
    credential: Any = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    service_tier: str | None = None,
    working_dir: str,
    limits: ResearchLimits | None = None,
    on_progress: ProgressCallback | None = None,
    search: LeafCallable | None = None,
    reason: LeafCallable | None = None,
    authorize_spawn: Callable[[], Awaitable[None]] | None = None,
) -> ResearchReport:
    """Run the full pipeline. Provide `search`/`reason` to inject leaf behavior
    (tests); otherwise they default to the real executors bound to `harness` +
    `credential` + `model` + `working_dir` (so a real call needs a harness)."""
    limits = limits or ResearchLimits()
    sem = asyncio.Semaphore(max(1, limits.concurrency))
    cost = 0.0

    if search is None:
        async def search(p: str) -> LeafResult:  # noqa: E306
            return await run_web_leaf(
                harness, prompt=p, working_dir=working_dir, credential=credential,
                model=model, timeout=limits.leaf_timeout, authorize_spawn=authorize_spawn,
                reasoning_effort=reasoning_effort, service_tier=service_tier,
            )
    if reason is None:
        async def reason(p: str) -> LeafResult:  # noqa: E306
            return await run_reason_leaf(
                harness, prompt=p, credential=credential, model=model,
                working_dir=working_dir, timeout=limits.reason_timeout, authorize_spawn=authorize_spawn,
                reasoning_effort=reasoning_effort, service_tier=service_tier,
            )

    async def _emit(phase: str, detail: str, **counts: int) -> None:
        if on_progress is not None:
            await on_progress(ResearchProgress(phase=phase, detail=detail, counts=counts))

    async def _bounded(call: LeafCallable, prompt: str) -> LeafResult:
        async with sem:
            return await call(prompt)

    # 1. Scope -----------------------------------------------------------------
    await _emit("scope", "Decomposing the question into search angles…")
    scope_res = await reason(scope_prompt(question, limits.max_angles))
    cost += scope_res.cost or 0.0
    angles = parse_angles("" if scope_res.error else scope_res.text, question=question, max_angles=limits.max_angles)

    # 2. Search (parallel web leaves, bounded) ---------------------------------
    await _emit("search", f"Searching {len(angles)} angle(s)…", angles=len(angles))
    # Each fan-out scope owns its leaves until every finally block retires.
    # gather may surface the first cancellation/error before siblings unwind.
    async with asyncio.TaskGroup() as search_group:
        search_tasks = [
            search_group.create_task(_bounded(
                search, search_prompt(a, question, limits.max_findings_per_angle),
            ))
            for a in angles
        ]
    search_results = [task.result() for task in search_tasks]
    findings: list[Finding] = []
    bounds = EvidenceBounds()
    searches = []
    for angle, res in zip(angles, search_results, strict=True):
        cost += res.cost or 0.0
        parsed = None if res.error else parse_search(res.text, angle=angle, max_findings=limits.max_findings_per_angle)
        found = parsed[0] if parsed is not None else []
        bounds.omitted |= parsed is not None and parsed[1]
        searches.append(SearchEvidence(angle=bounds.text(angle, 256),
            status="failed" if parsed is None else "findings" if found else "empty",
            findings=len(found), reason=bounds.text(res.error or ("Invalid search output" if parsed is None else ""), 256)))
        findings.extend(found)

    # 3. Dedup + rank (pure) ---------------------------------------------------
    claims = dedup_and_rank(findings, max_claims=limits.max_claims)

    # 4. Verify (K adversarial votes each; strict-majority refute kills) -------
    await _emit("verify", f"Verifying {len(claims)} claim(s)…",
                claims=len(claims), gathered=len(findings))
    async def _verify(f: Finding) -> tuple[ClaimEvidence, float]:
        async with asyncio.TaskGroup() as vote_group:
            vote_tasks = [
                vote_group.create_task(_bounded(
                    search, verify_prompt(f.claim, f.url, question),
                ))
                for _ in range(limits.votes_per_claim)
            ]
        votes = [task.result() for task in vote_tasks]
        checks = [parse_vote(v.text, v.error, bounds) for v in votes]
        refutes = sum(v.status == "refute" for v in checks)
        supports = sum(v.status == "support" for v in checks)
        failed = sum(v.status == "failed" for v in checks)
        status = claim_status(supports, refutes, failed, len(votes))
        if len(checks) > 8:
            bounds.omitted = True
        claim = ClaimEvidence(claim=bounds.text(f.claim, 512), url=bounds.url(f.url),
            angle=bounds.text(f.angle, 256), status=status, support=supports, refute=refutes,
            unverified=len(votes) - supports - refutes - failed, failed=failed, votes=checks[:8])
        leaf_cost = sum(v.cost or 0.0 for v in votes)
        return claim, leaf_cost

    checked_claims = []
    if claims:
        async with asyncio.TaskGroup() as claim_group:
            claim_tasks = [claim_group.create_task(_verify(f)) for f in claims]
        verdicts = [task.result() for task in claim_tasks]
        for checked, c in verdicts:
            cost += c
            checked_claims.append(checked)

    evidence = ResearchEvidence(searches=searches[:5], claims=checked_claims[:12], votes_per_claim=limits.votes_per_claim,
        scope_note=bounds.text(scope_res.error or "", 256),
        omitted=bounds.omitted or len(searches) > 5 or len(checked_claims) > 12 or len(findings) > len(claims))
    survivors = [Finding(claim=c.claim, url=c.url, angle=c.angle, verification=c.status)
                 for c in evidence.claims if c.status != "refuted"]
    sources = sorted({f.url for f in survivors if f.url} |
                     {url for c in evidence.claims for v in c.votes for url in v.sources})

    # 5. Synthesize ------------------------------------------------------------
    verified = sum(f.verification == "verified" for f in survivors)
    unverified = len(survivors) - verified
    verification_failed = sum(f.verification == "failed" for f in survivors)
    counts = {"verified": verified, "unverified": unverified, "verification_failed": verification_failed}
    await _emit("synthesize", f"Writing the report from {verified} verified and {unverified} unverified finding(s)…",
                **counts)
    partial = ResearchReport(
        question=question, report="", findings=survivors,
        sources=sources, angles=[s.angle for s in evidence.searches],
        claims_examined=len(claims), cost=cost or None, evidence=evidence,
    )
    # Provider failures are LeafResult.error. Unknown exceptions include
    # unconfirmed subprocess retirement and must retain their failure owner.
    synth_res = await reason(synthesize_prompt(question, evidence))
    cost += synth_res.cost or 0.0
    partial.cost = cost or None
    if synth_res.error or not synth_res.text.strip():
        raise ResearchSynthesisError(
            f"Report synthesis failed: {synth_res.error or 'the model produced no report'}", partial,
        )
    report_text = evidence.markdown() + "\n\n---\n\n" + synth_res.text

    await _emit("done", "Research complete.",
                **counts, sources=len(sources))
    return ResearchReport(
        question=question,
        report=report_text,
        findings=survivors,
        sources=sources,
        angles=angles,
        claims_examined=len(claims),
        cost=cost or None,
        evidence=evidence,
    )
