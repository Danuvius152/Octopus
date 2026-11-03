"""Test-only checks for settings applied in each resumed Codex turn."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from server.harness import HarnessEvent


def _records(raw: bytes) -> list[dict]:
    assert raw and raw.endswith(b"\n"), "rollout must end at a complete JSONL boundary"
    try:
        rows = [json.loads(line) for line in raw.decode("utf-8").split("\n")[:-1]]
    except (UnicodeError, ValueError) as error:
        raise AssertionError("invalid rollout JSONL") from error
    assert all(isinstance(row, dict) for row in rows), "rollout records must be objects"
    return rows


def _identified_records(raw: bytes, thread_id: str, cwd: Path) -> list[dict]:
    rows = _records(raw)
    assert rows[0].get("type") == "session_meta", "missing initial session identity"
    assert sum(row.get("type") == "session_meta" for row in rows) == 1
    meta = rows[0]["payload"]
    assert meta.get("id") == thread_id, "rollout belongs to another thread"
    assert meta.get("cwd") == str(cwd), "rollout belongs to another working directory"
    return rows


@dataclass(frozen=True)
class RolloutSnapshot:
    path: Path
    raw: bytes


def capture_rollout(sessions: Path, thread_id: str, cwd: Path) -> RolloutSnapshot:
    matches = []
    # Inspect only headers of other sessions; filenames are not identity proof.
    for path in sessions.rglob("rollout-*.jsonl"):
        try:
            with path.open("rb") as stream:
                header = json.loads(stream.readline())
        except (OSError, ValueError):
            continue
        meta = header.get("payload") if isinstance(header, dict) else None
        if isinstance(meta, dict) and header.get("type") == "session_meta" and meta.get("id") == thread_id:
            matches.append(path)
    assert len(matches) == 1, f"expected one rollout for this thread, found {len(matches)}"
    snapshot = RolloutSnapshot(matches[0], matches[0].read_bytes())
    _identified_records(snapshot.raw, thread_id, cwd)
    return snapshot


def successful_thread(
    events: list[HarnessEvent], *, expected_thread: str | None = None,
    recalled_word: str | None = None,
) -> str:
    errors = [event for event in events if event.type == "collection_error" or (event.type == "error" and not (
        event.error_code == "native_provider_retry" and (event.error_details or {}).get("willRetry") is True))]
    assert not errors, [(event.error_code, event.content, event.error_details) for event in errors]
    results = [event for event in events if event.type == "result"]
    assert len(results) == 1, "turn must have exactly one result"
    result = results[0]
    assert result.is_error is False, "turn did not complete successfully"
    assert isinstance(result.session_id, str) and result.session_id, "missing resume identity"
    if expected_thread is not None:
        assert result.session_id == expected_thread, "resume changed thread identity"
    if recalled_word is not None:
        text = "".join(event.content or "" for event in events if event.type == "text")
        assert recalled_word.upper() in text.upper(), f"resume did not recall {recalled_word}"
    return result.session_id


def verify_resume(
    before: RolloutSnapshot, after: RolloutSnapshot, *, thread_id: str,
    cwd: Path, service_tier: str, effort: str,
) -> dict:
    assert after.path == before.path, "resume changed rollout file"
    assert after.raw.startswith(before.raw), "resume rewrote or truncated the previous prefix"
    previous = _identified_records(before.raw, thread_id, cwd)
    current = _identified_records(after.raw, thread_id, cwd)
    appended = current[len(previous):]
    starts = [(i, row["payload"]) for i, row in enumerate(appended)
              if row.get("type") == "event_msg" and row.get("payload", {}).get("type") == "task_started"]
    ends = [(i, row["payload"]) for i, row in enumerate(appended)
            if row.get("type") == "event_msg" and row.get("payload", {}).get("type") == "task_complete"]
    assert len(starts) == len(ends) == 1, "resume interval must contain one complete turn"
    start, begin = starts[0]
    end, finish = ends[0]
    turn_id = begin.get("turn_id")
    assert isinstance(turn_id, str) and turn_id and finish.get("turn_id") == turn_id
    assert start < end, "turn completed before it started"
    old_turns = {row.get("payload", {}).get("turn_id") for row in previous}
    assert turn_id not in old_turns, "resume reused an earlier turn"
    contexts = [(i, row["payload"]) for i, row in enumerate(appended) if row.get("type") == "turn_context"]
    assert contexts, "missing actual resumed turn context"
    for position, context in contexts:
        assert start < position < end, "context is outside the resumed turn"
        assert context.get("turn_id") == turn_id, "context belongs to another turn"
        assert context.get("effort") == effort, "actual turn effort does not match"
        assert context.get("service_tier") in (None, service_tier), "context contradicts applied tier"
    applied = [(i, row["payload"]) for i, row in enumerate(appended)
               if row.get("type") == "event_msg" and row.get("payload", {}).get("type") == "thread_settings_applied"]
    changes = []
    for position, event in applied:
        assert position < end, "settings arrived after the resumed turn completed"
        assert event.get("turn_id") in (None, turn_id), "settings belong to another turn"
        settings = event.get("thread_settings", {})
        setting = (settings.get("service_tier"), settings.get("reasoning_effort"))
        if not changes or changes[-1] != setting:
            changes.append(setting)
    assert changes == [(service_tier, effort)], f"wrong settings within this resume interval: {changes}"
    for _, payload in [*starts, *ends, *contexts, *applied]:
        assert payload.get("thread_id") in (None, thread_id), "event belongs to another thread"
    return {
        "thread_id": thread_id, "turn_id": turn_id,
        "byte_start": len(before.raw), "byte_end": len(after.raw),
        "record_start": len(previous), "record_end": len(current),
        "applied_events": len(applied), "settings_changes": changes,
        "context_efforts": [context["effort"] for _, context in contexts],
    }


def record_phase(directory: Path, label: str, snapshot: RolloutSnapshot, events: list[HarnessEvent]) -> None:
    """Retain the exact prefix and normalized stream for independent review."""
    directory.mkdir(mode=0o700, exist_ok=True)
    with (directory / f"{label}.jsonl").open("xb") as stream:
        stream.write(snapshot.raw)
    (directory / f"{label}.jsonl").chmod(0o600)
    evidence = {
        "source_path": str(snapshot.path), "bytes": len(snapshot.raw),
        "sha256": hashlib.sha256(snapshot.raw).hexdigest(),
        "events": [asdict(event) for event in events],
    }
    (directory / f"{label}.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    (directory / f"{label}.json").chmod(0o600)
