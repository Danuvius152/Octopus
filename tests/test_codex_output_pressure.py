"""Multiplexed handoff preserves necessary facts under disposable preview load."""

import json

import pytest

from server.harness import HarnessEvent, get_harness


def event(kind, content, source="root"):
    return HarnessEvent(type=kind, content=content, runtime_metadata={"source": source})


def assert_accounted(run):
    sizes = [len(json.dumps(value, ensure_ascii=True, default=vars)) for value, _ in run._event_queue]
    assert [size for _, size in run._event_queue] == sizes
    assert run._event_bytes == sum(sizes)


@pytest.mark.parametrize("limit", ["count", "bytes"])
async def test_necessary_handoff_reclaims_only_queued_previews_when_needed(monkeypatch, limit):
    from server.harness import run as run_module

    monkeypatch.setattr(run_module, "_EVENT_BUFFER_COUNT", 4 if limit == "count" else 256)
    # Four equal-sized events fit; a fifth does not, along either dimension.
    sample = event("text_delta", "p" * 100)
    sample_size = len(json.dumps(sample, ensure_ascii=True, default=vars))
    monkeypatch.setattr(run_module, "_EVENT_BUFFER_BYTES", 1024 * 1024 if limit == "count" else 4 * sample_size)
    run = get_harness("codex").create_run()
    values = [event("text", "a" * 100), sample, event("text", "b" * 100, "leaf"),
              event("text_delta", "q" * 100, "leaf")]
    try:
        for value in values:
            await run._emit(value)
            assert_accounted(run)
        # The necessary event that already fits did not opportunistically
        # erase previews. Incoming preview must never concatenate or fail.
        assert [value for value, _ in run._event_queue] == values
        await run._emit(event("text_delta", "discard" * 100))
        assert [value for value, _ in run._event_queue] == values
        assert run.collection_error is None
        assert_accounted(run)
        required = event("text", "c" * 100, "root")
        await run._emit(required)
        assert_accounted(run)
        stream = run.stream()
        observed = [await anext(stream) for _ in range(3)]
        assert [(value.content, value.runtime_metadata) for value in observed] == [
            (value.content, value.runtime_metadata) for value in (values[0], values[2], required)]
        assert run._event_bytes == 0 and not run._event_queue
        await stream.aclose()
    finally:
        await run.stop()


async def test_single_oversized_preview_yields_to_a_legal_oversized_necessary_fact(monkeypatch):
    from server.harness import run as run_module

    monkeypatch.setattr(run_module, "_EVENT_BUFFER_BYTES", 1024)
    run = get_harness("codex").create_run()
    try:
        await run._emit(event("text_delta", "discard" * 1000))
        assert not run._event_queue and run._event_bytes == 0 and run.collection_error is None
        required = event("text", "complete" * 1000)
        required.raw = {"retained": "诊断😀" * 1000}
        await run._emit(required)
        assert_accounted(run)
        assert run._event_bytes > 1024 and run.collection_error is None
        stream = run.stream()
        assert await anext(stream) is required
        assert run._event_bytes == 0
        await stream.aclose()
    finally:
        await run.stop()


async def test_reclaiming_previews_cannot_hide_a_genuine_necessary_overflow(monkeypatch):
    from server.harness import run as run_module

    first, second = event("text", "a" * 100), event("text", "b" * 100, "leaf")
    preview = event("text_delta", "p" * 100)
    budget = sum(len(json.dumps(value, default=vars)) for value in (first, second, preview))
    monkeypatch.setattr(run_module, "_EVENT_BUFFER_BYTES", budget)
    run = get_harness("codex").create_run()
    try:
        for value in (first, preview, second):
            await run._emit(value)
        await run._emit(event("text", "necessary" * 1000))
        assert run.collection_error.error_code == "native_output_overflow"
        assert [value for value, _ in run._event_queue] == [first, second]
        assert_accounted(run)
        await run.stop()
        accepted = [value async for value in run.stream()]
        assert [(value.type, value.content) for value in accepted[:2]] == [("text", "a" * 100), ("text", "b" * 100)]
        assert [value.type for value in accepted[2:]] == ["collection_error"]
        assert run._event_bytes == 0
    finally:
        await run.stop()
