"""Research leaf executors (native-deep-research.md §4–5).

A "leaf" is one fan-out unit:
  - WEB leaf (`run_web_leaf`): an isolated, scoped `HarnessRun` sub-turn that
    uses the backend's native web tools (read-only) to search/read and return
    text. It carries NO inherited MCP/connectors/memory, a scratch cwd, a
    minimal system prompt, and `web_research=True` (the profile renders a
    web-enabled, destructive-tool-free turn) — so it can't touch the user's
    session or the host.
  - REASONING leaf (`run_reason_leaf`): a tool-free `run_oneshot` (scope
    decompose, synthesize) — the same agnostic primitive schedule_ai uses.

Provider failures degrade to `LeafResult.error` so the orchestrator can carry
on. Cancellation and unconfirmed process retirement propagate to the job
owner. Backend-agnostic.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..aio import complete_cleanup
from ..harness import HarnessCredential, HarnessOneshotError, OneShotContext, RunConfig

logger = logging.getLogger(__name__)

# Minimal persona for a web leaf — it has no Octopus in-app tools, just the
# backend's web tools; the per-leaf user prompt carries the real instruction.
_LEAF_SYSTEM = (
    "You are a focused web-research worker. Use ONLY your web tools to do what "
    "the user asks, then reply with exactly the format requested — no preamble."
)


@dataclass
class LeafResult:
    """One leaf's outcome. `text` retains the reply, including failed partials;
    `cost` is USD when the backend reports it; `error` is set on any failure."""

    text: str
    cost: float | None = None
    error: str | None = None


class LeafRetirementError(RuntimeError):
    """Keep the original run and business evidence when its cleanup fails."""

    def __init__(self, run: Any, result: LeafResult) -> None:
        super().__init__("Web leaf execution retirement is unconfirmed")
        self.run = run
        self.result = result


async def run_web_leaf(
    harness: Any,
    *,
    prompt: str,
    working_dir: str,
    credential: HarnessCredential | None,
    model: str | None,
    timeout: float = 120.0,
    authorize_spawn: Callable[[], Awaitable[None]] | None = None,
) -> LeafResult:
    """Run one isolated, web-enabled, read-only-ish sub-turn and return its
    final text. Side-effect-contained: empty MCP set, no connectors/memory,
    `web_research=True`. Bounded by `timeout`; the process group is reaped on
    stop (turn-safety.md §2)."""
    config = RunConfig(
        session_id=None,        # no callback env → no bg/ask/ask_agent wiring
        transport="exec" if getattr(harness, "backend", None) == "codex" else None,
        system_prompt=_LEAF_SYSTEM,
        model=model,
        mcp_servers=[],         # [] (not None) = NO servers (None = all defaults)
        tool_allow=None,
        tool_deny=None,
        connectors=[],
        memory_dir=None,
        web_research=True,
        authorize_spawn=authorize_spawn,
    )
    run = harness.create_run(config)
    parts: list[str] = []
    cost = 0.0
    error: str | None = None
    successful_terminal = False
    try:
        await run.start(prompt, working_dir, None, credential=credential)

        async def _consume() -> None:
            nonlocal cost, error, successful_terminal
            async for ev in run.stream():
                if ev.type == "text" and ev.content:
                    parts.append(ev.content)
                elif ev.type == "result":
                    successful_terminal = not ev.is_error
                    if ev.cost:
                        cost += ev.cost
                    if ev.is_error:
                        error = error or (ev.content or "web leaf failed")
                elif ev.type == "error" and ev.is_error:
                    error = error or (ev.content or "web leaf error")

        await asyncio.wait_for(_consume(), timeout=timeout)
        if not successful_terminal:
            error = error or "web leaf ended without a successful terminal"
    except TimeoutError:
        error = "web leaf timed out"
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — a leaf must never crash the job
        logger.exception("web research leaf crashed")
        error = str(e)
    finally:
        try:
            await complete_cleanup(run.stop())
        except Exception as retirement_error:
            evidence = LeafResult("".join(parts).strip(), cost or None,
                                  error or (None if successful_terminal else "web leaf has no successful terminal"))
            raise LeafRetirementError(run, evidence) from retirement_error
    return LeafResult(text="".join(parts).strip(), cost=cost or None, error=error)


async def run_reason_leaf(
    harness: Any,
    *,
    prompt: str,
    credential: HarnessCredential | None,
    model: str | None,
    working_dir: str | None,
    timeout: float = 90.0,
    authorize_spawn: Callable[[], Awaitable[None]] | None = None,
) -> LeafResult:
    """Run one tool-free reasoning call (`run_oneshot`) — scope decompose /
    synthesize. Provider errors degrade; cleanup failures propagate."""
    ctx = OneShotContext(
        prompt=prompt, model=model, credential=credential, working_dir=working_dir,
        authorize_spawn=authorize_spawn,
    )
    try:
        text = await harness.run_oneshot(ctx, timeout=timeout)
        return LeafResult(text=(text or "").strip())
    except HarnessOneshotError as e:
        return LeafResult(text="", error=str(e) if e.code in {"configuration", "unauthorized"} else f"reasoning leaf failed: {e.code}")
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("reasoning leaf crashed")
        raise
