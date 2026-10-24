"""Concurrent account writes preserve bytes, ownership and managed layout."""

import asyncio
from pathlib import Path

from server.harness import HarnessEvent
from server.large_prompts import LARGE_PROMPT_THRESHOLD_BYTES
from server.service_installation import read_service_profile
from server.users import UserManager
from server.workspace import paths_for
from tests.fake_run import FakeRunBase
from tests.test_account_file_layout import managed_files as managed_files
from tests.test_service_layout import account_installation as account_installation
from tests.test_service_layout import legacy_directories


async def test_concurrent_account_files_recreate_owned_directories_without_global_writes(managed_files, monkeypatch):
    config, _account, profile, _session, client, manager = managed_files
    assert LARGE_PROMPT_THRESHOLD_BYTES == 102400
    users = UserManager(manager.db)
    accounts = []
    for username in ("files-alice", "files-bob"):
        user = await users.create_user(username=username, password="owned-test-password")
        agent = await manager.db.get_system_agent(user["id"])
        assert agent is not None and agent["user_id"] == user["id"]
        session = await manager.create_session(agent["id"], name=username)
        assert session.user_id == user["id"]
        headers = {"Authorization": "Bearer " + await users.issue_token(user["id"])}
        root = Path(config.users_root) / user["id"]
        paths = paths_for(user["id"])
        assert paths.attachments == root / "attachments"
        assert paths.large_prompts == root / "large-prompts"
        # Account creation owns provisioning; each writer must also tolerate
        # its empty, reconstructible directory being removed afterwards.
        paths.attachments.rmdir()
        paths.large_prompts.rmdir()
        accounts.append((session, headers, root))

    legacy = legacy_directories(config)
    assert all(not root.exists() and not (root / ".octopus-installation").exists() for root in legacy)
    originals = {session.id: f"{session.user_id}\0原始附件字节".encode() for session, _headers, _root in accounts}
    uploads = await asyncio.gather(*(
        client.post(f"/api/sessions/{session.id}/attachments", headers=headers,
                    files={"file": ("original.bin", originals[session.id], "application/octet-stream")})
        for session, headers, _root in accounts
    ))
    assert all(response.status_code == 201 for response in uploads)

    received = {session.id: [] for session, _headers, _root in accounts}
    both_started = asyncio.Event()

    class RecordingRun(FakeRunBase):
        def __init__(self, session_id):
            self.session_id = session_id

        async def start(self, prompt, working_dir, resume_id=None, credential=None):
            received[self.session_id].append(prompt)
            if all(received.values()):
                both_started.set()

        def stream(self):
            async def events():
                # Keep both account executions alive before either completes.
                await both_started.wait()
                yield HarnessEvent(type="session_started", session_id=self.session_id)
                yield HarnessEvent(type="text", content="Complete")
                yield HarnessEvent(type="result", session_id=self.session_id, cost=0.0, num_turns=1)
            return events()

        async def stop(self):
            pass

        async def interrupt(self):
            pass

    monkeypatch.setattr(manager, "_make_run", lambda session, *_args: RecordingRun(session.id))
    prompts = {
        session.id: f"[bg-task-result] Account {session.user_id}\n"
        + "完整输入" * (LARGE_PROMPT_THRESHOLD_BYTES // 12 + 1)
        for session, _headers, _root in accounts
    }
    assert all(len(prompt.encode("utf-8")) > LARGE_PROMPT_THRESHOLD_BYTES for prompt in prompts.values())
    try:
        receipts = await asyncio.gather(*(
            manager.start_message(session.id, prompts[session.id]) for session, _headers, _root in accounts
        ))
        assert all(receipt.accepted for receipt in receipts)
        completions = await asyncio.wait_for(asyncio.gather(*(receipt.wait() for receipt in receipts)), 5)
        assert all(completion.outcome.status == "success" for completion in completions)
    finally:
        both_started.set()

    for index, ((session, headers, root), uploaded) in enumerate(zip(accounts, uploads, strict=True)):
        metadata = uploaded.json()
        stored = root / "attachments" / session.id / f"{metadata['id']}__original.bin"
        assert stored.read_bytes() == originals[session.id]
        downloaded = await client.get(f"/api/sessions/{session.id}/attachments/{metadata['id']}", headers=headers)
        assert downloaded.status_code == 200 and downloaded.content == originals[session.id]
        rows = await manager.db.load_messages(session.id)
        assert [row["content"] for row in rows if row["role"] == "user"] == [prompts[session.id]]
        spills = list((root / "large-prompts" / session.id).glob("*.txt"))
        assert len(spills) == 1 and spills[0].read_text(encoding="utf-8") == prompts[session.id]
        assert len(received[session.id]) == 1
        pointer = received[session.id][0]
        assert pointer.startswith("[bg-task-result] [octopus-large-prompt]")
        assert len(pointer.encode("utf-8")) < LARGE_PROMPT_THRESHOLD_BYTES
        assert str(spills[0]) in pointer
        other_session, other_headers, other_root = accounts[1 - index]
        assert str(other_root) not in pointer
        assert not (other_root / "attachments" / session.id).exists()
        assert not (other_root / "large-prompts" / session.id).exists()
        assert (await client.get(f"/api/sessions/{session.id}/attachments/{metadata['id']}",
                                 headers=other_headers)).status_code == 404
        assert (await client.get(f"/api/sessions/{other_session.id}/attachments/{metadata['id']}",
                                 headers=other_headers)).status_code == 404
        assert (await client.post(f"/api/sessions/{session.id}/attachments", headers=other_headers,
                                  files={"file": ("forbidden.bin", b"forbidden", "application/octet-stream")})).status_code == 404

    assert all(not root.exists() and not (root / ".octopus-installation").exists() for root in legacy)
    assert read_service_profile(config) == profile
