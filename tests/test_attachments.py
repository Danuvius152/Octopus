"""Tests for the file/image attachment feature.

Covers the on-disk storage module, the upload/download HTTP endpoints,
and the session_manager glue that resolves attachment ids → absolute
file paths and prepends an `<attachments>` header to the prompt.
"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest
from httpx import ASGITransport, AsyncClient

from server import deps
from server.attachments import (
    MAX_FILE_BYTES,
    AttachmentError,
    get_path,
    save_upload,
)
from server.config import settings
from server.database import Database
from server.harness import get_harness
from server.harness.claude_code import ClaudeDriver
from server.main import app
from server.session_manager import session_manager
from server.users import UserManager
from server.workspace import paths_for
from tests.harness_profiles import process_profile


@pytest.fixture
def users_root(tmp_path, monkeypatch):
    """Keep both storage helpers and real accounts under a per-test root."""
    monkeypatch.setattr(settings, "users_root", str(tmp_path / "users"))


@pytest.fixture
def storage_user_id(users_root):
    """Pure file helpers receive an explicit, isolated account owner."""
    return "attachment-storage-owner"


@pytest.fixture
def attachments_root(storage_user_id):
    root = paths_for(storage_user_id).attachments
    root.mkdir(parents=True)
    return root


@pytest.fixture
async def account_user_id(users_root, monkeypatch):
    """Create the real account aggregate used by authenticated HTTP tests."""
    db = Database(":memory:")
    await db.initialize()
    users = UserManager(db)
    user = await users.create_user(username="attachment-owner", password="password-for-files")
    monkeypatch.setattr(deps, "_user_manager", users)
    session_manager.sessions.clear()
    session_manager._admission_closed = False
    await session_manager.initialize(db)
    try:
        yield user["id"]
    finally:
        await db.close()


@pytest.fixture
async def client(account_user_id):
    """In-memory DB + ASGI client with a real account bearer."""
    headers = {
        "Authorization": "Bearer " + await deps.get_user_manager().issue_token(account_user_id)
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as c:
        yield c


@pytest.fixture
def runtime_input(tmp_path, monkeypatch):
    """Capture the actual input frame delivered to an offline Python child."""
    received = tmp_path / "received-input.json"
    model = tmp_path / "attachment_model.py"
    model.write_text(
        "import json,sys\n"
        "from pathlib import Path\n"
        "Path(sys.argv[1]).write_text(sys.stdin.readline())\n"
        "print(json.dumps({'type':'thread.started','thread_id':'attachment-thread'}),flush=True)\n"
        "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}),flush=True)\n"
        "sys.stdin.read()\n"
    )
    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", process_profile(
        harness.profile, binary=sys.executable, activity_observer=None, prepare_resume=None,
        new_driver=ClaudeDriver, can_steer=True, reusable=True,
        build_turn_argv=lambda context: (
            [sys.executable, "-u", str(model), str(received)], {"cwd": context.working_dir},
        ),
    ))
    return received


# ---------------------------------------------------------------------------
# Storage module — unit-level
# ---------------------------------------------------------------------------


def test_save_upload_writes_file_and_returns_metadata(attachments_root, storage_user_id):
    rec = save_upload("sess-abc", "hello.txt", b"hi there", declared_mime="text/plain",
                      user_id=storage_user_id)
    assert rec.size == 8
    assert rec.filename == "hello.txt"
    assert rec.mime_type == "text/plain"
    assert rec.path.is_file()
    assert rec.path.read_bytes() == b"hi there"
    # Layout is `<id>__<filename>` inside the session subdir
    assert rec.path.parent.name == "sess-abc"
    assert rec.path.name.endswith("__hello.txt")
    assert rec.path.name.startswith(rec.id + "__")


def test_save_upload_rejects_oversize(attachments_root, storage_user_id):
    big = b"x" * (MAX_FILE_BYTES + 1)
    with pytest.raises(AttachmentError):
        save_upload("s", "big.bin", big, user_id=storage_user_id)


def test_save_upload_rejects_empty(attachments_root, storage_user_id):
    with pytest.raises(AttachmentError):
        save_upload("s", "empty.txt", b"", user_id=storage_user_id)


def test_save_upload_sanitizes_filename(attachments_root, storage_user_id):
    rec = save_upload("s", "../../../etc/passwd", b"data", user_id=storage_user_id)
    # Path separators stripped; .. runs collapsed; file lands inside session dir
    assert "/" not in rec.path.name
    assert "\\" not in rec.path.name
    assert rec.path.parent.name == "s"
    assert ".." not in rec.filename


def test_save_upload_handles_blank_filename(attachments_root, storage_user_id):
    rec = save_upload("s", "", b"data", user_id=storage_user_id)
    assert rec.filename  # always non-empty fallback
    assert rec.path.is_file()


def test_get_path_roundtrip(attachments_root, storage_user_id):
    rec = save_upload("s", "hello.txt", b"data", user_id=storage_user_id)
    found = get_path("s", rec.id, user_id=storage_user_id)
    assert found == rec.path


def test_get_path_missing_returns_none(attachments_root, storage_user_id):
    assert get_path("nonexistent", "abcdef123456", user_id=storage_user_id) is None
    rec = save_upload("s", "hello.txt", b"data", user_id=storage_user_id)
    assert get_path("s", "wrongid000000", user_id=storage_user_id) is None
    # Real id still resolves
    assert get_path("s", rec.id, user_id=storage_user_id) == rec.path


def test_get_path_rejects_traversal(attachments_root, storage_user_id):
    save_upload("s", "ok.txt", b"data", user_id=storage_user_id)
    # Path separators in the id can't escape the session dir
    assert get_path("s", "../other", user_id=storage_user_id) is None
    assert get_path("s", "..\\other", user_id=storage_user_id) is None


def test_upload_does_not_follow_a_session_directory_symlink(attachments_root, storage_user_id):
    outside = attachments_root / "other-session"
    outside.mkdir()
    (attachments_root / "s").symlink_to(outside, target_is_directory=True)
    with pytest.raises(AttachmentError):
        save_upload("s", "evidence.txt", b"Do not write to the other session",
                    user_id=storage_user_id)
    assert list(outside.iterdir()) == []


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_and_download_roundtrip(client, account_user_id):
    agent = await session_manager.db.get_system_agent(account_user_id)
    assert agent is not None
    create = await client.post(
        "/api/sessions", headers=client.headers, json={"agent_id": agent["id"], "name": "U"}
    )
    sid = create.json()["id"]

    files = {"file": ("hello.txt", b"world", "text/plain")}
    up = await client.post(
        f"/api/sessions/{sid}/attachments", headers=client.headers, files=files
    )
    assert up.status_code == 201
    meta = up.json()
    assert meta["filename"] == "hello.txt"
    assert meta["size"] == 5
    assert meta["mime_type"] == "text/plain"
    aid = meta["id"]

    # Bearer-header download
    dn = await client.get(
        f"/api/sessions/{sid}/attachments/{aid}", headers=client.headers
    )
    assert dn.status_code == 200
    assert dn.content == b"world"

    # Query-token download (the path <img src> takes)
    dn_q = await client.get(
        f"/api/sessions/{sid}/attachments/{aid}",
        params={"token": client.headers["Authorization"].removeprefix("Bearer ")},
        headers={"Authorization": ""}
    )
    assert dn_q.status_code == 200
    assert dn_q.content == b"world"


@pytest.mark.asyncio
async def test_upload_requires_auth(client, account_user_id):
    agent = await session_manager.db.get_system_agent(account_user_id)
    assert agent is not None
    create = await client.post(
        "/api/sessions", headers=client.headers, json={"agent_id": agent["id"], "name": "Auth"}
    )
    sid = create.json()["id"]
    files = {"file": ("x.txt", b"x", "text/plain")}
    resp = await client.post(f"/api/sessions/{sid}/attachments", files=files, headers={"Authorization": ""})
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_upload_unknown_session_404(client):
    files = {"file": ("x.txt", b"x", "text/plain")}
    resp = await client.post(
        "/api/sessions/nope/attachments", headers=client.headers, files=files
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_download_missing_404(client, account_user_id):
    agent = await session_manager.db.get_system_agent(account_user_id)
    assert agent is not None
    create = await client.post(
        "/api/sessions", headers=client.headers, json={"agent_id": agent["id"], "name": "M"}
    )
    sid = create.json()["id"]
    resp = await client.get(
        f"/api/sessions/{sid}/attachments/doesnotexist", headers=client.headers
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_download_wrong_token_401(client, account_user_id):
    agent = await session_manager.db.get_system_agent(account_user_id)
    assert agent is not None
    create = await client.post(
        "/api/sessions", headers=client.headers, json={"agent_id": agent["id"], "name": "WT"}
    )
    sid = create.json()["id"]
    files = {"file": ("x.txt", b"x", "text/plain")}
    up = await client.post(
        f"/api/sessions/{sid}/attachments", headers=client.headers, files=files
    )
    aid = up.json()["id"]
    # Wrong query token
    resp = await client.get(
        f"/api/sessions/{sid}/attachments/{aid}?token=wrong", headers={"Authorization": ""}
    )
    assert resp.status_code == 401
    # No token at all
    resp = await client.get(f"/api/sessions/{sid}/attachments/{aid}", headers={"Authorization": ""})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_delete_session_closes_downloads_and_keeps_original_file_paths(client, account_user_id):
    agent = await session_manager.db.get_system_agent(account_user_id)
    assert agent is not None
    create = await client.post(
        "/api/sessions", headers=client.headers, json={"agent_id": agent["id"], "name": "Cleanup"}
    )
    sid = create.json()["id"]
    files = {"file": ("a.txt", b"hi", "text/plain")}
    uploaded = await client.post(
        f"/api/sessions/{sid}/attachments", headers=client.headers, files=files
    )
    assert uploaded.status_code == 201
    session_dir = paths_for(account_user_id).attachments / sid
    assert session_dir.is_dir()

    deleted = await client.delete(f"/api/sessions/{sid}", headers=client.headers)
    assert deleted.status_code == 204
    retained = list(session_dir.iterdir())
    assert len(retained) == 1 and retained[0].read_bytes() == b"hi"
    assert (await client.get(f"/api/sessions/{sid}/attachments/{uploaded.json()['id']}")).status_code == 404


# ---------------------------------------------------------------------------
# session_manager glue — attachment_ids prepend a header to the prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_prepends_attachment_header(
    client, account_user_id, runtime_input,
):
    """An uploaded attachment's absolute path is injected into the prompt
    that hits the backend, so the agent can `Read` it.

    An offline Python child captures the actual input frame.
    """
    agent = await session_manager.db.get_system_agent(account_user_id)
    assert agent is not None
    create = await client.post(
        "/api/sessions", headers=client.headers, json={"agent_id": agent["id"], "name": "P", "backend": "codex"}
    )
    sid = create.json()["id"]
    files = {"file": ("notes.txt", b"hello", "text/plain")}
    up = await client.post(
        f"/api/sessions/{sid}/attachments", headers=client.headers, files=files
    )
    aid = up.json()["id"]

    receipt = await session_manager.start_message(
        sid, "what's in this file?", attachment_ids=[aid]
    )
    assert (await asyncio.wait_for(receipt.wait(), 5)).outcome.status == "success"

    prompt = json.loads(runtime_input.read_text())["message"]["content"]
    assert "<attachments>" in prompt
    assert "</attachments>" in prompt
    # Absolute path to the file we uploaded should be in the header
    expected_path = get_path(sid, aid, user_id=account_user_id)
    assert expected_path is not None
    assert str(expected_path) in prompt
    # Original user text still present, after the header
    assert "what's in this file?" in prompt
    assert prompt.index("</attachments>") < prompt.index("what's in this file?")


@pytest.mark.asyncio
async def test_send_message_no_attachments_unchanged(client, account_user_id, runtime_input):
    """No attachments → no `<attachments>` block, prompt is verbatim."""
    agent = await session_manager.db.get_system_agent(account_user_id)
    assert agent is not None
    create = await client.post(
        "/api/sessions", headers=client.headers, json={"agent_id": agent["id"], "name": "Plain", "backend": "codex"}
    )
    sid = create.json()["id"]

    receipt = await session_manager.start_message(sid, "hello there")
    assert (await asyncio.wait_for(receipt.wait(), 5)).outcome.status == "success"
    assert json.loads(runtime_input.read_text())["message"]["content"] == "hello there"


@pytest.mark.asyncio
async def test_send_message_missing_attachment_id_rejects_complete_input(
    client, account_user_id, runtime_input,
):
    """Execution rechecks the files and records a recoverable failure."""
    agent = await session_manager.db.get_system_agent(account_user_id)
    assert agent is not None
    create = await client.post(
        "/api/sessions", headers=client.headers, json={"agent_id": agent["id"], "name": "Drop", "backend": "codex"}
    )
    sid = create.json()["id"]

    uploaded = await client.post(
        f"/api/sessions/{sid}/attachments", headers=client.headers,
        files={"file": ("vanishing.txt", b"Required original bytes", "text/plain")},
    )
    aid = uploaded.json()["id"]
    paused = asyncio.Event()

    async def pause_before_claim(event):
        if event.get("session_id") == sid and event.get("type") == "dequeued" and not paused.is_set():
            await session_manager.interrupt(sid)
            paused.set()

    session_manager.on_broadcast("pause-attachment-before-claim", pause_before_claim)
    try:
        receipt = await session_manager.start_message(sid, "still send", attachment_ids=[aid])
        assert receipt.accepted
        await asyncio.wait_for(paused.wait(), 5)
        await session_manager.await_session_execution(sid, timeout=5)
        get_path(sid, aid, user_id=account_user_id).unlink()
        await session_manager.resume_dispatch(sid)
        completed = await asyncio.wait_for(receipt.wait(), 5)
    finally:
        session_manager.remove_broadcast("pause-attachment-before-claim")
    assert not runtime_input.exists()
    assert completed.outcome.code == "attachment_unavailable"
    messages = await session_manager.db.load_messages(sid)
    result = next(row for row in messages if row["type"] == "result")
    assert result["tool_input"]["rejected_input"] == {"content": "still send", "attachment_ids": [aid]}
    assert not [row for row in messages if row["role"] == "user"]


@pytest.mark.asyncio
async def test_user_message_persists_attachments_metadata(
    client, account_user_id, runtime_input,
):
    """After a turn with attachments, the DB row for the user message
    carries the metadata so the chat history can re-render the chips on
    reload."""
    agent = await session_manager.db.get_system_agent(account_user_id)
    assert agent is not None
    create = await client.post(
        "/api/sessions", headers=client.headers, json={"agent_id": agent["id"], "name": "Persist", "backend": "codex"}
    )
    sid = create.json()["id"]
    files = {"file": ("doc.md", b"x", "text/markdown")}
    up = await client.post(
        f"/api/sessions/{sid}/attachments", headers=client.headers, files=files
    )
    aid = up.json()["id"]

    receipt = await session_manager.start_message(sid, "look", attachment_ids=[aid])
    assert (await asyncio.wait_for(receipt.wait(), 5)).outcome.status == "success"
    assert "look" in json.loads(runtime_input.read_text())["message"]["content"]

    msgs = await session_manager.db.load_messages(sid)
    user_msgs = [m for m in msgs if m["role"] == "user"]
    assert user_msgs, "user message wasn't persisted"
    assert user_msgs[0]["attachments"]
    persisted = user_msgs[0]["attachments"][0]
    assert persisted["id"] == aid
    assert persisted["filename"] == "doc.md"


@pytest.mark.asyncio
async def test_start_message_caps_attachment_count(client, account_user_id):
    """Too many attachments → ValueError surfacing as a WS error."""
    agent = await session_manager.db.get_system_agent(account_user_id)
    assert agent is not None
    create = await client.post(
        "/api/sessions", headers=client.headers, json={"agent_id": agent["id"], "name": "Cap"}
    )
    sid = create.json()["id"]
    too_many = [f"id{i}" for i in range(50)]
    with pytest.raises(ValueError, match="too many"):
        await session_manager.start_message(sid, "x", attachment_ids=too_many)


# ---------------------------------------------------------------------------
# Current storage reopening — nullable attachments retain their original meaning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_current_reopen_preserves_messages_without_attachments(tmp_path):
    """A retained NULL attachment list stays empty without rewriting the body."""
    db_path = tmp_path / "legacy.db"
    seed = Database(str(db_path))
    await seed.initialize()
    users = UserManager(seed)
    owner = await users.create_user(username="attachment-upgrade", password="attachment-password")
    agent = await seed.get_system_agent(owner["id"])
    await seed.save_session("legacy", "L", str(tmp_path), "2024-01-01T00:00:00Z",
                            agent_id=agent["id"], user_id=owner["id"])
    await seed.append_message("legacy", 0, "user", "text", "hi")
    await seed.close()

    db = Database(str(db_path))
    await db.initialize()
    msgs = await db.load_messages("legacy")
    assert len(msgs) == 1
    assert msgs[0]["content"] == "hi"
    assert msgs[0]["attachments"] == []  # default for legacy rows
    assert (await db.get_session_binding("legacy"))["user_id"] == owner["id"]
    await db.close()
