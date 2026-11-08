"""History collaborators for independent full-copy forks.

Full copy uses Claude's native transcript file or Codex's non-executing history
API. ForkArtifact carries the actual continuation identity; ForkCheckpoint
retains creation evidence even before path validation or cleanup permission.
The original saga owns that evidence and its resource/compensation ordering.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, NotRequired, Protocol, TypedDict

if TYPE_CHECKING:
    from .events import HarnessCredential
    from .run import HarnessRun

# Strategy tokens — used only inside the harness layer for clarity.
NATIVE_TRANSCRIPT = "native_transcript"
HISTORY_REPLAY = "history_replay"


@dataclass
class ForkArtifact:
    """What ``Harness.prepare_fork_copy`` returns.

    Native preparation returns the actual resume handle with no replay. File
    copies use the caller's hint; a native API can allocate a different ID and
    checkpoint it in the original saga. Replay returns no handle; the first
    explicit request later establishes its own native identity."""

    resume_id: str | None
    needs_replay: bool


@dataclass(frozen=True)
class ForkHooks:
    """The existing saga commits identity and owns the short native host."""

    checkpoint: Callable[[ForkCheckpoint], Awaitable[None]]
    manage: Callable[[HarnessRun], AbstractAsyncContextManager[None]]
    session_id: str
    user_id: str | None


class ForkCheckpoint(TypedDict):
    """Saved creation evidence, not proof that compensation is permitted.

    A returned path is deliberately untrusted until the adapter verifies it;
    the original identity must be saved even when that verification fails.
    """

    transport: Literal["app-server"]
    phase: Literal["unknown", "created"]
    cleanup_allowed: bool
    home: str
    source_thread_id: str
    rpc_id: NotRequired[str]
    connection_generation: NotRequired[str]
    thread_id: NotRequired[str]
    path: NotRequired[object]


def read_fork_checkpoint(value: object) -> ForkCheckpoint:
    """Read saved evidence without treating a malformed checkpoint as legacy."""
    if not isinstance(value, dict):
        raise ValueError("Malformed native fork checkpoint; retain recovery material")
    transport, phase = value.get("transport"), value.get("phase")
    allowed, home, source = value.get("cleanup_allowed"), value.get("home"), value.get("source_thread_id")
    if (transport != "app-server" or phase not in ("unknown", "created")
            or not isinstance(allowed, bool) or not isinstance(home, str) or not isinstance(source, str)):
        raise ValueError("Malformed native fork checkpoint; retain recovery material")
    result: ForkCheckpoint = {"transport": "app-server", "phase": "created" if phase == "created" else "unknown",
                              "cleanup_allowed": allowed, "home": home, "source_thread_id": source}
    for key in ("rpc_id", "connection_generation", "thread_id"):
        if key in value and not isinstance(value[key], str):
            raise ValueError("Malformed native fork identity; retain recovery material")
    if "rpc_id" in value:
        result["rpc_id"] = value["rpc_id"]
    if "connection_generation" in value:
        result["connection_generation"] = value["connection_generation"]
    if "thread_id" in value:
        result["thread_id"] = value["thread_id"]
    if "path" in value:
        result["path"] = value["path"]
    return result


class ForkCopy(Protocol):
    def __call__(self, *, parent_working_dir: str, parent_resume_id: str | None,
                 parent_credential: HarnessCredential | None = None, dest_working_dir: str,
                 new_resume_id: str, hooks: ForkHooks | None = None) -> Awaitable[ForkArtifact]: ...


class ForkCleanup(Protocol):
    def __call__(self, working_dir: str, resume_id_hint: str | None, fork_id: str, *,
                 credential: HarnessCredential | None = None, artifact: ForkCheckpoint | None = None,
                 hooks: ForkHooks | None = None) -> Awaitable[None]: ...


class BackendForkNotSupported(Exception):
    """Raised when a backend has no working fork strategy. Forward-compat —
    neither v1 backend raises it (both set ``profile.can_fork=True``)."""

    def __init__(self, backend: str) -> None:
        super().__init__(f"Backend {backend!r} does not support forking")
        self.backend = backend
