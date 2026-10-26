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

from .leaf import LeafResult, run_reason_leaf, run_web_leaf
from .schemas import (
    Finding,
    dedup_and_rank,
    parse_angles,
    parse_findings,
    parse_verdict,
    positively_verified,
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
            )
    if reason is None:
        async def reason(p: str) -> LeafResult:  # noqa: E306
            return await run_reason_leaf(
                harness, prompt=p, credential=credential, model=model,
                working_dir=working_dir, timeout=limits.reason_timeout, authorize_spawn=authorize_spawn,
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
    angles = parse_angles(scope_res.text, question=question, max_angles=limits.max_angles)

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
    for angle, res in zip(angles, search_results, strict=True):
        cost += res.cost or 0.0
        findings.extend(
            parse_findings(res.text, angle=angle, max_findings=limits.max_findings_per_angle)
        )

    # 3. Dedup + rank (pure) ---------------------------------------------------
    claims = dedup_and_rank(findings, max_claims=limits.max_claims)

    # 4. Verify (K adversarial votes each; strict-majority refute kills) -------
    await _emit("verify", f"Verifying {len(claims)} claim(s)…",
                claims=len(claims), gathered=len(findings))
    kill_threshold = limits.votes_per_claim // 2 + 1

    async def _verify(f: Finding) -> tuple[bool, float]:
        async with asyncio.TaskGroup() as vote_group:
            vote_tasks = [
                vote_group.create_task(_bounded(
                    search, verify_prompt(f.claim, f.url, question),
                ))
                for _ in range(limits.votes_per_claim)
            ]
        votes = [task.result() for task in vote_tasks]
        valid = [v for v in votes if not v.error and parse_verdict(v.text) is not None]
        refutes = sum(parse_verdict(v.text) is True for v in valid)
        supports = sum(positively_verified(v.text) for v in valid)
        f.verification = ("verified" if supports >= kill_threshold else
                          "unverified" if valid else "failed")
        leaf_cost = sum(v.cost or 0.0 for v in votes)
        return refutes < kill_threshold, leaf_cost

    survivors: list[Finding] = []
    if claims:
        async with asyncio.TaskGroup() as claim_group:
            claim_tasks = [claim_group.create_task(_verify(f)) for f in claims]
        verdicts = [task.result() for task in claim_tasks]
        for f, (survived, c) in zip(claims, verdicts, strict=True):
            cost += c
            if survived:
                survivors.append(f)

    # 5. Synthesize ------------------------------------------------------------
    verified = sum(f.verification == "verified" for f in survivors)
    unverified = len(survivors) - verified
    verification_failed = sum(f.verification == "failed" for f in survivors)
    counts = {"verified": verified, "unverified": unverified, "verification_failed": verification_failed}
    await _emit("synthesize", f"Writing the report from {verified} verified and {unverified} unverified finding(s)…",
                **counts)
    partial = ResearchReport(
        question=question, report="", findings=findings,
        sources=sorted({f.url for f in findings if f.url}), angles=angles,
        claims_examined=len(claims), cost=cost or None,
    )
    # Provider failures are LeafResult.error. Unknown exceptions include
    # unconfirmed subprocess retirement and must retain their failure owner.
    synth_res = await reason(synthesize_prompt(question, survivors))
    cost += synth_res.cost or 0.0
    partial.cost = cost or None
    if synth_res.error or not synth_res.text.strip():
        raise ResearchSynthesisError(
            f"Report synthesis failed: {synth_res.error or 'the model produced no report'}", partial,
        )
    report_text = synth_res.text

    sources = sorted({f.url for f in survivors if f.url})
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
    )
