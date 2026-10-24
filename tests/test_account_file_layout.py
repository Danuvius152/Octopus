"""Normal account file writes must remain valid managed-installation data."""

import asyncio

import httpx
import pytest
from fastapi import FastAPI

from server import attachments as attachment_files
from server import deps
from server.config import settings
from server.database import Database
from server.harness import HarnessEvent
from server.large_prompts import LARGE_PROMPT_THRESHOLD_BYTES
from server.routers.attachments import router as attachments_router
from server.routers.sessions import router as sessions_router
from server.service_installation import configure_service, read_service_profile
from server.sessions import SessionManager
from server.users import UserManager
from tests.fake_run import FakeRunBase
from tests.test_service_layout import account_installation as account_installation


@pytest.fixture
async def managed_files(account_installation, monkeypatch):
    config, account = account_installation
    for name, value in config.model_dump().items():
        monkeypatch.setattr(settings, name, value)
    profile = configure_service(config, unit="octopus-layout-test.service", manager="user")
    assert read_service_profile(config) == profile
    database = Database(config.db_path)
    await database.initialize()
    users = UserManager(database)
    monkeypatch.setattr(deps, "_user_manager", users)
    manager = SessionManager()
    await manager.initialize(database)
    agent = await database.get_system_agent("owner")
    assert agent is not None
    session = await manager.create_session(agent["id"], name="Owned files", working_dir=str(account / "workspace"))
    assert session.user_id == "owner"
    app = FastAPI()
    app.include_router(attachments_router)
    app.include_router(sessions_router)
    app.dependency_overrides[deps.get_session_manager] = lambda: manager
    headers = {"Authorization": "Bearer " + await users.issue_token("owner")}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers) as client:
            yield config, account, profile, session, client, manager
    finally:
        await database.close()


async def test_first_account_upload_keeps_managed_installation_restartable(managed_files):
    config, account, profile, session, client, _manager = managed_files
    original = b"The complete account attachment"
    uploaded = await client.post(f"/api/sessions/{session.id}/attachments",
                                 files={"file": ("original.txt", original, "text/plain")})
    assert uploaded.status_code == 201
    identifier = uploaded.json()["id"]
    downloaded = await client.get(f"/api/sessions/{session.id}/attachments/{identifier}")
    assert downloaded.status_code == 200 and downloaded.content == original
    assert read_service_profile(config) == profile
    stored = account / "attachments" / session.id / f"{identifier}__original.txt"
    assert stored.read_bytes() == original


async def test_first_account_spill_keeps_managed_installation_restartable(managed_files, monkeypatch):
    config, account, profile, session, _client, manager = managed_files
    received = []

    class RecordingRun(FakeRunBase):
        async def start(self, prompt, working_dir, resume_id=None, credential=None):
            received.append(prompt)

        def stream(self):
            async def events():
                yield HarnessEvent(type="session_started", session_id="account-spill")
                yield HarnessEvent(type="text", content="Complete")
                yield HarnessEvent(type="result", session_id="account-spill", cost=0.0, num_turns=1)
            return events()

        async def stop(self):
            pass

        async def interrupt(self):
            pass

    monkeypatch.setattr(manager, "_make_run", lambda *args: RecordingRun())
    original = "[bg-task-result] " + "完整输入" * (LARGE_PROMPT_THRESHOLD_BYTES // 12 + 1)
    assert len(original.encode("utf-8")) > LARGE_PROMPT_THRESHOLD_BYTES
    receipt = await manager.start_message(session.id, original)
    assert receipt.accepted
    assert (await asyncio.wait_for(receipt.wait(), 5)).outcome.status == "success"
    assert len(received) == 1 and received[0].startswith("[bg-task-result] [octopus-large-prompt]")
    assert len(received[0].encode("utf-8")) < LARGE_PROMPT_THRESHOLD_BYTES
    rows = await manager.db.load_messages(session.id)
    assert any(row["role"] == "user" and row["content"] == original for row in rows)
    assert read_service_profile(config) == profile
    files = list((account / "large-prompts" / session.id).glob("*.txt"))
    assert len(files) == 1 and files[0].read_text(encoding="utf-8") == original
    assert str(files[0]) in received[0]


async def test_parent_delete_keeps_retryable_source_until_all_forks_have_files(managed_files, monkeypatch):
    _config, account, _profile, parent, client, manager = managed_files
    original = b"The same complete bytes must survive every descendant"
    uploaded = await client.post(f"/api/sessions/{parent.id}/attachments",
                                 files={"file": ("retained.txt", original, "text/plain")})
    assert uploaded.status_code == 201
    metadata = uploaded.json()
    identifier = metadata["id"]
    await manager.db.append_message(parent.id, 0, "user", "text", "Keep this file", attachments=[metadata])
    for child_id, source_id in (("forkchild001", parent.id), ("forkgrand001", "forkchild001")):
        await manager.db.create_fork_session(
            fork_id=child_id, name="Retained fork", working_dir=parent.working_dir,
            created_at=parent.created_at, parent_id=source_id, backend=parent.backend,
            user_id=parent.user_id, agent_id=parent.agent_id, credential_id=None,
            resume_id=None, fork_after_seq=0,
        )
        await manager.db.update_session_field(child_id, fork_status="ready", archived=True)
    child_messages = {sid: await manager.db.load_messages(sid) for sid in ("forkchild001", "forkgrand001")}
    source = account / "attachments" / parent.id / f"{identifier}__retained.txt"
    copy = attachment_files.shutil.copy2

    def unavailable(source_path, target_path):
        if target_path.parent.name == "forkgrand001":
            raise OSError("descendant copy unavailable")
        return copy(source_path, target_path)

    with monkeypatch.context() as failure:
        failure.setattr(attachment_files.shutil, "copy2", unavailable)
        with pytest.raises(OSError, match="descendant copy unavailable"):
            await manager.delete_session(parent.id)
    assert manager.get_session(parent.id, parent.user_id) is parent
    assert await manager.db.get_session_owner(parent.id) == parent.user_id
    assert source.read_bytes() == original
    await manager.resume_dispatch(parent.id)
    assert parent._admission_closed and not parent._dispatch_paused
    assert (await client.post(f"/api/sessions/{parent.id}/duplicate", json={})).status_code == 409
    assert (await client.post(f"/api/sessions/{parent.id}/fork", json={"rewind_to_msg_seq": 0})).status_code == 409
    for sid, messages in child_messages.items():
        assert await manager.db.load_messages(sid) == messages
        response = await client.get(f"/api/sessions/{sid}/attachments/{identifier}")
        assert response.status_code == 200 and response.content == original
    assert await manager.delete_session(parent.id)
    assert manager.get_session(parent.id, parent.user_id) is None
    assert await manager.db.get_session_owner(parent.id) is None
    assert source.read_bytes() == original
    for sid, messages in child_messages.items():
        assert await manager.db.load_messages(sid) == messages
        assert (account / "attachments" / sid / source.name).read_bytes() == original
        response = await client.get(f"/api/sessions/{sid}/attachments/{identifier}")
        assert response.status_code == 200 and response.content == original
