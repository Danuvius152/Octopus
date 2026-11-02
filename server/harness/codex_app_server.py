"""Ordinary single-root Codex 0.166.0 stdio protocol.

The driver reports native facts. HarnessRun owns the host and retirement;
the original submission recorder commits input evidence before business I/O.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from ..submission_models import NativeInputEvidence
from . import codex_rpc
from .codex import (
    _CODEX_TRANSIENT_ERROR_PATTERNS,
    _OCTOPUS_SYSTEM_PROMPT_CODEX,
    _apply_home_dir,
    _apply_mcp_bearers,
    _fork_prepare_replay,
    _mcp_config_args,
)
from .codex_activity import CodexActivityObserver
from .codex_control import _latest_checkpoint, controller_message, turn_message
from .codex_items import (
    NATIVE_LINE_BYTES,
    diagnostic_frame,
    item_events,
    observation_frame,
)
from .codex_rpc import NativeProtocolError, NativeRpcError, RpcConnection
from .codex_rpc import encode_frame as _encode_frame
from .driver import ActiveTurnRef, RunDriver, SteerNotAccepted
from .events import HarnessEvent, HarnessPreparationError, NativeQuestionRef
from .native_output import NativeOutput
from .profile import ParseOutput, RuntimeProfile, TurnContext

if TYPE_CHECKING:
    from .run import HarnessRun

logger = logging.getLogger(__name__)

BINARY_SHA256 = "b8a11fedf24b2dfb59a70c9e52b030f9a508dcd49cd2dd0d41956f4c785156b8"
_METRICS_CLOSE_GRACE = 1.0
_METRICS_PENDING_COUNT = 256
_METRICS_PENDING_BYTES = 1024 * 1024
_REQUIRED_FEATURES = {"features.fast_mode": True, "features.wake_on_agent_messages": False, "features.goals": False,
                "features.sleep_tool": False, "features.multi_agent": True,
                "features.multi_agent_v2.enabled": True, "features.multi_agent_v2.wait_agent_enabled": True}


def verify_binary(path: str) -> None:
    with open(path, "rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != BINARY_SHA256:
        raise ValueError("Codex App Server requires the G0-Base verified 0.166.0 binary")


def build_argv(ctx: TurnContext) -> tuple[list[str], dict[str, Any]]:
    from ..config import settings

    argv = [settings.codex_app_server_binary, "app-server"]
    for key, value in _REQUIRED_FEATURES.items():
        argv += ["-c", f"{key}={str(value).lower()}"]
    argv += ["-c", "features.retain_client_developer_messages=true"]
    argv += _mcp_config_args(ctx)
    env = os.environ.copy()
    _apply_home_dir(env, ctx.credential)
    _apply_mcp_bearers(env, ctx)
    return argv, {"cwd": ctx.working_dir, "env": env}


def profile(base: RuntimeProfile) -> RuntimeProfile:
    from .codex_history import fork_cleanup, fork_copy

    return replace(base, new_driver=AppServerDriver, build_turn_argv=build_argv,
                   activity_observer=None, prepare_resume=None, can_steer=True,
                   native_input_evidence=True, tools_prompt=_OCTOPUS_SYSTEM_PROMPT_CODEX,
                   injects_memory_prompt=True, can_fork=True, fork_prepare=_fork_prepare_replay,
                   reusable=False, can_continue=True, stale_session_patterns=(),
                   auth_error_patterns=(), transient_error_patterns=_CODEX_TRANSIENT_ERROR_PATTERNS,
                   transient_error_codes=("server_overloaded", "native_provider_transient"), inline_large_inputs=True,
                   fork_copy=fork_copy, fork_cleanup=fork_cleanup)


def _provider_error_code(error: dict[str, Any]) -> str:
    """Only the bound provider's typed error can reject its credential."""
    info = error.get("codexErrorInfo")
    if info == "unauthorized":
        return "unauthorized"
    if isinstance(info, dict) and any(
        isinstance(info.get(kind), dict) and info[kind].get("httpStatusCode") == 401
        for kind in ("httpConnectionFailed", "responseStreamConnectionFailed",
                     "responseStreamDisconnected", "responseTooManyFailedAttempts")
    ):
        return "unauthorized"
    if info == "usageLimitExceeded":
        return "usage_limit_exceeded"
    # Explicit personal quota evidence wins over coincident capacity words.
    message = "\n".join(str(error.get(field) or "") for field in ("message", "additionalDetails")).lower()
    if any(phrase in message for phrase in (
        "insufficient_quota", "insufficient quota", "insufficient credits", "out of credits",
        "exceeded your current quota", "usage limit exceeded", "usage limit reached",
        "you have hit your usage limit", "you've hit your usage limit",
    )):
        return "usage_limit_exceeded"
    if info == "internalServerError":
        return "native_provider_transient"
    if isinstance(info, dict):
        for kind in ("httpConnectionFailed", "responseStreamConnectionFailed",
                     "responseStreamDisconnected", "responseTooManyFailedAttempts"):
            details = info.get(kind)
            if not isinstance(details, dict):
                continue
            status = details.get("httpStatusCode")
            if status == 429:
                return "rate_limit_exceeded"
            if status is None or isinstance(status, int) and (500 <= status < 600 or status == 408):
                return "native_provider_transient"
    return {"usageLimitExceeded": "usage_limit_exceeded", "rateLimitExceeded": "rate_limit_exceeded",
            "serverOverloaded": "server_overloaded"}.get(info if isinstance(info, str) else "", "native_turn_failed")


async def initialize_connection(io: HarnessRun, working_dir: str,
                                request: Callable[..., Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    """Shared native handshake and required configuration, without turn state."""
    await request("initialize", {"clientInfo": {"name": "octopus", "version": "1"},
                                  "capabilities": {"experimentalApi": True}})
    await io.write_control(b'{"method":"initialized"}\n')
    config = (await request("config/read", {"includeLayers": True, "cwd": working_dir}))["config"]
    for key, expected in _REQUIRED_FEATURES.items():
        actual: Any = config
        for part in key.split("."):
            actual = actual.get(part) if isinstance(actual, dict) else None
        if actual is not expected:
            raise NativeProtocolError(f"Codex required configuration mismatch: {key}")
    reminder = (config.get("features") or {}).get("current_time_reminder")
    if (isinstance(reminder, dict) and reminder.get("enabled") is True
            and reminder.get("clock_source") == "external"):
        raise NativeProtocolError("Codex external current-time reminders require currentTime/read, which is not supported")
    return config


class AppServerDriver(RunDriver):
    input_in_argv = False
    multiplexed = True
    stdout_line_limit_bytes = NATIVE_LINE_BYTES

    def __init__(self, profile: RuntimeProfile) -> None:
        super().__init__(profile)
        self._io: HarnessRun | None = None
        self._rpc = RpcConnection()
        self._input: NativeInputEvidence | None = None
        self._buffer: list[dict[str, Any]] = []
        self._buffer_bytes = 0
        self._terminal: str | None = None
        # Retain original evidence after a waiter times out or is cancelled;
        # a late ACK can still settle only that input on this connection.
        self._steers: dict[str, NativeInputEvidence] = {}
        self._questions: dict[str | int, NativeQuestionRef] = {}
        self._proof_tail: asyncio.Task[None] | None = None
        self._proofs: set[asyncio.Task[None]] = set()
        self._output: NativeOutput | None = None
        self._items: dict[tuple[str, str, str], tuple[str | None, str]] = {}
        self._items_changed = asyncio.Event()
        self._started_turns: set[tuple[str, str]] = set()
        self._metrics: dict[str, CodexActivityObserver] = {}
        self._metrics_env: dict[str, str] = {}
        self._metrics_started = time.time()
        self._prepared_at: float | None = None
        self._submitted_at: float | None = None
        self._accepted_at: float | None = None
        self._first_output_at: float | None = None
        self._terminal_at: float | None = None
        self._steer_started: dict[str, float] = {}
        self._metrics_capacity_drops = 0
        self._pending_measurements: list[tuple[HarnessEvent, int]] = []
        self._pending_measurement_bytes = 0
        self._preparation_mode = "not_started"
        self._controls: tuple[str, str | None, str] | None = None

    def continue_configuration(self, previous: RunDriver) -> None:
        if not isinstance(previous, AppServerDriver):
            raise ValueError("Native recovery cannot change its transport")
        self._controls = previous._controls

    def _save_proof(self, evidence: NativeInputEvidence, completed: Any) -> None:
        assert self._io is not None
        if len(self._proofs) >= 256:
            raise NativeProtocolError("Native input proof handoff exceeded capacity")
        previous = self._proof_tail

        async def save() -> None:
            try:
                if previous is not None:
                    await previous
                assert self._io is not None
                await self._io.record_input(evidence)
                completed()
            except Exception as error:
                assert self._io is not None
                self._io.fail_stream("native_input_recording_failed", self._io.format_exception(error))
                self._rpc.reject_all(error)

        task = self._proof_tail = asyncio.create_task(save(), name="native-input-proof")
        self._proofs.add(task)
        task.add_done_callback(self._proofs.discard)

    async def finish_observations(self) -> None:
        if self._proof_tail is not None:
            await asyncio.shield(self._proof_tail)

    async def close_observations(self, *, retired: bool) -> None:
        complete = True
        deadline = asyncio.get_running_loop().time() + _METRICS_CLOSE_GRACE

        async def finish(observer: CodexActivityObserver) -> None:
            nonlocal complete
            try:
                await observer.drain_observations()
            except Exception:
                complete = False
                logger.warning("Native response metadata drain failed", exc_info=True)
        try:
            async with asyncio.timeout_at(deadline):
                await asyncio.gather(*(finish(observer) for observer in self._metrics.values()))
        except TimeoutError:
            complete = False
            logger.warning("Native response metadata reached its finite collection deadline")
        finally:
            for observer in self._metrics.values():
                observer.invalidate()
                try:
                    source_closed = await observer.close(deadline=deadline)
                    complete &= source_closed
                    if source_closed:
                        self._observe_measurements(observer.finish_metrics())
                except Exception:
                    complete = False
                    logger.warning("Native supplementary observer close failed", exc_info=True)
        if self._io is None or self._input is None:
            self._metrics.clear()
            self._metrics_env.clear()
            return
        self._flush_measurements(final=True)
        now = time.monotonic()
        started, closed = self._io._started_at, self._io.admission_closed_at

        def elapsed(start: float | None, end: float | None) -> float | None:
            return round(max(0, end - start) * 1000, 3) if start is not None and end is not None else None

        self._io.record_measurement(HarnessEvent(type="model_measurement", raw={
            "kind": "codex_run", "transport": "app-server", "root_thread_id": self._input.thread_id,
            "root_turn_id": self._input.turn_id, "root_turns": int(self._input.turn_id is not None),
            "child_turns": self._output.child_turn_count if self._output is not None else 0,
            "input_state": self._input.state, "native_status": self._terminal,
            "resource_state": "proven" if retired else "failed", "end_reason": self._io.native_end_reason,
            "host_ms": elapsed(started, now), "preparation_ms": elapsed(started, self._prepared_at),
            "spawn_ms": elapsed(started, self._io._spawned_at), "preparation_mode": self._preparation_mode,
            "start_ack_ms": elapsed(self._submitted_at, self._accepted_at),
            "first_output_ms": elapsed(self._submitted_at, self._first_output_at),
            "root_active_ms": elapsed(self._submitted_at, self._terminal_at or closed),
            "retirement_ms": elapsed(closed, now), "metrics_threads": len(self._metrics),
            "metrics_unavailable": sum(observer.metrics_status != "verified" for observer in self._metrics.values()),
            "metrics_capacity_drops": self._metrics_capacity_drops,
            "metrics_collection_complete": complete,
            "retirement_phases": self._io.retirement_phases,
        }), request_id=self._input.input_id, attempt_id=self._input.attempt_id)
        self._metrics.clear()
        self._metrics_env.clear()

    def _observe_measurements(self, events: list[HarnessEvent]) -> None:
        assert self._io is not None and self._input is not None
        for event in events:
            assert event.raw is not None
            event.raw["transport"] = "app-server"
            scope = self._measurement_scope(event)
            if scope is not None:
                self._publish_measurement(event, scope)
                continue
            # Rollout persistence can precede the corresponding typed turn.
            # This disposable join buffer never accepts work or delays business
            # output. Unassociated measurements become diagnostic only when the
            # original finite collection ends; capacity loss stays explicit.
            size = len(json.dumps(event.raw, ensure_ascii=False).encode())
            if (len(self._pending_measurements) >= _METRICS_PENDING_COUNT
                    or self._pending_measurement_bytes + size > _METRICS_PENDING_BYTES):
                self._metrics_capacity_drops += 1
                continue
            self._pending_measurements.append((event, size))
            self._pending_measurement_bytes += size

    def _measurement_scope(self, event: HarnessEvent) -> str | None:
        assert self._input is not None and event.raw is not None
        thread, turn = event.raw.get("native_thread_id"), event.raw.get("native_turn_id")
        if self._input.turn_id is not None:
            if thread == self._input.thread_id:
                return "root" if turn == self._input.turn_id else "anomalous_root"
            if self._output is not None and self._output.contains_turn(thread, turn):
                return "child"
        return None

    def _publish_measurement(self, event: HarnessEvent, scope: str) -> None:
        assert self._io is not None and self._input is not None and event.raw is not None
        event.raw.update(root_thread_id=self._input.thread_id, root_turn_id=self._input.turn_id, scope=scope)
        self._io.record_measurement(event, request_id=self._input.input_id, attempt_id=self._input.attempt_id)

    def _flush_measurements(self, *, final: bool = False) -> None:
        pending, self._pending_measurements = self._pending_measurements, []
        self._pending_measurement_bytes = 0
        for event, size in pending:
            scope = self._measurement_scope(event)
            if scope is None and not final:
                self._pending_measurements.append((event, size))
                self._pending_measurement_bytes += size
            else:
                self._publish_measurement(event, scope or "unbound")

    def _metrics_observer(self, thread_id: str, *, path: str | None = None) -> CodexActivityObserver | None:
        if thread_id in self._metrics or len(self._metrics) >= 128:
            if thread_id not in self._metrics:
                self._metrics_capacity_drops += 1
            return self._metrics.get(thread_id)
        observer = CodexActivityObserver(self._metrics_env, lambda _: None,
            native_thread_id=thread_id, rollout_path=path, started_at=self._metrics_started)
        observer.set_event_handler(self._observe_measurements)
        self._metrics[thread_id] = observer
        return observer

    def question_open(self, ref: NativeQuestionRef) -> bool:
        return (not self._terminal and ref.connection_generation == self._rpc.generation
                and self._questions.get(ref.server_request_id) == ref)

    def question_sent(self, ref: NativeQuestionRef) -> None:
        self._questions.pop(ref.server_request_id, None)

    async def answer_question(self, io: HarnessRun, ref: NativeQuestionRef,
                              answers: list[dict[str, Any]] | None) -> None:
        if not self.question_open(ref):
            return
        result = {"answers": {} if answers is None else {
            question_id: {"answers": [answer["text"]] if answer.get("text") else
                            [option["label"] for option in answer["selected"]]}
            for question_id, answer in zip(ref.question_ids, answers, strict=True)}}
        await io.write_question((json.dumps({"id": ref.server_request_id, "result": result}) + "\n").encode(), ref)

    def _question(self, obj: dict[str, Any]) -> ParseOutput:
        assert self._input is not None and self._io is not None
        params = obj.get("params") or {}
        if (params.get("threadId") != self._input.thread_id or params.get("turnId") != self._input.turn_id
                or not isinstance(params.get("itemId"), str)):
            raise NativeProtocolError("Native user question does not belong to the accepted root")
        identifier = obj["id"]
        if isinstance(identifier, bool) or not isinstance(identifier, (str, int)):
            raise NativeProtocolError("Native server request has no usable identity")
        if identifier in self._questions:
            raise NativeProtocolError("Duplicate outstanding native server request")
        if len(self._questions) >= 64:
            raise NativeProtocolError("Outstanding native questions exceeded capacity")
        questions = params["questions"]
        if (not isinstance(questions, list) or not questions
                or not isinstance(params.get("isBlocking"), bool)
                or any(not isinstance(question, dict) or not isinstance(question.get("id"), str)
                       or not isinstance(question.get("question"), str) for question in questions)
                or len({question["id"] for question in questions}) != len(questions)):
            raise NativeProtocolError("Malformed native question form")
        ref = NativeQuestionRef(uuid.uuid4().hex, self._rpc.generation, identifier, self._input.input_id,
            self._input.attempt_id, params["threadId"], params["turnId"], params["itemId"],
            tuple(question["id"] for question in questions), params["isBlocking"])
        self._questions[identifier] = ref
        self._io.offer_question(HarnessEvent(type="question_request", tool_use_id=ref.question_id,
            tool_input={"questions": questions}, native_question=ref, raw=obj))
        return ParseOutput()

    @property
    def active_turn(self) -> ActiveTurnRef | None:
        return None if self._terminal else self.observed_root

    @property
    def observed_root(self) -> ActiveTurnRef | None:
        evidence = self._input
        if evidence is None or evidence.state != "accepted":
            return None
        assert evidence.thread_id and evidence.turn_id
        return ActiveTurnRef(self._rpc.generation, evidence.attempt_id, evidence.thread_id, evidence.turn_id)

    @property
    def native_terminal_status(self) -> str | None:
        return self._terminal

    @property
    def steering_open(self) -> bool:
        return self.active_turn is not None

    async def steer(self, io: HarnessRun, text: str, *, input_id: str | None,
                    target: ActiveTurnRef | None) -> str:
        if target is None or io.active_turn != target or input_id is None:
            raise SteerNotAccepted("The original native target is not active")
        try:
            request = self._rpc.request("turn/steer", {"threadId": target.thread_id,
                "expectedTurnId": target.turn_id, "input": [{"type": "text", "text": text}],
                "clientUserMessageId": input_id})
        except ValueError as error:
            raise SteerNotAccepted(str(error)) from error
        identifier = request.identifier
        with request:
            evidence = NativeInputEvidence(input_id=input_id, attempt_id=target.attempt_id, kind="steer",
                connection_generation=target.connection_generation, thread_id=target.thread_id, target_turn_id=target.turn_id)
            writing = False
            try:
                await io.record_input(evidence)
                evidence = evidence.model_copy(update={"rpc_id": identifier, "state": "unknown"})
                await io.record_input(evidence)
                self._steers[identifier] = evidence
                self._steer_started[identifier] = time.monotonic()
                writing = True
                await io.write_input(request.data, target=target)
                async with asyncio.timeout(codex_rpc.RPC_TIMEOUT):
                    await request.response
                return identifier
            except (SteerNotAccepted, asyncio.CancelledError) as error:
                if not writing or isinstance(error, SteerNotAccepted):
                    # Only this pre-write boundary can prove no bytes escaped.
                    known = self._steers.get(identifier)
                    if evidence.state == "unknown" and (known is None or known.state != "rejected"):
                        rejected = evidence.model_copy(update={"state": "rejected"})
                        await io.record_input(rejected)
                        self._steers[identifier] = rejected
                    raise SteerNotAccepted("Native input was not accepted",
                        submitted=isinstance(error, SteerNotAccepted) and error.submitted) from error
                raise
            finally:
                known = self._steers.get(identifier)
                if known is not None and known.state in {"accepted", "rejected"}:
                    self._steers.pop(identifier)
                    self._steer_started.pop(identifier, None)

    def _accept_steer(self, identifier: str, evidence: NativeInputEvidence, source: str) -> None:
        assert self._io is not None and self._input is not None
        if evidence.state != "accepted":
            began = self._steer_started.pop(identifier, None)
            self._io.record_measurement(HarnessEvent(type="model_measurement",
                duration_ms=(time.monotonic() - began) * 1000 if began is not None else None,
                raw={"kind": "codex_steer", "input_id": evidence.input_id,
                     "native_thread_id": evidence.thread_id, "native_turn_id": evidence.target_turn_id,
                     "state": "accepted", "acceptance_source": source}),
                request_id=self._input.input_id, attempt_id=evidence.attempt_id)
            updated = NativeInputEvidence.model_validate({**evidence.model_dump(), "state": "accepted",
                "turn_id": evidence.target_turn_id, "acceptance_source": source})
            self._steers[identifier] = updated

            def completed() -> None:
                self._rpc.resolve(identifier, {"turnId": evidence.target_turn_id})
                if not self._rpc.waiting(identifier):
                    self._steers.pop(identifier, None)

            self._save_proof(updated, completed)

    def build_argv(self, context: TurnContext) -> tuple[list[str], dict[str, Any]]:
        # Static saves and actual preparation check the known text payloads.
        # _request checks each complete envelope again before any write.
        _encode_frame("validation", "thread/start", {"developerInstructions": controller_message(context.system_prompt)})
        _encode_frame("validation", "turn/start", {"input": [{"type": "text", "text": context.prompt}]})
        if context.turn_instructions:
            _encode_frame("validation", "thread/inject_items", {"items": [{"type": "message", "role": "developer",
                "content": [{"type": "input_text", "text": turn_message(context.turn_instructions)}]}]})
        return super().build_argv(context)

    async def begin_input(self, io: HarnessRun, resume_id: str | None) -> None:
        self._io = io
        self._metrics_started = time.time()
        self._preparation_mode = "resume" if resume_id else "new"
        request_id, attempt_id = io.input_identity
        self._input = NativeInputEvidence(input_id=request_id, attempt_id=attempt_id, thread_id=resume_id,
                                          connection_generation=self._rpc.generation)
        await self._record()

    async def prepare(self, io: HarnessRun, context: TurnContext, argv: list[str], kwargs: dict[str, Any]) -> None:
        await asyncio.to_thread(verify_binary, argv[0])
        self._metrics_env = {"CODEX_HOME": (kwargs.get("env") or os.environ).get("CODEX_HOME") or ""}

    async def _model_metadata(self, model: str | None) -> dict[str, Any]:
        """Resolve the current native default or capability across catalog pages."""
        matches: list[dict[str, Any]] = []
        params: dict[str, Any] = {"includeHidden": True}
        cursors: set[str] = set()
        while True:
            page = await self._request("model/list", params)
            matches.extend(item for item in page["data"] if (
                item.get("model") == model if model is not None else item.get("isDefault")))
            cursor = page.get("nextCursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise NativeProtocolError("Codex model catalog returned an invalid cursor")
            cursors.add(cursor)
            params["cursor"] = cursor
        if len(matches) != 1:
            raise NativeProtocolError("Codex did not identify one current model or default")
        return matches[0]

    async def _record(self, **changes: Any) -> None:
        assert self._input is not None and self._io is not None
        updated = NativeInputEvidence.model_validate({**self._input.model_dump(), **changes})
        await self._io.record_input(updated)
        self._input = updated

    async def _request(self, method: str, params: dict[str, Any], *, business: bool = False,
                       controller_change: bool = False) -> dict[str, Any]:
        assert self._io is not None
        with self._rpc.request(method, params) as request:
            try:
                if business:
                    await self._record(rpc_id=request.identifier, state="unknown")
                    self._submitted_at = time.monotonic()
                    await self._io.write_input(request.data)
                else:
                    if controller_change:
                        await self._record(controller="unknown")
                    await self._io.authorize_input()
                    await self._io.write_control(request.data)
                async with asyncio.timeout(codex_rpc.RPC_TIMEOUT):
                    return await request.response
            except TimeoutError as error:
                if business:
                    raise NativeProtocolError(
                        "Native input acceptance is unknown; no input will be replayed. "
                        f"Unbound native observations: {json.dumps(self._buffer, ensure_ascii=False)}"
                    ) from error
                raise

    async def start_input(self, io: HarnessRun, context: TurnContext) -> None:
        assert self._input is not None
        request_id = self._input.input_id
        try:
            config = await initialize_connection(io, context.working_dir, self._request)
            controller = controller_message(context.system_prompt)
            # Null product overrides select this invocation's native defaults,
            # rather than the settings persisted in the resumed history.
            metadata = None
            model: str | None
            if self._controls is not None:
                model, effort, tier = self._controls
            else:
                model = context.model or config.get("model")
                if not model:
                    metadata = await self._model_metadata(None)
                    model = metadata.get("model")
                effort = context.reasoning_effort or config.get("model_reasoning_effort")
                tier = context.service_tier or "default"
            if not isinstance(model, str) or not model:
                raise NativeProtocolError("Codex current model could not be resolved")
            if tier == "fast":
                metadata = metadata or await self._model_metadata(model)
                if not any(item.get("id") == "priority" for item in metadata.get("serviceTiers", [])):
                    raise NativeProtocolError("Codex current model does not advertise Fast service")
            # A thread preparation refusal must not let the next attempt
            # resolve a different native default for this logical request.
            self._controls = model, effort, tier
            overrides: dict[str, Any] = {**_REQUIRED_FEATURES, "service_tier": tier}
            if effort:
                overrides["model_reasoning_effort"] = effort
            params: dict[str, Any] = {"cwd": context.working_dir, "model": model,
                "approvalPolicy": "never", "sandbox": "danger-full-access", "config": overrides,
                "developerInstructions": controller}
            if context.resume_id:
                params.update(threadId=context.resume_id, excludeTurns=True)
            prepared = await self._request("thread/resume" if context.resume_id else "thread/start", params)
            thread = prepared["thread"]
            thread_id = thread["id"]
            if not isinstance(thread_id, str) or not thread_id or context.resume_id and thread_id != context.resume_id:
                raise NativeProtocolError("Codex returned a different native history")
            if context.resume_id and (prepared.get("collaborationMode") or {}).get("mode") != "default":
                raise NativeProtocolError("Codex collaboration mode must be Default; incompatible or unknown history cannot run")
            if prepared.get("approvalPolicy") != "never" or prepared.get("sandbox", {}).get("type") != "dangerFullAccess":
                raise NativeProtocolError("Codex effective thread permissions differ from the request")
            if prepared.get("model") != model:
                raise NativeProtocolError("Codex effective model differs from the request")
            if effort is not None and prepared.get("reasoningEffort") != effort:
                raise NativeProtocolError("Codex effective reasoning effort differs from the request")
            if prepared.get("cwd") != context.working_dir:
                raise NativeProtocolError("Codex effective working directory differs from the request")
            if prepared.get("serviceTier") != ("priority" if tier == "fast" else tier):
                raise NativeProtocolError("Codex effective service tier differs from the request")
            await self._record(thread_id=thread_id)
            observer = self._metrics_observer(thread_id, path=thread.get("path"))
            if observer is not None:
                try:
                    async with asyncio.timeout(_METRICS_CLOSE_GRACE):
                        await observer.start()
                except TimeoutError:
                    observer.invalidate("metrics_preparation_cutoff")
                    await observer.close(deadline=asyncio.get_running_loop().time())
            await io._emit(HarnessEvent(type="session_started", session_id=thread_id))
            if context.resume_id:
                checkpoint = await asyncio.to_thread(_latest_checkpoint, thread["path"], thread_id)
                if checkpoint != controller:
                    await self._request("thread/inject_items", {"threadId": thread_id, "items": [{
                        "type": "message", "role": "developer", "content": [{"type": "input_text", "text": controller}],
                    }]}, controller_change=True)
                    await self._record(controller="confirmed")
            if context.turn_instructions:
                await self._request("thread/inject_items", {"threadId": thread_id, "items": [{
                    "type": "message", "role": "developer", "content": [{"type": "input_text", "text": turn_message(context.turn_instructions)}],
                }]}, controller_change=True)
                await self._record(controller="confirmed")
        except Exception as error:
            io.retire()
            code = _provider_error_code(error.provider_error) if isinstance(error, NativeRpcError) else "native_turn_failed"
            if code == "unauthorized":
                # A preparation RPC can refuse this request, but has no bound
                # root provider evidence that could revoke the main login.
                code = "permission_denied"
            raise HarnessPreparationError(str(error), error_code=
                "native_preparation_failed" if code == "native_turn_failed" else code) from error
        self._prepared_at = time.monotonic()
        await self._request("turn/start", {"threadId": thread_id,
            "input": [{"type": "text", "text": context.prompt}], "clientUserMessageId": request_id,
            "collaborationMode": {"mode": "default", "settings": {
                "model": model, "reasoning_effort": effort, "developer_instructions": None,
            }}}, business=True)

    def _bind(self, turn_id: Any, source: str) -> ParseOutput:
        assert self._input is not None
        if not isinstance(turn_id, str) or not turn_id:
            raise NativeProtocolError("Native start acceptance has no turn identity")
        if self._input.turn_id is not None and self._input.turn_id != turn_id:
            raise NativeProtocolError("Conflicting accepted native root identities")
        if self._input.state != "accepted":
            self._accepted_at = time.monotonic()
            self._input = NativeInputEvidence.model_validate({**self._input.model_dump(), "turn_id": turn_id,
                "state": "accepted", "acceptance_source": source})

            def completed() -> None:
                assert self._input is not None
                self._rpc.resolve(self._input.rpc_id or "", {"turn": {"id": turn_id}})

            self._save_proof(self._input, completed)
            assert self._input.thread_id is not None
            self._output = NativeOutput(self._input.input_id, self._input.attempt_id, self._input.thread_id, turn_id)
        # Start acceptance and stream integrity are independent. Resolve the
        # accepted start before interpreting its buffered output, so a later
        # malformed item fails through the stream without discarding partials.
        buffered, self._buffer = self._buffer, []
        self._buffer_bytes = 0
        output = ParseOutput()
        for frame in buffered:
            try:
                if frame["method"] == "item/tool/requestUserInput" and "id" in frame:
                    parsed = self._question(frame)
                elif frame["method"] == "serverRequest/resolved":
                    self._resolve_question(frame.get("params") or {})
                    parsed = ParseOutput()
                else:
                    parsed = self._event(frame)
            except Exception as error:
                failed = self._protocol_failure(frame, error)
                output.events.extend(failed.events)
                output.end_of_stream = True
                break
            output.events.extend(parsed.events)
            output.end_of_stream |= parsed.end_of_stream
        self._flush_measurements()
        return output

    def receive(self, obj: dict[str, Any]) -> ParseOutput:
        try:
            obj = observation_frame(obj)
            params = obj.get("params") or {}
            if (self._input is not None and obj.get("method") == "turn/started"
                    and isinstance(params.get("threadId"), str)):
                # Capture declared execution consumption before strict binding
                # rejects it. This read-only source never accepts that turn or
                # grants it a tool/input capability; unknown ancestry stays
                # diagnostic, outside normal response aggregates.
                observer = self._metrics_observer(params["threadId"])
                if observer is not None:
                    observer.start_live()
            return self._receive(obj)
        except Exception as error:
            return self._protocol_failure(diagnostic_frame(obj), error)

    def _protocol_failure(self, obj: dict[str, Any], error: Exception) -> ParseOutput:
        detail = f"{error}; native frame: {json.dumps(obj)}"
        if self._input is not None and self._input.state == "unknown":
            detail += f"; unbound observations: {json.dumps(self._buffer, ensure_ascii=False)}"
        protected = {identifier for identifier, evidence in self._steers.items() if evidence.state == "accepted"}
        if self._input is not None and self._input.state == "accepted" and self._input.rpc_id:
            protected.add(self._input.rpc_id)
        self._rpc.reject_all(NativeProtocolError(detail), exclude=protected)
        if self._io is not None:
            self._io._note_protocol_issue("fatal_protocol_error", json.dumps(obj), error=error)
            self._io.retire()
        return ParseOutput(events=[HarnessEvent(type="result", is_error=True,
            error_code="native_protocol_failed", content=detail, raw=obj)], end_of_stream=True)

    def _receive(self, obj: dict[str, Any]) -> ParseOutput:
        assert self._input is not None
        identifier = obj.get("id")
        if "method" not in obj:
            waiting = self._rpc.waiting(identifier)
            steer = self._steers.get(identifier)
            if steer is not None:
                assert isinstance(identifier, str)
                if "error" in obj:
                    if (obj["error"] or {}).get("code") != -32600:
                        # The pinned server uses InvalidRequest for explicit
                        # NotSubmitted reasons. A generic internal failure
                        # does not prove that no input was accepted.
                        self._rpc.reject(identifier, NativeProtocolError(json.dumps(obj["error"])))
                        return ParseOutput()
                    updated = steer.model_copy(update={"state": "rejected"})
                    assert self._io is not None
                    self._steers[identifier] = updated

                    def rejected() -> None:
                        self._rpc.reject(identifier, SteerNotAccepted(json.dumps(obj["error"]), submitted=True))

                    self._save_proof(updated, rejected)
                else:
                    turn_id = (obj.get("result") or {}).get("turnId")
                    if turn_id != steer.target_turn_id:
                        raise NativeProtocolError("Native steer response does not name its original root")
                    self._accept_steer(identifier, steer, "steer_response")
                return ParseOutput()
            if not waiting and identifier != self._input.rpc_id:
                return ParseOutput()
            output = ParseOutput()
            if "error" in obj:
                if identifier == self._input.rpc_id and (obj["error"] or {}).get("code") in {-32600, -32602}:
                    self._input = self._input.model_copy(update={"state": "rejected"})

                    def rejected_start() -> None:
                        self._rpc.reject(identifier, NativeProtocolError(json.dumps(obj["error"])))

                    self._save_proof(self._input, rejected_start)
                else:
                    self._rpc.receive(obj)
            else:
                result = obj.get("result")
                if not isinstance(result, dict):
                    raise NativeProtocolError("Malformed native RPC response")
                if identifier == self._input.rpc_id:
                    output = self._bind(result.get("turn", {}).get("id"), "start_response")
                else:
                    self._rpc.receive(obj)
            return output
        if identifier is not None:
            if obj["method"] == "item/tool/requestUserInput":
                if not self._input.turn_id:
                    self._buffer_frame(obj)
                    return ParseOutput()
                return self._question(obj)
            assert self._io is not None
            method = obj["method"]
            denied: dict[str, Any] | None = None
            if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
                denied = {"decision": "decline"}
            elif method == "item/permissions/requestApproval":
                denied = {"permissions": {}, "scope": "turn"}
            elif method == "mcpServer/elicitation/request":
                denied = {"action": "decline"}
            elif method in {"execCommandApproval", "applyPatchApproval"}:
                denied = {"decision": "denied"}
            elif method == "item/tool/call":
                denied = {"success": False, "contentItems": [{"type": "inputText",
                    "text": "No dynamic tool is registered for this request"}]}
            if denied is not None:
                self._io.reply_control((json.dumps({"id": identifier, "result": denied}) + "\n").encode())
                return ParseOutput()
            self._io.reply_control((json.dumps({"id": identifier, "error": {
                "code": -32601, "message": "This native request is not supported by the selected transport"}}) + "\n").encode())
            raise NativeProtocolError(f"Unsupported native request: {obj['method']}")
        params = obj.get("params") or {}
        if obj["method"] == "thread/tokenUsage/updated":
            self._usage_snapshot(params)
            return ParseOutput(activity=False)
        if obj["method"] == "serverRequest/resolved":
            if not self._input.turn_id:
                self._buffer_frame(obj)
            else:
                self._resolve_question(params)
            return ParseOutput()
        if obj["method"] == "turn/started" and self._input.rpc_id is None:
            raise NativeProtocolError("Native root began before this input was submitted")
        if params.get("threadId") != self._input.thread_id and not self._input.turn_id:
            if obj["method"].startswith(("item/", "turn/")) or obj["method"] == "error":
                self._buffer_frame(obj)
            return ParseOutput()
        if (params.get("threadId") == self._input.thread_id and obj["method"] == "item/completed"
                and (params.get("item") or {}).get("type") == "userMessage"):
            client_id = params["item"].get("clientId")
            for rpc_id, evidence in self._steers.items():
                if client_id == evidence.input_id:
                    if params.get("turnId") != evidence.target_turn_id:
                        raise NativeProtocolError("Native steer observation does not name its original root")
                    self._accept_steer(rpc_id, evidence, "user_message")
                    break
        if not self._input.turn_id:
            if obj["method"] not in {"turn/started", "turn/completed", "item/started", "item/completed", "item/agentMessage/delta", "error"}:
                return ParseOutput()
            self._buffer_frame(obj)
            item = params.get("item") or {}
            if (obj["method"] == "item/completed" and item.get("type") == "userMessage"
                    and item.get("clientId") == self._input.input_id):
                return self._bind(params.get("turnId"), "user_message")
            return ParseOutput()
        return self._event(obj)

    def _usage_snapshot(self, params: dict[str, Any]) -> None:
        assert self._input is not None and self._io is not None
        thread, turn, usage = params.get("threadId"), params.get("turnId"), params.get("tokenUsage")
        if not isinstance(thread, str) or not isinstance(turn, str) or not isinstance(usage, dict):
            return
        total = usage.get("total")
        if not isinstance(total, dict):
            return
        detail: dict[str, Any] = {"kind": "codex_native_usage", "native_thread_id": thread,
            "native_turn_id": turn, "source": "provider_cumulative_snapshot",
            "historical": self._input.rpc_id is None or thread == self._input.thread_id
                and turn != self._input.turn_id and (thread, turn) not in self._started_turns}
        for source, target in (("inputTokens", "input_tokens"), ("cachedInputTokens", "cached_input_tokens"),
                ("outputTokens", "output_tokens"), ("reasoningOutputTokens", "reasoning_tokens"), ("totalTokens", "total_tokens")):
            value = total.get(source)
            detail[target] = value if type(value) is int and value >= 0 else None
        self._io.record_measurement(HarnessEvent(type="model_measurement", raw=detail),
            request_id=self._input.input_id, attempt_id=self._input.attempt_id)

    def _buffer_frame(self, obj: dict[str, Any]) -> None:
        self._buffer_bytes += len(json.dumps(obj, ensure_ascii=False).encode())
        if self._buffer_bytes > 8 * 1024 * 1024 or len(self._buffer) >= 256:
            raise NativeProtocolError("Unbound native events exceeded the correlation buffer")
        self._buffer.append(obj)

    def _resolve_question(self, params: dict[str, Any]) -> None:
        ref = self._questions.get(params.get("requestId"))
        if ref is not None and params.get("threadId") == ref.thread_id:
            self.question_sent(ref)
            if self._io is not None:
                self._io.questions_changed()

    def _event(self, obj: dict[str, Any]) -> ParseOutput:
        assert self._output is not None
        method, params = obj["method"], obj.get("params") or {}
        if not method.startswith(("turn/", "item/")) and method != "error":
            return ParseOutput()
        if not self._output.accepts(params.get("threadId"), params.get("turn") or {}):
            raise NativeProtocolError("Native output has no original root/child association")
        observer = self._metrics_observer(params["threadId"])
        turn_id = (params.get("turn") or {}).get("id") if method in {"turn/started", "turn/completed"} else params.get("turnId")
        if not isinstance(turn_id, str) or not turn_id:
            raise NativeProtocolError("Native output has no turn identity")
        if method == "item/agentMessage/delta" and (not isinstance(params.get("itemId"), str) or not params["itemId"]):
            raise NativeProtocolError("Native delta has no item identity")
        if method == "turn/started":
            turn_key = params["threadId"], turn_id
            if turn_key in self._started_turns:
                return ParseOutput(activity=False)
            self._started_turns.add(turn_key)
            if len(self._started_turns) > 16384:
                raise NativeProtocolError("Native turn identity capacity exceeded")
        if method == "item/agentMessage/delta":
            # A root delta promises a complete item, even if its started
            # notification was omitted. Child cancellation promises no answer.
            self._items.setdefault((params["threadId"], turn_id, params["itemId"]), ("agentMessage", "started"))
        item = params.get("item") or {}
        if method in {"item/started", "item/completed"}:
            if not isinstance(item.get("id"), str) or not item["id"]:
                raise NativeProtocolError("Native item has no identity")
            key = params["threadId"], turn_id, item["id"]
            previous = self._items.get(key)
            digest = hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest() if method == "item/completed" else "started"
            if previous == (item.get("type"), digest) or previous is not None and method == "item/started":
                return ParseOutput(activity=False)
            if previous is not None and previous[1] != "started":
                raise NativeProtocolError("Conflicting complete native item")
            self._items[key] = item.get("type"), digest
            self._items_changed.set()
        if len(self._items) > 16384:
            raise NativeProtocolError("Native item identity capacity exceeded")
        output = self._raw_event(obj)
        source = self._output.source(params["threadId"], turn_id, item.get("id") or params.get("itemId"),
                                     item.get("phase"))
        for event in output.events:
            event.runtime_metadata = self._output.metadata(source)
        observations = self._output.observe(method, params)
        output.events.extend(observations)
        if observer is not None and self._output.contains_turn(params["threadId"], turn_id):
            observer.start_live()
        if method in {"turn/started", "turn/completed"}:
            self._flush_measurements()
        if method == "turn/completed" and observations:
            # A newly observed child terminal is progress; repeating its
            # diagnostic after that state is known cannot renew idle.
            output.activity = True
        for event in output.events:
            if event.subagent is not None and event.subagent.status == "interrupted":
                event.subagent.stop_reason = self._io.native_end_reason if self._io and self._io._lease_closed else "native_interrupted"
        return output

    def retirement_events(self, reason: str, *, reconcile: bool = False) -> list[HarnessEvent]:
        return self._output.retire(reason, reconcile=reconcile) if self._output is not None else []

    def release_output(self) -> None:
        self._items.clear()
        self._started_turns.clear()
        self._buffer.clear()
        self._buffer_bytes = 0
        self._output = None
        self.parser = self.profile.new_event_parser()

    def _raw_event(self, obj: dict[str, Any]) -> ParseOutput:
        assert self._input is not None
        method, params = obj["method"], obj.get("params") or {}
        turn_id = (params.get("turn") or {}).get("id") if method in {"turn/started", "turn/completed"} else params.get("turnId")
        root = params.get("threadId") == self._input.thread_id
        if root and (method.startswith(("turn/", "item/")) or method == "error") and turn_id != self._input.turn_id:
            raise NativeProtocolError(f"Unexpected root turn: {turn_id}")
        if root and method == "turn/started" and self._terminal:
            raise NativeProtocolError("Native root restarted after terminal")
        if root and self._first_output_at is None and (method == "item/agentMessage/delta" and params.get("delta")
                or method == "item/completed" and (params.get("item") or {}).get("type") in {"agentMessage", "reasoning"}):
            self._first_output_at = time.monotonic()
        if method == "error":
            if not isinstance(params.get("willRetry"), bool) or not isinstance(params.get("error"), dict):
                raise NativeProtocolError("Malformed native error notification")
            error = params["error"]
            return ParseOutput(events=[HarnessEvent(type="error", content=error.get("message"),
                error_code="native_provider_retry" if params["willRetry"] else "native_provider_error" if root else "native_child_error",
                error_details={**error, "willRetry": params["willRetry"], "connection_generation": self._rpc.generation}, raw=obj)],
                activity=bool(params["willRetry"] and not self._terminal and self._output
                    and self._output.active_turn(params["threadId"], turn_id)
                    and self._io and self._io.question_admission_open))
        if method == "turn/completed":
            if not root:
                error = (params.get("turn") or {}).get("error")
                if error:
                    return ParseOutput(events=[HarnessEvent(type="error", content=error.get("message"),
                        error_code="native_child_error", error_details=error, raw=obj)], activity=False)
                return ParseOutput()
            turn = params["turn"]
            status = turn.get("status")
            if status not in {"completed", "failed", "interrupted"}:
                raise NativeProtocolError(f"Invalid native root terminal status: {status}")
            if self._terminal:
                if status != self._terminal:
                    raise NativeProtocolError("Conflicting native root terminals")
                return ParseOutput(activity=False)
            self._terminal = status
            self._terminal_at = time.monotonic()
            assert self._io is not None
            if self._io.native_end_reason == "process_ended":
                self._io.native_end_reason = "parent_done" if turn.get("status") == "completed" else "parent_error"
            self._io.retire()
            return ParseOutput(events=[HarnessEvent(type="result", session_id=self._input.thread_id,
                is_error=status != "completed", error_code=None if status == "completed" else
                    _provider_error_code(turn.get("error") or {}) if status == "failed" else "native_turn_interrupted",
                content=(turn.get("error") or {}).get("message"), error_details=turn.get("error"), raw=obj)])
        if method == "item/agentMessage/delta":
            return ParseOutput(events=[HarnessEvent(type="text_delta", content=params.get("delta", ""), raw=obj)],
                               activity=bool(params.get("delta")))
        if method in {"item/started", "item/completed"}:
            output = item_events(method, params)
            for event in output.events:
                event.raw = obj
            return output
        return ParseOutput(activity=method.startswith(("turn/", "item/")))

    async def interrupt(self, io: HarnessRun) -> None:
        if self._input is not None and self._input.turn_id and not self._terminal:
            await io.write_control((json.dumps({"id": "octopus:interrupt", "method": "turn/interrupt",
                "params": {"threadId": self._input.thread_id, "turnId": self._input.turn_id}}) + "\n").encode())
        if self._pending_root_commands():
            # EOF removes the native notification subscriber before killing its
            # background terminals. Clean while the original reader is attached;
            # an ACK alone cannot prove the command's complete output arrived.
            assert self._input is not None
            with self._rpc.request("thread/backgroundTerminals/clean", {"threadId": self._input.thread_id}) as request:
                await io.write_control(request.data)
                await request.response
                while self._pending_root_commands():
                    self._items_changed.clear()
                    await self._items_changed.wait()

    def _pending_root_commands(self) -> bool:
        return self._input is not None and any(
            key[:2] == (self._input.thread_id, self._input.turn_id) and value == ("commandExecution", "started")
            for key, value in self._items.items())

    def connection_closed(self) -> None:
        self._questions.clear()
        if self._input is not None and self._input.state == "accepted" and self._io is not None:
            missing = [key[2] for key, value in self._items.items()
                       if key[:2] == (self._input.thread_id, self._input.turn_id) and value[1] == "started"]
            if missing:
                self._io.fail_stream("native_collection_incomplete", f"Native root items did not complete: {missing}")
        protected = {identifier for identifier, evidence in self._steers.items() if evidence.state in {"accepted", "rejected"}}
        if self._input and self._input.rpc_id:
            if self._input.state in {"accepted", "rejected"}:
                protected.add(self._input.rpc_id)  # Its original proof still owns its commit.
            elif self._input.state == "unknown":
                self._rpc.reject(self._input.rpc_id, NativeProtocolError(
                    "Native connection ended without acknowledgment; acceptance unknown; "
                    f"unbound observations: {json.dumps(self._buffer, ensure_ascii=False)}"))
        self._rpc.close(NativeProtocolError("Native connection ended without acknowledgment"), exclude=protected)
