"""Script model effects while retaining the Owner-facing Run lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from server.harness import (
    HarnessCredential,
    HarnessEvent,
    HarnessRun,
    RunConfig,
    get_harness,
)
from server.harness.driver import ActiveTurnRef, ArgvDriver
from server.mcp_identity import register_run
from tests.harness_profiles import process_profile


class FakeRunBase(HarnessRun):
    """A scripted input, event stream and cleanup with an original run lease."""

    reusable = False
    can_steer = False

    def __init__(self, *, backend: str = "claude-code", config: RunConfig | None = None):
        super().__init__(self._scripted_profile(backend), config)

    def _scripted_profile(self, backend):
        profile = get_harness(backend).profile
        return process_profile(profile, new_driver=ArgvDriver, can_continue=profile.can_continue,
                               reusable=self.reusable, can_steer=self.can_steer, inline_large_inputs=False)

    def configure_for(self, session):
        self._profile = self._scripted_profile(session.backend)
        self._driver = ArgvDriver(self._profile)
        self._config.session_id = session.id
        self._config.user_id = session.user_id
        self._config.work_id = self.work_id = session._run.work_id
        return self

    async def start(self, prompt, working_dir, resume_id=None, credential=None, *, operation="message") -> None:
        self.operation = operation
        self.credential = credential
        await self._prepare_scripted_input(prompt, resume_id)
        if self._config.session_id is not None:
            register_run(self.run_lease_id, self._config.session_id, self._config.user_id, self.work_id)
        await self._begin_scripted_input()
        await self._start(prompt, working_dir, resume_id, credential=credential)

    async def _prepare_scripted_input(self, prompt: str, resume_id: str | None) -> None:
        """A scripted preparation refusal happens before input evidence or lease registration."""

    async def _start(self, prompt, working_dir, resume_id, *, credential: HarnessCredential | None) -> None:
        raise NotImplementedError

    async def _begin_scripted_input(self) -> None:
        if self._input_history_recorder is not None:
            await self._input_history_recorder(None, True)
        await self.authorize_input()
        self.input_may_have_run = True

    async def stream(self) -> AsyncIterator[HarnessEvent]:
        async for event in self._stream():
            self._activity()
            yield event

    def _stream(self) -> AsyncIterator[HarnessEvent]:
        raise NotImplementedError

    async def steer(self, text: str, *, input_id: str | None = None, target: ActiveTurnRef | None = None) -> str:
        if not self.can_steer:
            raise RuntimeError("This test runtime does not support steering")
        await self.authorize_input()
        return await self.send_input(text)

    def is_alive(self) -> bool:
        return not self._lease_closed

    def spawn_signature(self, working_dir, credential) -> str:
        return "fake-signature"

    def matches_configuration(self, config, working_dir, credential) -> bool:
        """These stand-ins bake no settings into a real process."""
        return True

    async def write_turn_input(self, prompt: str) -> None:
        self.operation = "message"
        await self._begin_scripted_input()
        await self._write_turn_input(prompt)

    async def _write_turn_input(self, prompt: str) -> None:
        self.sent_turns = getattr(self, "sent_turns", [])
        self.sent_turns.append(prompt)

    async def _stop_process(self) -> None:
        """A script with no external resources can retire immediately."""


def model_run_factory(factory):
    """Give an injected script the profile and lease scope of its captured session."""
    def make_run(session, *args, **kwargs):
        return factory(session, *args, **kwargs).configure_for(session)

    return make_run


def capture_model_runs(manager, monkeypatch, *, hold=False):
    """Keep admission and settlement real while controlling the Harness seam."""
    started = []

    class Run(FakeRunBase):
        def __init__(self, session_id):
            super().__init__()
            self.session_id = session_id
            self.release = asyncio.Event()

        async def _start(self, prompt, working_dir, resume_id=None, credential=None):
            self.prompt = prompt
            self.request_id = manager.dispatch_snapshot(self.session_id)["active_request_id"]
            started.append(self)

        async def _stream(self):
            if hold:
                await self.release.wait()
            yield HarnessEvent(type="result", is_error=False)

        async def interrupt(self):
            self.release.set()

        async def _stop_process(self):
            self.release.set()

    monkeypatch.setattr(manager, "_make_run", model_run_factory(lambda session, *args, **kwargs: Run(session.id)))
    return started
