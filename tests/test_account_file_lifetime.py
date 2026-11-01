"""Current file references outlive the request or session that first made them."""

import asyncio
import json
import threading
from pathlib import Path

import pytest

from server.large_prompts import LARGE_PROMPT_THRESHOLD_BYTES, spill_if_large
from server.routers import attachments as attachment_routes
from tests.test_account_file_layout import managed_files as managed_files
from tests.test_service_layout import account_installation as account_installation


@pytest.mark.parametrize("resource", ["attachment", "spill"])
async def test_native_fork_original_file_pointers_survive_parent_deletion(managed_files, resource):
    _config, account, _profile, parent, client, manager = managed_files
    uploaded = await client.post(f"/api/sessions/{parent.id}/attachments",
                                 files={"file": ("retained.txt", b"Native attachment bytes", "text/plain")})
    assert uploaded.status_code == 201
    metadata = uploaded.json()
    attachment = account / "attachments" / parent.id / f"{metadata['id']}__retained.txt"
    original_prompt = "[bg-task-result] " + "原始完整输入" * (LARGE_PROMPT_THRESHOLD_BYTES // 18 + 1)
    pointer = spill_if_large(parent.id, original_prompt, user_id=parent.user_id)
    spills = list((account / "large-prompts" / parent.id).glob("*.txt"))
    assert len(spills) == 1 and str(spills[0]) in pointer
    await manager.db.append_message(parent.id, 0, "user", "text", "Original attachment",
                                    attachments=[metadata])
    native_text = f"Read the original file at {attachment}.\n{pointer}"
    home = account / "provider-transcripts"
    native_directory = home / "sessions" / "2026" / "10" / "24"
    native_directory.mkdir(parents=True)
    records = [{"type": "session_meta", "payload": {"id": "native-parent", "cwd": parent.working_dir}},
               {"type": "response_item", "payload": {"text": native_text}}]
    (native_directory / "rollout-2026-10-24T00-00-00-native-parent.jsonl").write_text(
        "\n".join(json.dumps(record) for record in records) + "\n")
    # Retained provider history is the input to the file-lifetime contract.
    # Actual current native creation is exercised by the owned fork saga tests.
    records[0]["payload"]["id"] = "native-child"
    (native_directory / "rollout-2026-10-24T00-00-00-native-child.jsonl").write_text(
        "\n".join(json.dumps(record) for record in records) + "\n")
    await manager.db.create_fork_session(
        fork_id="nativechild001", name="Native fork", working_dir=parent.working_dir,
        created_at=parent.created_at, parent_id=parent.id, backend="codex", user_id=parent.user_id,
        agent_id=parent.agent_id, credential_id=None, resume_id="native-child", fork_after_seq=0,
    )
    await manager.db.update_session_field("nativechild001", fork_status="ready", archived=True)
    copied = list((home / "sessions").glob("**/*native-child.jsonl"))
    assert len(copied) == 1
    before = copied[0].read_bytes()
    copied_records = [json.loads(line) for line in before.splitlines()]
    assert copied_records[0]["payload"]["id"] == "native-child"
    assert copied_records[1]["payload"]["text"] == native_text
    expected = attachment.read_bytes() if resource == "attachment" else original_prompt.encode("utf-8")
    source = attachment if resource == "attachment" else spills[0]
    assert await manager.delete_session(parent.id)
    assert await manager.db.get_session_owner(parent.id) is None
    assert copied[0].read_bytes() == before
    assert source.read_bytes() == expected
    restored = type(manager)()
    await restored.initialize(manager.db)
    assert source.read_bytes() == expected
    assert await manager.db.get_session_owner("nativechild001") == parent.user_id
    response = await client.get(f"/api/sessions/nativechild001/attachments/{metadata['id']}")
    assert response.status_code == 200 and response.content == b"Native attachment bytes"


async def test_delete_refuses_an_original_fork_reservation_until_copy_finishes(managed_files, monkeypatch):
    _config, account, _profile, parent, client, manager = managed_files
    await manager.db.append_message(parent.id, 0, "user", "text", "The complete original history")
    before = await manager.db.load_messages(parent.id)
    (account / "workspace" / "original.txt").write_bytes(b"Complete workspace")
    monkeypatch.setattr(manager, "_fork_copy_base", lambda: str(account / "forks"))
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    copy_tree = manager._copy_tree

    def copying(source, destination):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5), "The original fork copy was not released"
        copy_tree(source, destination)

    monkeypatch.setattr(manager, "_copy_tree", copying)
    creation = asyncio.create_task(client.post(f"/api/sessions/{parent.id}/duplicate", json={}))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert await manager.db.load_incomplete_forks() == []
        refused = await client.delete(f"/api/sessions/{parent.id}")
        assert refused.status_code == 409, "Parent deletion must refuse the original fork reservation"
        assert manager.get_session(parent.id, parent.user_id) is parent
        assert await manager.db.load_messages(parent.id) == before
        assert not parent._admission_closed and not parent._dispatch_paused
        release.set()
        created = await asyncio.wait_for(creation, 3)
        assert created.status_code == 201, created.text
        child = created.json()
        assert await manager.db.load_messages(child["id"]) == before
        assert (Path(child["working_dir"]) / "original.txt").read_bytes() == b"Complete workspace"
        assert (await client.delete(f"/api/sessions/{parent.id}")).status_code == 204
        assert await manager.db.load_messages(child["id"]) == before
    finally:
        release.set()
        await asyncio.gather(creation, return_exceptions=True)


async def test_authorized_download_can_open_its_original_path_after_parent_delete(managed_files, monkeypatch):
    _config, _account, _profile, parent, client, _manager = managed_files
    original = b"The accepted download still owns these complete bytes"
    uploaded = await client.post(f"/api/sessions/{parent.id}/attachments",
                                 files={"file": ("download.txt", original, "text/plain")})
    assert uploaded.status_code == 201
    entered, release = asyncio.Event(), asyncio.Event()
    response_type = attachment_routes.FileResponse

    class HeldResponse(response_type):
        async def __call__(self, scope, receive, send):
            entered.set()
            await release.wait()
            await super().__call__(scope, receive, send)

    monkeypatch.setattr(attachment_routes, "FileResponse", HeldResponse)
    downloading = asyncio.create_task(client.get(
        f"/api/sessions/{parent.id}/attachments/{uploaded.json()['id']}"))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert (await client.delete(f"/api/sessions/{parent.id}")).status_code == 204
        release.set()
        downloaded = await asyncio.wait_for(downloading, 3)
        assert downloaded.status_code == 200 and downloaded.content == original
        assert (await client.get(f"/api/sessions/{parent.id}/attachments/{uploaded.json()['id']}")).status_code == 404
    finally:
        release.set()
        await asyncio.gather(downloading, return_exceptions=True)
