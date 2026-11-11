"""Shared stand-in base for `HarnessRun` fakes.

The run loop asks a backend whether its process can be kept alive between
turns (inline-steering.md §7). A fake that doesn't answer crashes the turn
with an `AttributeError` instead of failing the assertion under test, and the
failure points at session_manager rather than at the fake — so the contract
lives here, once, for every test that stands in for a run.

Default is non-reusable: a fake behaves like the spawn-per-turn path unless a
test explicitly opts in.
"""

from __future__ import annotations

import asyncio

from server.diagnostics import exception_diagnostic
from server.harness import HarnessEvent


class FakeRunBase:
    """The parts of `HarnessRun` every stand-in must answer."""

    reusable = False
    can_steer = False

    def close_admission(self) -> None:
        """Stand-ins own no native process or live tool scope to close."""

    async def steer(self, text: str):
        if not self.can_steer:
            raise RuntimeError("This test runtime does not support steering")
        return await self.send_input(text)

    def is_alive(self) -> bool:
        return True

    def format_exception(self, error: BaseException) -> str:
        return exception_diagnostic(error)

    def spawn_signature(self, working_dir, credential) -> str:
        return "fake-signature"

    def matches_configuration(self, config, working_dir, credential) -> bool:
        """These stand-ins bake no settings into a real process."""
        return True

    async def send_turn(self, prompt: str) -> None:
        self.sent_turns = getattr(self, "sent_turns", [])
        self.sent_turns.append(prompt)


def capture_model_runs(manager, monkeypatch, *, hold=False):
    """Keep admission and settlement real while controlling the Harness seam."""
    started = []

    class Run(FakeRunBase):
        def __init__(self, session_id):
            self.session_id = session_id
            self.release = asyncio.Event()

        async def start(self, prompt, working_dir, resume_id=None, credential=None):
            self.prompt = prompt
            self.request_id = manager.dispatch_snapshot(self.session_id)["active_request_id"]
            started.append(self)

        async def stream(self):
            if hold:
                await self.release.wait()
            yield HarnessEvent(type="result", is_error=False)

        async def interrupt(self):
            self.release.set()

        async def stop(self):
            self.release.set()

    monkeypatch.setattr(manager, "_make_run", lambda session, *args, **kwargs: Run(session.id))
    return started
