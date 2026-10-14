"""Read-only Codex activity and observations, bound to one captured subprocess.

Neither tracing lines nor native item contents leave this adapter. A current
process turn UUID, its stdout thread UUID and the rollout's own session UUID
must agree before metadata can affect the current attempt. Native observations
carry only identities and lifecycle labels, independently of idle activity.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import stat
import time
from collections.abc import Callable, Generator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from .events import HarnessEvent, SubagentUpdate

logger = logging.getLogger(__name__)
_DIAGNOSTIC_ENV = "error,codex_exec=info,opentelemetry_sdk=off,opentelemetry_otlp=off"
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_TRACE = re.compile(
    r'^\S+\s+(?P<level>TRACE|DEBUG|INFO|WARN|ERROR)\s+'
    r'(?:codex\.exec\{otel\.kind="internal"(?: thread\.id="(?P<thread>[0-9a-f-]{36})")?\}:\s*)?'
    r'codex_exec:\s*(?P<message>.*)$'
)
_TRACE_LEVEL = re.compile(r"^\d{4}-\d{2}-\d{2}T\S+\s+(?:TRACE|DEBUG|INFO|WARN|ERROR)\b", re.I)
_TURN = re.compile(r"^Sent prompt with event ID: (?P<id>[0-9a-f-]{36})$")
# Response bodies remain opaque: only their function header strings are read.
_METADATA = re.compile(rb'^\s*\{\s*(?:"timestamp"\s*:\s*(?P<timestamp>"[^"\\]*")\s*,\s*)?(?:"ordinal"\s*:\s*\d+\s*,\s*)?"type"\s*:\s*"(?P<type>session_meta|event_msg|response_item)"')
_ITEM_COMPLETION = re.compile(rb'\s*,\s*"payload"\s*:\s*\{\s*"type"\s*:\s*"item_completed"')
_TURN_STARTED = re.compile(rb'\s*,\s*"payload"\s*:\s*\{\s*"type"\s*:\s*"task_started"')
_FUNCTION = re.compile(rb'\s*,\s*"payload"\s*:\s*\{')
_STRING = rb'"(?:[^"\\]|\\.)*"'
_HEADER_FIELD = re.compile(rb'\s*(?P<key>' + _STRING + rb')\s*:\s*(?P<value>' + _STRING + rb'|null)\s*(?P<end>[,}])')
_NATIVE_TOOLS = {"spawn_agent", "followup_task", "send_message", "interrupt_agent"}
_MAX_LINE = 256 * 1024
_MAX_READ = 1024 * 1024
_DISCOVERY_BATCH_SIZE = 1024


def _uuid(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        normalized = str(UUID(value))
    except ValueError:
        return None
    return normalized if normalized == value else None


class CodexActivityObserver:
    """An owned incremental cursor; polling alone never produces activity."""

    def __init__(
        self, env: Mapping[str, str], notify: Callable[[float], None], *,
        resume_id: str | None = None,
        wall_clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._root = Path(env.get("CODEX_HOME") or Path.home() / ".codex").resolve() / "sessions"
        self._notify = notify
        self._wall = wall_clock
        self._monotonic = monotonic
        self._started = wall_clock()
        self._started_monotonic = monotonic()
        self._thread = _uuid(resume_id)
        self._stdout_thread: str | None = None
        self._diagnostic_thread: str | None = None
        self._turn: str | None = None
        self._path: Path | None = None
        self._discovery: Generator[None, None, Path | None] | None = None
        self._inode: tuple[int, int] | None = None
        self._offset = 0
        self._partial = b""
        self._skipping_line = False
        self._file_verified = False
        self._latest_completion_ms: int | None = None
        self._closed = False
        self._disabled = False
        self._warning: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._read_task: asyncio.Task[tuple[list[float], list[HarnessEvent]]] | None = None
        self._cursor_lock = asyncio.Lock()
        self._event_handler: Callable[[list[HarnessEvent]], None] | None = None
        self._native_turn: str | None = None
        self._function_headers: dict[str, str] = {}
        self._observed: dict[tuple[str, str], str] = {}
        self._stderr_continuation = False
        self.matched_records = 0

    def set_event_handler(self, handler: Callable[[list[HarnessEvent]], None]) -> None:
        """Attach the current run's metadata-only normalized observation sink."""
        self._event_handler = handler

    @property
    def identity_confirmed(self) -> bool:
        return bool(self._file_verified and self._turn and self._stdout_thread == self._thread)

    def _disable(self, code: str) -> None:
        self._disabled = True
        if self._warning is None:
            self._warning = code
            # Codes only: no thread, pathname, diagnostic or native body.
            logger.warning("Codex supplementary metadata unavailable (%s); using stdout observations and activity", code)

    def consume_stderr(self, line: str) -> bool:
        """Consume every newly enabled exec diagnostic before any retention."""
        clean = _ANSI.sub("", line)
        if _TRACE_LEVEL.match(clean):
            self._stderr_continuation = False
        match = _TRACE.fullmatch(clean)
        if match and match["level"] in ("INFO", "WARN", "DEBUG", "TRACE"):
            self._stderr_continuation = match["level"] != "INFO"
            if match["level"] == "INFO":
                identity = _TURN.fullmatch(match["message"])
                if identity:
                    turn = _uuid(identity["id"])
                    diagnostic_thread = _uuid(match["thread"]) if match["thread"] else None
                    if match["thread"] and diagnostic_thread is None:
                        self._disable("thread_identity_conflict")
                    if diagnostic_thread is not None:
                        if self._thread is not None and self._thread != diagnostic_thread:
                            self._disable("thread_identity_conflict")
                        self._diagnostic_thread = diagnostic_thread
                    if turn is None or (self._turn is not None and self._turn != turn):
                        self._disable("turn_identity_conflict")
                    elif not self._closed:
                        self._turn = turn
                elif not match["message"].startswith(("Codex initialized with event:", "Sent review request with event ID:")):
                    self._stderr_continuation = True
            return True
        # A formatter change must not accidentally expose the new diagnostics.
        if "codex_exec" in clean and re.search(r"\b(?:INFO|WARN|DEBUG|TRACE)\b", clean, re.I):
            self._stderr_continuation = True
            if "Sent prompt with event ID:" in clean:
                self._disable("diagnostic_format")
            return True
        if "Sent prompt with event ID:" in clean or "Codex initialized with event:" in clean:
            self._disable("diagnostic_format")
            self._stderr_continuation = True
            return True
        # Display-formatted WARN errors may contain raw newlines. Unmarked
        # continuation lines remain diagnostic data; the next structured
        # record (including existing ERROR) restores normal framing.
        return self._stderr_continuation

    def observe_stdout(self, obj: dict[str, Any]) -> None:
        if self._closed or self._disabled or obj.get("type") != "thread.started":
            return
        thread = _uuid(obj.get("thread_id"))
        if (thread is None or (self._thread is not None and self._thread != thread)
                or (self._diagnostic_thread is not None and self._diagnostic_thread != thread)):
            self._disable("thread_identity_conflict")
            return
        self._thread = self._stdout_thread = thread

    def _discover(self) -> Path | None:
        if self._root.is_symlink():
            self._disable("rollout_root_symlink")
            return None
        if self._thread is None:
            return None
        if not self._root.is_dir():
            if not self._closed and self._turn and self._stdout_thread:
                self._disable("rollout_missing")
            return None
        if self._discovery is None:
            self._discovery = self._find_rollout()
        for _ in range(_DISCOVERY_BATCH_SIZE):
            try:
                next(self._discovery)
            except StopIteration as done:
                self._discovery = None
                if isinstance(done.value, Path):
                    return done.value
                if not self._closed and self._turn and self._stdout_thread:
                    self._disable("rollout_missing")
                return None
        return None

    def _find_rollout(self) -> Generator[None, None, Path | None]:
        # UUIDv7 carries the native thread's creation date. It is only a
        # bounded directory hint; the file/header/turn checks still establish
        # ownership. Adjacent UTC dates cover the CLI's local-date partition.
        identifier = UUID(self._thread or "")
        scopes: list[tuple[list[Path], int]] = []
        if identifier.version == 7:
            born = datetime.fromtimestamp((identifier.int >> 80) / 1000, UTC)
            candidates = [self._root / (born + timedelta(days=offset)).strftime("%Y/%m/%d")
                          for offset in (-1, 0, 1)]
            scopes.append((candidates, 0))
        # Native fork copies may use UUIDv4 and a copy-date directory.
        scopes.append(([self._root], 3))
        for directories, max_depth in scopes:
            pending = [(directory, 0) for directory in directories if directory.exists()]
            found = None
            while pending:
                directory, depth = pending.pop()
                if directory.is_symlink() or any(parent.is_symlink() for parent in directory.parents
                                                  if parent != self._root.parent):
                    self._disable("rollout_directory_symlink")
                    return None
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if self._closed or self._disabled:
                            return None
                        if not entry.is_symlink():
                            if entry.is_dir(follow_symlinks=False) and depth < max_depth:
                                pending.append((Path(entry.path), depth + 1))
                            elif (entry.is_file(follow_symlinks=False)
                                  and entry.name.endswith(f"-{self._thread}.jsonl")):
                                if found is not None:
                                    self._disable("rollout_identity_ambiguous")
                                    return None
                                found = Path(entry.path)
                        yield None
            if found is not None:
                return found
        return None

    def _open(self) -> int | None:
        if self._path is None:
            self._path = self._discover()
        if self._path is None:
            return None
        # Open each component relative to captured directory descriptors. This
        # also rejects a parent replaced by a symlink between discovery/read.
        directory_fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            relative = self._path.relative_to(self._root)
            for component in relative.parts[:-1]:
                next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                  dir_fd=directory_fd)
                os.close(directory_fd)
                directory_fd = next_fd
            fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            os.close(fd)
            self._disable("rollout_not_regular")
            return None
        inode = (info.st_dev, info.st_ino)
        if self._inode is not None and (inode != self._inode or info.st_size < self._offset):
            os.close(fd)
            self._disable("rollout_replaced")
            return None
        if self._inode is None:
            raw_header = os.read(fd, _MAX_LINE)
            if b"\n" not in raw_header:
                os.close(fd)
                return None
            header = raw_header.split(b"\n", 1)[0]
            obj = self._decode_metadata(header)
            payload = obj.get("payload") if obj else None
            if not obj or obj.get("type") != "session_meta" or not isinstance(payload, dict) or payload.get("id") != self._thread:
                os.close(fd)
                self._disable("rollout_identity_mismatch")
                return None
            self._inode = inode
            self._file_verified = True
        os.lseek(fd, self._offset, os.SEEK_SET)
        return fd

    def _decode_metadata(self, line: bytes) -> dict[str, Any] | None:
        envelope = _METADATA.match(line)
        if envelope is None:
            if re.search(rb'"type"\s*:\s*"item_completed"', line):
                self._disable("activity_envelope")
            return None
        if envelope["type"] == b"response_item":
            return self._function_header(line, envelope.end(), envelope["timestamp"])
        if envelope["type"] == b"event_msg":
            # Neither chat-like messages nor reasoning bodies are decoded.
            suffix = line[envelope.end():]
            if not (_ITEM_COMPLETION.match(suffix) or _TURN_STARTED.match(suffix)):
                if re.search(rb'"type"\s*:\s*"item_completed"', suffix):
                    self._disable("activity_envelope")
                return None
        try:
            obj = json.loads(line)
        except (ValueError, UnicodeError):
            self._disable("activity_json")
            return None
        return obj if isinstance(obj, dict) else None

    def _function_header(self, line: bytes, offset: int, timestamp: bytes | None) -> dict[str, Any] | None:
        payload = _FUNCTION.match(line, offset)
        if payload is None or timestamp is None:
            return None
        # Walk the canonical scalar function header. The arguments string is
        # skipped lexically, never decoded, stored or interpreted as keys.
        position = payload.end()
        fields: dict[str, str] = {}
        while match := _HEADER_FIELD.match(line, position):
            key = json.loads(match["key"])
            if key in fields:
                return None
            if key in {"type", "name", "namespace", "call_id"}:
                value = json.loads(match["value"])
                if not isinstance(value, str):
                    return None
                fields[key] = value
            if fields.get("type") not in (None, "function_call"):
                return None
            if {"type", "name", "namespace", "call_id"} <= fields.keys():
                if fields["namespace"] != "collaboration" or fields["name"] not in _NATIVE_TOOLS:
                    return None
                return {**fields, "type": "function_header", "timestamp": json.loads(timestamp)}
            if match["end"] == b"}":
                return None
            position = match.end()
        return None

    def _record_header(self, obj: dict[str, Any]) -> None:
        if obj.get("type") == "event_msg" and obj.get("payload", {}).get("type") == "task_started":
            self._native_turn = _uuid(obj["payload"].get("turn_id"))
            return
        if (obj.get("type") != "function_header" or not self.identity_confirmed
                or self._native_turn != self._turn):
            return
        try:
            timestamp = datetime.fromisoformat(obj["timestamp"].replace("Z", "+00:00")).timestamp()
        except (AttributeError, ValueError, TypeError):
            return
        identifier = obj.get("call_id")
        if (not isinstance(identifier, str) or not identifier
                or timestamp < self._started or timestamp > self._wall() + 2):
            return
        name = obj["name"]
        previous = self._function_headers.get(identifier)
        if previous is not None and previous != name:
            self._disable("native_call_identity_conflict")
            return
        self._function_headers[identifier] = name

    def _observation(self, obj: dict[str, Any]) -> list[HarnessEvent]:
        payload = obj.get("payload")
        if (self._event_handler is None or not self.identity_confirmed
                or obj.get("type") != "event_msg" or not isinstance(payload, dict)
                or payload.get("type") != "item_completed"
                or payload.get("thread_id") != self._thread or payload.get("turn_id") != self._turn):
            return []
        item = payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "SubAgentActivity":
            return []
        identifier, kind = item.get("id"), item.get("kind")
        child = _uuid(item.get("agent_thread_id"))
        path = item.get("agent_path")
        started, completed = payload.get("started_at_ms"), payload.get("completed_at_ms")
        if (not isinstance(identifier, str) or not identifier or child is None
                or not isinstance(path, str) or not path.startswith("/root/")
                or kind not in {"started", "interacted", "completed", "interrupted"}
                or type(started) is not int or type(completed) is not int):
            self._disable("native_observation_schema")
            return []
        if (started / 1000 < self._started or completed < started
                or completed / 1000 > self._wall() + 2):
            return []
        key = (identifier, kind)
        previous_child = self._observed.get(key)
        if previous_child is not None:
            if previous_child != child:
                self._disable("native_call_identity_conflict")
            return []
        tool = self._function_headers.get(identifier)
        child_turn = None
        if kind == "completed":
            prefix = "subagent-completed-"
            child_turn = _uuid(identifier[len(prefix):]) if identifier.startswith(prefix) else None
            if child_turn is None:
                self._disable("native_completion_identity")
                return []
        elif kind == "started":
            if tool not in (None, "spawn_agent"):
                self._disable("native_call_identity_conflict")
                return []
            tool = "spawn_agent"
        elif kind == "interrupted":
            if tool != "interrupt_agent":
                return []
        elif tool not in {"followup_task", "send_message"}:
            return []
        self._observed[key] = child
        raw = {"native_observation": True, "native_kind": kind,
               "native_tool": tool, "native_child_turn_id": child_turn,
               "native_parent_turn_id": self._turn}
        events: list[HarnessEvent] = []
        if tool:
            events.extend([
                HarnessEvent(type="tool_use", tool_name=f"subagent:{tool}", tool_use_id=identifier,
                             tool_input={"agent_path": path, "native_thread_id": child}, raw=raw),
                HarnessEvent(type="tool_result", tool_use_id=identifier,
                             content=f"{'Started' if kind == 'started' else 'Accepted'} {path}", raw=raw),
            ])
        if tool != "send_message":
            status = "completed" if kind == "completed" else "interrupted" if kind == "interrupted" else "running"
            events.append(HarnessEvent(
                type="subagent", subagent=SubagentUpdate(
                    task_id=child, tool_use_id=identifier if tool else None,
                    status=status, name=path, description=f"{kind}: {path}",
                ), raw=raw,
            ))
        return events

    def _completion(self, obj: dict[str, Any]) -> float | None:
        payload = obj.get("payload")
        if obj.get("type") != "event_msg" or not isinstance(payload, dict) or payload.get("type") != "item_completed":
            return None
        if not self.identity_confirmed:
            return None
        if _uuid(payload.get("thread_id")) is None or _uuid(payload.get("turn_id")) is None:
            self._disable("activity_schema")
            return None
        if payload["thread_id"] != self._thread or payload["turn_id"] != self._turn:
            return None
        item = payload.get("item")
        if not isinstance(item, dict) or item.get("type") != "Reasoning":
            return None
        identifier = item.get("id")
        started, completed = payload.get("started_at_ms"), payload.get("completed_at_ms")
        if (not isinstance(identifier, str) or not identifier or len(identifier) > 256
                or type(started) is not int or type(completed) is not int):
            self._disable("activity_schema")
            return None
        if self._latest_completion_ms is not None and completed <= self._latest_completion_ms:
            return None
        if (started / 1000 < self._started or completed < started
                or completed / 1000 > self._wall() + 2):
            return None
        self._latest_completion_ms = completed
        return min(self._monotonic(), self._started_monotonic + completed / 1000 - self._started)

    def _discard_large_line(self, prefix: bytes) -> None:
        envelope = _METADATA.match(prefix)
        if (envelope is not None and envelope["type"] == b"event_msg"
                and _ITEM_COMPLETION.match(prefix[envelope.end():])):
            self._disable("activity_line_limit")

    def _read(self, *, baseline: bool = False, drain: bool = False) -> tuple[list[float], list[HarnessEvent]]:
        if self._closed or self._disabled:
            return [], []
        fd = None
        try:
            fd = self._open()
            if fd is None:
                return [], []
            if baseline:
                self._offset = os.lseek(fd, 0, os.SEEK_END)
                return [], []
            # Final convergence consumes only the already-written prefix;
            # appenders cannot prolong it. Ordinary polls retain their budget.
            boundary = os.fstat(fd).st_size if drain else self._offset + _MAX_READ
            ticks: list[float] = []
            events: list[HarnessEvent] = []
            while self._offset < boundary and not self._closed and not self._disabled:
                data = os.read(fd, min(_MAX_READ, boundary - self._offset))
                if not data:
                    break
                self._offset += len(data)
                pieces = (self._partial + data).split(b"\n")
                self._partial = pieces.pop()
                for line in pieces:
                    if self._skipping_line:
                        self._skipping_line = False
                        continue
                    if len(line) > _MAX_LINE:
                        self._discard_large_line(line[:512])
                        continue
                    obj = self._decode_metadata(line)
                    if obj is None:
                        continue
                    self._record_header(obj)
                    tick = self._completion(obj)
                    if tick is not None:
                        ticks.append(tick)
                    events.extend(self._observation(obj))
                if len(self._partial) > _MAX_LINE:
                    self._discard_large_line(self._partial[:512])
                    self._partial = b""
                    self._skipping_line = True
            return ticks, events
        except (OSError, ValueError):
            self._disable("rollout_read")
            return [], []
        finally:
            if fd is not None:
                os.close(fd)

    async def _read_owned(self, *, baseline: bool = False, drain: bool = False) -> tuple[list[float], list[HarnessEvent]]:
        async with self._cursor_lock:
            # A cancelled waiter never permits a second worker to advance the
            # same cursor while its shielded predecessor is still reading.
            if self._read_task is not None:
                await asyncio.shield(self._read_task)
            task = asyncio.create_task(asyncio.to_thread(self._read, baseline=baseline, drain=drain))
            self._read_task = task
            try:
                return await asyncio.shield(task)
            finally:
                if task.done() and self._read_task is task:
                    self._read_task = None

    async def start(self) -> None:
        # Existing threads start at a captured EOF before the child is spawned.
        if self._thread is not None:
            await self._read_owned(baseline=True)
        if not self._closed and not self._disabled:
            self._task = asyncio.create_task(self._watch(), name="codex-native-activity")

    async def poll_once(self) -> None:
        await self._poll()

    async def drain_observations(self) -> None:
        """Converge verified metadata to a finite EOF before normal retirement."""
        await self._poll(drain=True)

    async def _poll(self, *, drain: bool = False) -> None:
        try:
            ticks, events = await self._read_owned(drain=drain)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._disable("observer_failed")
            return
        for tick in ticks:
            if self._closed or self._disabled:
                return
            self.matched_records += 1
            self._notify(tick)
        if events and not self._closed and not self._disabled and self._event_handler is not None:
            try:
                self._event_handler(events)
            except Exception:
                self._disable("native_projection_failed")

    async def _watch(self) -> None:
        while not self._closed and not self._disabled:
            await self.poll_once()
            if self._wall() - self._started > 30 and (not self._turn or not self._stdout_thread):
                self._disable("identity_unconfirmed")
                return
            await asyncio.sleep(0.25)

    def invalidate(self) -> None:
        self._closed = True

    async def close(self) -> None:
        self.invalidate()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._read_task is not None:
            await asyncio.shield(self._read_task)
            self._read_task = None
        if self._discovery is not None:
            self._discovery.close()
            self._discovery = None


def diagnostic_environment(env: dict[str, str]) -> None:
    """A per-child override only; never write Codex or shell configuration."""
    env["RUST_LOG"] = _DIAGNOSTIC_ENV
