"""Fork contract DTOs for the harness layer (session-rewind.md §3).

A fork makes a parent session re-spawnable from a chosen branch point. The
*one* backend-specific concern is "synthesize a resumable transcript in this
backend's native format". Each profile picks a strategy:

  • NATIVE_TRANSCRIPT — synthesize a resumable transcript on disk at the
    backend's location using the caller-minted ``resume_id_hint`` as the
    artifact id; spawn resumes natively. (Claude.)
  • HISTORY_REPLAY — no on-disk work; return ``needs_replay=True`` so the
    first fork turn's user prompt is wrapped with the truncated history.
    (Codex.)

The harness owns both; callers above it (SessionManager, routers, frontend)
never branch on backend.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .run import HarnessRun

# Strategy tokens — used only inside the harness layer for clarity.
NATIVE_TRANSCRIPT = "native_transcript"
HISTORY_REPLAY = "history_replay"


@dataclass
class ForkArtifact:
    """What ``Harness.prepare_fork`` returns.

    Native preparation returns the actual resume handle with no replay. File
    copies use the caller's hint; a native API can allocate a different ID and
    checkpoint it in the original saga. Replay returns no handle; the first
    explicit request later establishes its own native identity."""

    resume_id: str | None
    needs_replay: bool


@dataclass(frozen=True)
class ForkHooks:
    """The existing saga commits identity and owns the short native host."""

    checkpoint: Callable[[dict[str, Any]], Awaitable[None]]
    manage: Callable[[HarnessRun], AbstractAsyncContextManager[None]]
    session_id: str
    user_id: str | None


class BackendForkNotSupported(Exception):
    """Raised when a backend has no working fork strategy. Forward-compat —
    neither v1 backend raises it (both set ``profile.can_fork=True``)."""

    def __init__(self, backend: str) -> None:
        super().__init__(f"Backend {backend!r} does not support forking")
        self.backend = backend
