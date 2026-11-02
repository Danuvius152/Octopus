"""Native images retain original path and bounded references, never a media copy."""

import base64
import json

import pytest

from tests.native_app_server import ROOT, inject, item, start_output, terminal
from tests.native_app_server import native_runtime as native_runtime
from tests.session_runtime import wait_dispatch_idle


def image_frame(payload, stage="completed"):
    return {"method": f"item/{stage}", "params": {"threadId": ROOT, "turnId": "turn-1", "item": payload}}


@pytest.mark.parametrize("reference", [{"fileId": "native-file", "result": ""}, {"result": "aW1hZ2U="}])
@pytest.mark.parametrize("status,path", [("completed", True), ("completed", False), ("failed", False)])
@pytest.mark.parametrize("complete_only", [False, True])
async def test_image_generation_keeps_save_availability_distinct_from_provider_success(native_runtime, tmp_path, reference, status, path, complete_only):
    manager, database, session, _, _, events = native_runtime
    receipt = await start_output(native_runtime)
    saved_path = str(tmp_path / "native-output.png")
    payload = {"id": "image", "type": "imageGeneration", "status": status, "revisedPrompt": "An image",
               "transparentBackground": True, "inputImages": [{"path": "/original/input.png", "sha256": "a" * 64}],
               "generationId": "generation-1", "failure": {"type": "usageLimitExceeded", "limitId": "image"} if status == "failed" else None,
               **reference, **({"savedPath": saved_path} if path else {})}
    start = {**payload, "status": "in_progress", "result": ""}
    if not complete_only:
        inject(tmp_path, image_frame(start, "started"))
    inject(tmp_path, image_frame(payload), image_frame(payload),
           item(ROOT, "turn-1", "final", "Final answer"), terminal())
    await wait_dispatch_idle(manager, session.id)
    rows = [row for row in await database.load_messages(session.id) if row["type"] == "tool_result"]
    assert len(rows) == 1
    row = rows[0]
    recorded = row["tool_input"]["native_item"]
    assert recorded["status"] == status and recorded["failure"] == payload["failure"]
    assert recorded["inputImages"] == payload["inputImages"]
    assert recorded["generationId"] == "generation-1" and recorded["transparentBackground"] is True
    assert row["is_error"] == (status == "failed")
    if path:
        assert recorded["savedPath"] == saved_path and saved_path in row["content"]
    elif status == "completed":
        assert "local file unavailable" in row["content"].lower() and "savedPath" not in recorded
    assert "result" not in recorded
    if "fileId" in reference:
        assert recorded["fileId"] == "native-file" and "inlineImage" not in recorded
    else:
        assert recorded["inlineImage"]["bytes"] == 5
    assert next(event for event in events if event.get("type") == "tool_result")["tool_input"] == row["tool_input"]
    result = await database.read_session_result(session.id, receipt["request_id"])
    assert result.outcome.status == "success" and result.fragments == ("Final answer",)


async def test_inline_image_larger_than_the_text_frame_limit_is_projected_before_recording(native_runtime, tmp_path):
    manager, database, session, _, _, events = native_runtime
    receipt = await start_output(native_runtime)
    encoded = base64.b64encode(b"image-bytes" * 400000).decode()
    assert len(encoded) > 4 * 1024 * 1024
    payload = {"id": "large", "type": "imageGeneration", "status": "completed", "result": encoded}
    inject(tmp_path, image_frame(payload), item(ROOT, "turn-1", "final", "Large image completed"), terminal())
    await wait_dispatch_idle(manager, session.id)
    result = await database.read_session_result(session.id, receipt["request_id"])
    assert result.outcome.status == "success"
    rows = await database.load_messages(session.id)
    assert len(json.dumps(rows)) < 20000 and len(json.dumps(events)) < 50000
    assert not list(tmp_path.glob("*.png"))


@pytest.mark.parametrize("size,accepted", [(24, True), (25, False)])
async def test_inline_image_budget_is_explicit_and_preserves_the_valid_prefix(native_runtime, tmp_path, monkeypatch, size, accepted):
    from server.harness import codex_items

    manager, database, session, _, _, _ = native_runtime
    monkeypatch.setattr(codex_items, "IMAGE_BYTES", 24)
    receipt = await start_output(native_runtime)
    inject(tmp_path, item(ROOT, "turn-1", "prefix", "Valid prefix"), image_frame({
        "id": "boundary", "type": "imageGeneration", "status": "completed", "result": base64.b64encode(b"x" * size).decode(),
    }), terminal())
    await wait_dispatch_idle(manager, session.id)
    result = await database.read_session_result(session.id, receipt["request_id"])
    assert result.outcome.status == ("success" if accepted else "failed")
    assert result.fragments == ("Valid prefix",)
    if not accepted:
        assert "budget" in result.outcome.message.lower()


async def test_child_image_keeps_original_path_when_the_current_file_changes_or_disappears(native_runtime, tmp_path):
    from server.file_viewer import (
        MAX_FILE_BYTES,
        FileNotFound,
        FileTooLarge,
        resolve_safe_path,
    )
    from tests.native_app_server import child_end, child_start

    manager, database, session, _, _, _ = native_runtime
    receipt = await start_output(native_runtime)
    path = tmp_path / "original-native.png"
    path.write_bytes(b"initial image")
    frame = image_frame({"id": "child-image", "type": "imageGeneration", "status": "completed",
                         "fileId": "native-reference", "savedPath": str(path)})
    frame["params"].update(threadId="child", turnId="child-turn")
    inject(tmp_path, child_start(), frame, child_end(), item(ROOT, "turn-1", "final", "Root answer"), terminal())
    await wait_dispatch_idle(manager, session.id)
    result = await database.read_session_result(session.id, receipt["request_id"])
    assert result.outcome.status == "success" and result.fragments == ("Root answer",)
    row = next(row for row in await database.load_messages(session.id) if row["type"] == "tool_result")
    assert row["runtime_metadata"]["native"]["thread_id"] == "child"
    assert resolve_safe_path(tmp_path, str(path)).size == len(b"initial image")
    path.write_bytes(b"current changed image")
    assert resolve_safe_path(tmp_path, str(path)).abs_path.read_bytes() == b"current changed image"
    path.write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    with pytest.raises(FileTooLarge):
        resolve_safe_path(tmp_path, str(path))
    path.unlink()
    with pytest.raises(FileNotFound):
        resolve_safe_path(tmp_path, str(path))
    assert not list(tmp_path.glob("*.png"))
    assert next(row for row in await database.load_messages(session.id) if row["type"] == "tool_result")["tool_input"] == row["tool_input"]
