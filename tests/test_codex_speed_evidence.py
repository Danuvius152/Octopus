"""Hermetic regressions for the actual speed-resume test oracle."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from server.context_models import ContextObservation, ContextReading, ContextSource
from server.harness import HarnessEvent
from tests.codex_speed_evidence import (
    RolloutSnapshot,
    capture_rollout,
    record_phase,
    successful_thread,
    verify_resume,
)

THREAD = "speed-thread"
CWD = Path("/tmp/speed-test")
SOURCE = Path("/synthetic/rollout.jsonl")


def test_phase_evidence_preserves_typed_context_observations_as_json(tmp_path):
    observation = ContextObservation(backend="codex", native_session_id=THREAD, actual_model="model",
        source=ContextSource(request_id="request", attempt_id="attempt", connection_generation="connection",
            native_turn_id="turn", observation_seq=1), kind="sample",
        reading=ContextReading(used_tokens=250, capacity_tokens=1000, basis="codex_last_total"))
    record_phase(tmp_path, "sample", snapshot(initial()), [HarnessEvent(type="context_observation", context=observation)])
    evidence = json.loads((tmp_path / "sample.json").read_text())
    assert evidence["events"][0]["context"]["source"]["request_id"] == "request"
    assert evidence["events"][0]["context"]["reading"]["used_tokens"] == 250


def event(kind, **payload):
    return {"type": "event_msg", "payload": {"type": kind, **payload}}


def applied(tier="priority", effort="high"):
    return event("thread_settings_applied", thread_settings={"service_tier": tier, "reasoning_effort": effort})


def turn(turn_id, tier="priority", effort="high", repetitions=1):
    return [
        *[applied(tier, effort) for _ in range(repetitions)],
        event("task_started", turn_id=turn_id),
        {"type": "turn_context", "payload": {"turn_id": turn_id, "effort": effort}},
        event("task_complete", turn_id=turn_id),
    ]


def initial(with_settings=True):
    rows = [{"type": "session_meta", "payload": {"id": THREAD, "cwd": str(CWD)}}]
    rows += turn("initial", "default", "low") if with_settings else turn("initial", "default", "low")[1:]
    return rows


def snapshot(rows):
    return RolloutSnapshot(SOURCE, b"".join((json.dumps(row) + "\n").encode() for row in rows))


def check(before, after, tier="priority", effort="high"):
    return verify_resume(before, after, thread_id=THREAD, cwd=CWD, service_tier=tier, effort=effort)


@pytest.mark.parametrize("with_settings", [False, True])
@pytest.mark.parametrize("repetitions", [1, 3])
def test_initial_settings_and_duplicate_resume_settings_keep_separate_boundaries(with_settings, repetitions):
    a_rows = initial(with_settings)
    b_rows = a_rows + turn("fast", repetitions=repetitions)
    c_rows = b_rows + turn("normal", "default", "low", repetitions=repetitions)
    a, b, c = map(snapshot, [a_rows, b_rows, c_rows])
    fast = check(a, b)
    normal = check(b, c, "default", "low")
    assert fast["applied_events"] == normal["applied_events"] == repetitions
    assert fast["byte_start"] == len(a.raw) and fast["byte_end"] == normal["byte_start"] == len(b.raw)
    assert fast["turn_id"] == "fast" and normal["turn_id"] == "normal"


@pytest.mark.parametrize("defect", [
    "wrong-tier", "wrong-effort", "missing-tier", "missing-effort", "no-settings",
    "extra-change", "reverse-change", "missing-start", "missing-end", "wrong-end",
    "reversed-turn", "reused-turn", "extra-turn", "no-context", "wrong-context-effort",
    "missing-context-effort", "foreign-context", "extra-foreign-context", "context-before-start",
    "context-after-end", "contradicting-context-tier", "late-settings", "foreign-settings-turn",
    "foreign-thread",
])
def test_resume_rejects_missing_wrong_or_misattributed_evidence(defect):
    rows = turn("fast")
    if defect == "wrong-tier":
        rows[0] = applied("default")
    elif defect == "wrong-effort":
        rows[0] = applied(effort="low")
    elif defect in {"missing-tier", "missing-effort"}:
        rows[0]["payload"]["thread_settings"].pop("service_tier" if defect == "missing-tier" else "reasoning_effort")
    elif defect == "no-settings":
        rows.pop(0)
    elif defect == "extra-change":
        rows.insert(1, applied("default", "low"))
        rows.insert(2, applied())
    elif defect == "reverse-change":
        rows.insert(0, applied("default", "low"))
    elif defect == "missing-start":
        rows.pop(1)
    elif defect == "missing-end":
        rows.pop()
    elif defect == "wrong-end":
        rows[-1]["payload"]["turn_id"] = "foreign"
    elif defect == "reversed-turn":
        rows[1], rows[-1] = rows[-1], rows[1]
    elif defect == "reused-turn":
        rows = turn("initial")
    elif defect == "extra-turn":
        rows += turn("extra")
    elif defect == "no-context":
        rows.pop(2)
    elif defect == "wrong-context-effort":
        rows[2]["payload"]["effort"] = "low"
    elif defect == "missing-context-effort":
        rows[2]["payload"].pop("effort")
    elif defect == "foreign-context":
        rows[2]["payload"]["turn_id"] = "foreign"
    elif defect == "extra-foreign-context":
        extra = deepcopy(rows[2])
        extra["payload"]["turn_id"] = "foreign"
        rows.insert(3, extra)
    elif defect == "context-before-start":
        rows.insert(1, rows.pop(2))
    elif defect == "context-after-end":
        rows.append(rows.pop(2))
    elif defect == "contradicting-context-tier":
        rows[2]["payload"]["service_tier"] = "default"
    elif defect == "late-settings":
        rows.append(rows.pop(0))
    elif defect == "foreign-settings-turn":
        rows[0]["payload"]["turn_id"] = "foreign"
    elif defect == "foreign-thread":
        rows[1]["payload"]["thread_id"] = "foreign"
    a_rows = initial()
    with pytest.raises(AssertionError):
        check(snapshot(a_rows), snapshot(a_rows + rows))


def test_correct_combined_changes_cannot_hide_settings_in_the_wrong_resume():
    a_rows = initial()
    fast_without_settings = turn("fast")[1:]
    b_rows = a_rows + fast_without_settings
    c_rows = b_rows + [applied()] + turn("normal", "default", "low")
    with pytest.raises(AssertionError, match="wrong settings"):
        check(snapshot(a_rows), snapshot(b_rows))
    with pytest.raises(AssertionError, match="wrong settings"):
        check(snapshot(b_rows), snapshot(c_rows), "default", "low")


@pytest.mark.parametrize("defect", ["rewrite", "truncate", "unterminated", "bad-json", "non-object", "file-change", "thread-change", "cwd-change"])
def test_snapshot_integrity_and_identity_are_required(defect):
    a = snapshot(initial())
    b = snapshot(initial() + turn("fast"))
    if defect == "rewrite":
        b = RolloutSnapshot(SOURCE, b.raw.replace(b'"low"', b'"bad"', 1))
    elif defect == "truncate":
        b = RolloutSnapshot(SOURCE, a.raw[:-1])
    elif defect == "unterminated":
        b = RolloutSnapshot(SOURCE, b.raw[:-1])
    elif defect == "bad-json":
        b = RolloutSnapshot(SOURCE, b.raw + b"{broken}\n")
    elif defect == "non-object":
        b = RolloutSnapshot(SOURCE, b.raw + b"[]\n")
    elif defect == "file-change":
        b = RolloutSnapshot(Path("/other/rollout.jsonl"), b.raw)
    else:
        rows = initial()
        rows[0]["payload"]["id" if defect == "thread-change" else "cwd"] = "foreign"
        a, b = snapshot(rows), snapshot(rows + turn("fast"))
    with pytest.raises(AssertionError):
        check(a, b)


@pytest.mark.parametrize("defect", [None, "missing", "wrong-id", "wrong-cwd", "duplicate", "incomplete"])
def test_capture_requires_unique_matching_meta_and_complete_bytes(tmp_path, defect):
    path = tmp_path / "rollout-unrelated-filename.jsonl"
    rows = initial()
    if defect == "wrong-id":
        rows[0]["payload"]["id"] = "foreign"
    elif defect == "wrong-cwd":
        rows[0]["payload"]["cwd"] = "/foreign"
    raw = snapshot(rows).raw
    if defect != "missing":
        path.write_bytes(raw[:-1] if defect == "incomplete" else raw)
    if defect == "duplicate":
        (tmp_path / "rollout-another.jsonl").write_bytes(raw)
    if defect is None:
        assert capture_rollout(tmp_path, THREAD, CWD) == RolloutSnapshot(path, raw)
    else:
        with pytest.raises(AssertionError):
            capture_rollout(tmp_path, THREAD, CWD)


@pytest.mark.parametrize("defect", [None, "retry-diagnostic", "retry-without-evidence", "retry-then-failure", "collection-gap", "missing-result", "duplicate-result", "failed-result", "error-event", "wrong-thread", "missing-thread", "lost-memory"])
def test_successful_same_thread_and_recall_are_required(defect):
    result = HarnessEvent(type="result", session_id=THREAD)
    events = [HarnessEvent(type="text", content="MARIGOLD"), result]
    if defect == "missing-result":
        events.pop()
    elif defect == "duplicate-result":
        events.append(result)
    elif defect == "failed-result":
        result.is_error = True
    elif defect == "error-event":
        events.append(HarnessEvent(type="error"))
    elif defect == "collection-gap":
        events.insert(0, HarnessEvent(type="collection_error", error_code="native_collection_incomplete"))
    elif defect in {"retry-diagnostic", "retry-without-evidence", "retry-then-failure"}:
        events.insert(0, HarnessEvent(type="error", error_code="native_provider_retry",
                                     content="Reconnecting...", error_details={"willRetry": defect != "retry-without-evidence"}))
        result.is_error = defect == "retry-then-failure"
    elif defect == "wrong-thread":
        result.session_id = "foreign"
    elif defect == "missing-thread":
        result.session_id = None
    elif defect == "lost-memory":
        events[0].content = "forgotten"
    if defect in {None, "retry-diagnostic"}:
        assert successful_thread(events, expected_thread=THREAD, recalled_word="MARIGOLD") == THREAD
    else:
        with pytest.raises(AssertionError):
            successful_thread(events, expected_thread=THREAD, recalled_word="MARIGOLD")
