"""Tests for the POST /api/sessions/import endpoint."""

import asyncio
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from server.agent_manager import AgentManager
from server.database import Database
from server.db.transaction import Transaction
from server.deps import set_user_manager
from server.harness.claude_code import ClaudeDriver
from server.main import app
from server.session_manager import SessionManager, session_manager
from server.users import UserManager
from tests.accounts import create_account
from tests.claude_runtime import claude_runtime as claude_runtime
from tests.claude_runtime import inputs as claude_inputs
from tests.session_api import session_client

_ATTACHMENT = {"id": "imported-file", "filename": "notes.txt", "size": 5, "mime_type": "text/plain"}
_TRANSCRIPT = {
    "name": "Complete history",
    "claude_session_id": "original-native-id",
    "messages": [
        {"role": "user", "type": "text", "content": "First", "seq": 20, "attachments": [_ATTACHMENT],
         "created_at": "2026-11-02T14:50:00+00:00"},
        {"role": "assistant", "type": "text", "content": "Second", "seq": 3},
        {"role": "user", "type": "text", "content": "Third", "seq": 15},
    ],
}


@pytest.fixture
async def client(tmp_path):
    db = Database(str(tmp_path / "import.db"))
    await db.initialize()
    _, token = await create_account(db, username="import-owner")
    session_manager.sessions.clear()
    session_manager._admission_closed = False
    await session_manager.initialize(db)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {token}"}) as c:
        yield c

    await db.close()


@pytest.mark.asyncio
async def test_import_basic(client):
    resp = await client.post(
        "/api/sessions/import",
        headers=client.headers,
        json={
            "name": "Imported",
            "messages": [
                {"role": "user", "type": "text", "content": "hello"},
                {"role": "assistant", "type": "text", "content": "hi there"},
            ],
        },
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "Imported"
    assert data["message_count"] == 2
    assert len(data["messages"]) == 2


@pytest.mark.asyncio
async def test_import_sets_claude_session_id(client):
    resp = await client.post(
        "/api/sessions/import",
        headers=client.headers,
        json={
            "name": "With Resume",
            "claude_session_id": "abc-123-def",
            "messages": [{"role": "user", "type": "text", "content": "test"}],
        },
    )
    assert resp.status_code == 201
    sid = resp.json()["id"]

    # Verify via GET that the session has the claude_session_id
    # (it's stored on the Session object; we can check the manager directly)
    session = session_manager.get_session(sid)
    assert session is not None
    assert session.claude_session_id == "abc-123-def"


@pytest.mark.asyncio
async def test_import_messages_retrievable(client):
    resp = await client.post(
        "/api/sessions/import",
        headers=client.headers,
        json={
            "name": "Persistent",
            "messages": [
                {"role": "user", "type": "text", "content": "msg1"},
                {
                    "role": "assistant",
                    "type": "tool_use",
                    "tool_name": "Read",
                    "tool_input": {"path": "/f", "options": {"limit": 1.25}},
                    "tool_use_id": "t1",
                    "cost": 0.125,
                },
                {
                    "role": "tool",
                    "type": "tool_result",
                    "content": "file data",
                    "tool_use_id": "t1",
                },
            ],
        },
    )
    sid = resp.json()["id"]

    # GET the session and verify messages are persisted
    get_resp = await client.get(f"/api/sessions/{sid}", headers=client.headers)
    assert get_resp.status_code == 200
    messages = get_resp.json()["messages"]
    assert len(messages) == 3
    assert messages[0]["content"] == "msg1"
    assert messages[1]["tool_name"] == "Read"
    assert messages[1]["tool_input"] == {"path": "/f", "options": {"limit": 1.25}}
    assert messages[1]["cost"] == 0.125
    assert messages[2]["type"] == "tool_result"


@pytest.mark.asyncio
async def test_import_auth_required(client):
    resp = await client.post(
        "/api/sessions/import",
        headers={"Authorization": ""},
        json={"name": "No Auth", "messages": []},
    )
    assert resp.status_code in (401, 403)


async def test_each_accounts_import_uses_its_own_default_and_rejects_a_foreign_agent(client):
    database = session_manager.db
    users = UserManager(database)
    first = await users.resolve_token(client.headers["Authorization"].removeprefix("Bearer "))
    invite = await users.create_invite(created_by=first["id"])
    second = await users.register(invite_code=invite["code"], username="second-owner", password="another-password")
    second_token = await users.issue_token(second["id"])
    first_agent = await database.get_system_agent(first["id"])
    second_agent = await database.get_system_agent(second["id"])
    assert first_agent["id"] != second_agent["id"] and second_agent["is_system"]
    one = await client.post("/api/sessions/import", json={"name": "First import", "messages": []})
    two = await client.post("/api/sessions/import", json={"name": "Second import", "messages": []},
                            headers={"Authorization": f"Bearer {second_token}"})
    assert one.status_code == two.status_code == 201
    assert one.json()["agent_id"] == first_agent["id"]
    assert two.json()["agent_id"] == second_agent["id"]
    assert session_manager.get_session(one.json()["id"]).user_id == first["id"]
    assert session_manager.get_session(two.json()["id"]).user_id == second["id"]
    foreign = await client.post("/api/sessions/import", headers={"Authorization": f"Bearer {second_token}"},
                                json={"name": "Refused", "agent_id": first_agent["id"], "messages": []})
    assert foreign.status_code == 404 and len(await database.load_sessions()) == 2


@pytest.mark.asyncio
async def test_import_empty_messages(client):
    resp = await client.post(
        "/api/sessions/import",
        headers=client.headers,
        json={"name": "Empty"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["message_count"] == 0
    assert data["messages"] == []


@pytest.mark.asyncio
async def test_import_appears_in_list(client):
    await client.post(
        "/api/sessions/import",
        headers=client.headers,
        json={
            "name": "Listed Session",
            "messages": [{"role": "user", "type": "text", "content": "hey"}],
        },
    )

    resp = await client.get("/api/sessions", headers=client.headers)
    assert resp.status_code == 200
    sessions = resp.json()
    names = [s["name"] for s in sessions]
    assert "Listed Session" in names


@pytest.mark.parametrize("failed_seq", [0, 2])
async def test_import_message_failure_rolls_back_the_session_and_history(client, failed_seq):
    db = session_manager.db
    async with db.transaction() as tx:
        await tx.execute(
            f"CREATE TEMP TRIGGER reject_import_message BEFORE INSERT ON messages WHEN NEW.seq = {failed_seq} "
            "BEGIN SELECT RAISE(ABORT, 'test message rejection'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="test message rejection"):
        await client.post("/api/sessions/import", json=_TRANSCRIPT)
    await _assert_no_import(client)


async def _assert_no_import(client):
    assert not session_manager.sessions
    assert (await client.get("/api/sessions")).json() == []
    async with session_manager.db.transaction(write=False) as tx:
        counts = await tx.fetch_one("SELECT (SELECT count(*) FROM sessions), (SELECT count(*) FROM messages), "
                                    "(SELECT count(*) FROM client_submissions)")
        assert tuple(counts) == (0, 0, 0)


async def test_failure_after_writing_final_message_rolls_back_everything(client, monkeypatch):
    execute_many = Transaction.execute_many

    async def refuse_before_commit(tx, sql, parameters):
        await execute_many(tx, sql, parameters)
        if sql.startswith("INSERT INTO messages "):
            assert (await tx.fetch_one("SELECT count(*) FROM messages"))[0] == 3
            raise sqlite3.IntegrityError("test refusal after final message")

    monkeypatch.setattr(Transaction, "execute_many", refuse_before_commit)
    with pytest.raises(sqlite3.IntegrityError, match="test refusal after final message"):
        await client.post("/api/sessions/import", json=_TRANSCRIPT)
    await _assert_no_import(client)


async def test_import_validates_history_before_persisting_unserializable_response_content(client):
    content = "leaf"
    for _ in range(253):
        content = {"nested": content}
    response = await client.post("/api/sessions/import", json={
        "name": "Excessively nested history",
        "messages": [{"role": "assistant", "type": "text", "content": content}],
    })
    assert response.status_code == 422
    await _assert_no_import(client)


@pytest.mark.parametrize("message_field", [
    pytest.param('"content":{"nested":[1e400]}', id="nested-content-overflow"),
    pytest.param('"tool_input":{"nested":[-1e400]}', id="nested-tool-arguments-overflow"),
    pytest.param('"cost":1e400', id="cost-overflow"),
    pytest.param('"cost":NaN', id="cost-nan"),
])
async def test_import_rejects_non_finite_numbers_before_installation(client, message_field):
    response = await client.post(
        "/api/sessions/import",
        content=(
            '{"messages":[{"role":"user","type":"text","content":"Valid prefix"},'
            '{"role":"assistant","type":"tool_use",' + message_field + '}]}'
        ),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    await _assert_no_import(client)


def _assert_complete_history(detail, name=_TRANSCRIPT["name"]):
    assert detail["name"] == name
    assert detail["claude_session_id"] == _TRANSCRIPT["claude_session_id"]
    assert detail["message_count"] == detail["next_message_seq"] == 3
    assert detail["dispatch_state"]["items"] == []
    assert detail["dispatch_state"]["active_request_id"] is None
    assert [message["seq"] for message in detail["messages"]] == [0, 1, 2]
    assert [message["content"] for message in detail["messages"]] == ["First", "Second", "Third"]
    assert detail["messages"][0]["created_at"] == "2026-11-02T14:50:00+00:00"
    assert detail["messages"][0]["attachments"] == [_ATTACHMENT]


@pytest.mark.parametrize("name", [_TRANSCRIPT["name"], ""])
async def test_import_response_matches_history_read_by_a_new_installation(client, tmp_path, name):
    response = await client.post("/api/sessions/import", json=_TRANSCRIPT | {"name": name})
    assert response.status_code == 201, response.text
    imported = response.json()
    _assert_complete_history(imported, name)
    token = client.headers["Authorization"].removeprefix("Bearer ")
    await session_manager.db.close()
    reader = Database(str(tmp_path / "import.db"))
    await reader.initialize()
    try:
        restarted = SessionManager()
        await restarted.initialize(reader)
        set_user_manager(UserManager(reader))
        async with session_client(restarted, bearer=token) as fresh:
            restored = (await fresh.get(f"/api/sessions/{imported['id']}")).json()
        _assert_complete_history(restored, name)
        assert restored["messages"] == imported["messages"]
    finally:
        await reader.close()


async def test_import_known_busy_refuses_the_write_and_the_same_generation_can_import(client, tmp_path, monkeypatch):
    db = session_manager.db
    async with db.transaction(write=False) as tx:
        await tx.execute("PRAGMA busy_timeout=50")
    install = db.install_session_history

    async def use_a_second_writer(record, messages):
        with closing(sqlite3.connect(tmp_path / "import.db", isolation_level=None)) as competing:
            competing.execute("BEGIN IMMEDIATE")
            try:
                return await install(record, messages)
            finally:
                competing.rollback()

    with monkeypatch.context() as patch:
        patch.setattr(db, "install_session_history", use_a_second_writer)
        refused = await client.post("/api/sessions/import", json=_TRANSCRIPT)
    assert refused.status_code == 503, refused.text
    await _assert_no_import(client)
    response = await client.post("/api/sessions/import", json=_TRANSCRIPT)
    assert response.status_code == 201, response.text
    _assert_complete_history(response.json())


async def test_disconnected_import_waiter_does_not_cancel_the_original_installation(client, tmp_path, monkeypatch):
    written, release = asyncio.Event(), asyncio.Event()
    execute_many = Transaction.execute_many

    async def hold_before_commit(tx, sql, parameters):
        await execute_many(tx, sql, parameters)
        if sql.startswith("INSERT INTO messages "):
            written.set()
            await release.wait()

    monkeypatch.setattr(Transaction, "execute_many", hold_before_commit)
    request = asyncio.create_task(client.post("/api/sessions/import", json=_TRANSCRIPT))
    try:
        await asyncio.wait_for(written.wait(), 3)
        assert not session_manager.sessions
        with closing(sqlite3.connect(tmp_path / "import.db")) as reader:
            assert reader.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
            assert reader.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
        (operation,) = (op for pending in session_manager._agent_operations.values() for op in pending)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)
    await asyncio.wait_for(operation, 3)
    [session] = (await client.get("/api/sessions")).json()
    _assert_complete_history((await client.get(f"/api/sessions/{session['id']}")).json())


async def test_import_response_is_fixed_before_waiting_agent_archive_evicts_it(client, monkeypatch):
    db = session_manager.db
    user = await UserManager(db).resolve_token(client.headers["Authorization"].removeprefix("Bearer "))
    agent = await AgentManager(db).create_agent(name="Archive after import", user_id=user["id"])
    completed, release = asyncio.Event(), asyncio.Event()
    install = session_manager.import_session

    async def delay_http_response(**fields):
        detail = await install(**fields)
        completed.set()
        await release.wait()
        return detail

    monkeypatch.setattr(session_manager, "import_session", delay_http_response)
    request = asyncio.create_task(client.post("/api/sessions/import", json=_TRANSCRIPT | {"agent_id": agent["id"]}))
    try:
        await asyncio.wait_for(completed.wait(), 3)
        [sid] = session_manager.sessions
        await session_manager.archive_agent(agent["id"], user_id=user["id"])
        assert session_manager.get_session(sid) is None
    finally:
        release.set()
        response = await asyncio.wait_for(request, 3)
    assert response.status_code == 201, response.text
    assert response.json()["id"] == sid
    _assert_complete_history(response.json())


async def test_long_import_commits_a_complete_ordered_transcript(client):
    count = 50_000
    response = await client.post("/api/sessions/import", json={
        "name": "Long transcript",
        "messages": [{"role": "user", "type": "text", "content": f"Message {seq}"} for seq in range(count)],
    })
    assert response.status_code == 201, response.text
    detail = response.json()
    assert detail["message_count"] == detail["next_message_seq"] == len(detail["messages"]) == count
    assert detail["messages"][0]["content"] == "Message 0"
    assert detail["messages"][-1]["content"] == "Message 49999"
    assert await session_manager.db.get_session_resume_id(detail["id"]) is None


@pytest.mark.parametrize("committed", [False, True])
async def test_import_commit_loss_fail_stops_and_restart_reads_only_the_durable_fact(tmp_path, committed):
    path = tmp_path / "lost-import.db"
    db = Database(str(path))
    await db.initialize()
    _, token = await create_account(db)
    await db.close()
    proof = tmp_path / "commit-boundary"
    program = (
        "import asyncio, json, sys; from pathlib import Path; "
        "from tests.session_import_support import lose_import_commit; "
        "token, transcript = json.load(sys.stdin); "
        "asyncio.run(lose_import_commit(Path(sys.argv[1]), token, transcript, Path(sys.argv[2]), sys.argv[3] == 'True'))"
    )
    result = await asyncio.to_thread(subprocess.run,
        [sys.executable, "-c", program, str(path), str(proof), str(committed)],
        input=json.dumps([token, _TRANSCRIPT]), capture_output=True, text=True, timeout=10,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert result.returncode == 70, result.stderr
    assert proof.exists() and not proof.with_suffix(".response").exists()
    restarted = Database(str(path))
    await restarted.initialize()
    try:
        manager = SessionManager()
        await manager.initialize(restarted)
        set_user_manager(UserManager(restarted))
        async with session_client(manager, bearer=token) as client:
            sessions = (await client.get("/api/sessions")).json()
            assert len(sessions) == int(committed)
            if committed:
                _assert_complete_history((await client.get(f"/api/sessions/{sessions[0]['id']}")).json())
        async with restarted.transaction(write=False) as tx:
            assert (await tx.fetch_one("SELECT count(*) FROM messages"))[0] == (3 if committed else 0)
            assert (await tx.fetch_one("SELECT count(*) FROM client_submissions"))[0] == 0
    finally:
        await restarted.close()


async def test_imported_text_controls_cannot_change_fork_or_lost_history_replay(
    claude_runtime, tmp_path, monkeypatch,
):
    manager, db, seed, starts, _ = claude_runtime
    token = await UserManager(db).issue_token(seed.user_id)
    tool_arguments = {"request_context": {"project": "marker"}, "handoff_withdrawn": "tool argument"}
    async with session_client(manager, bearer=token) as client:
        imported = await client.post("/api/sessions/import", json={
            "name": "External history", "working_dir": str(tmp_path),
            "claude_session_id": "external-claude-history",
            "messages": [
                {"role": "user", "type": "text", "content": "Original opening"},
                {"role": "assistant", "type": "text", "content": "Keep this paragraph",
                 "tool_input": {"handoff_withdrawn": True}},
                {"role": "user", "type": "text", "content": "Ordinary checkpoint",
                 "tool_input": {"request_context": {"source": "handoff"}, "handoff_id": "external marker",
                                "copied_input": {"observation": "external marker"}}},
                {"role": "assistant", "type": "tool_use", "tool_name": "Example",
                 "tool_use_id": "example", "tool_input": tool_arguments},
                {"role": "user", "type": "text", "content": "Fork point"},
            ],
        })
        assert imported.status_code == 201, imported.text
        detail = imported.json()
        assert detail["messages"][1]["tool_input"] is None
        assert detail["messages"][2]["tool_input"] is None
        assert detail["messages"][3]["tool_input"] == tool_arguments
        assert detail["credential_id"] is None and not starts
        assert detail["dispatch_state"]["active_request_id"] is None
        assert detail["dispatch_state"]["items"] == []
        sid = detail["id"]
        async with db.transaction(write=False) as tx:
            assert await tx.fetch_one("SELECT 1 FROM client_submissions WHERE session_id=?", (sid,)) is None

    prepare = ClaudeDriver.prepare

    async def missing_native_history(driver, io, context, argv, kwargs):
        if context.resume_id == "external-claude-history":
            raise ValueError(f"No conversation found with session ID: {context.resume_id}")
        await prepare(driver, io, context, argv, kwargs)

    monkeypatch.setattr(ClaudeDriver, "prepare", missing_native_history)
    (tmp_path / "claude-plan.json").write_text('{"save_input_history": true}')
    recovered = await manager.start_message(sid, "Continue the import")
    recovery = await asyncio.wait_for(recovered.wait(), 5)
    assert recovery.outcome.status == "success" and recovery.outcome.recovery["stale_session"] == 1
    async with session_client(manager, bearer=token) as client:
        fork = await client.post(f"/api/sessions/{sid}/fork", json={"rewind_to_msg_seq": 4, "revert_files": False})
        assert fork.status_code == 201, fork.text
    fork_input = await manager.start_message(fork.json()["id"], "Continue the fork")
    assert (await asyncio.wait_for(fork_input.wait(), 5)).outcome.status == "success"

    frames = await claude_inputs(tmp_path, 2)
    assert len(frames) == 2
    for frame, wrapper in zip(frames, ("<session-history", "<fork-history"), strict=True):
        prompt = frame["input"]["message"]["content"]
        assert wrapper in prompt
        assert all(body in prompt for body in ("Original opening", "Keep this paragraph", "Ordinary checkpoint"))
        assert all(marker not in prompt for marker in ("ticket handoff", "input_observation=", "external marker"))
    assert all(context.credential is None for context in starts)
