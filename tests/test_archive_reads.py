"""Archive reads retain all owned history with bounded SQLite round trips."""

import re

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.database import Database
from server.deps import get_session_manager
from server.routers.sessions import router
from server.sessions import SessionManager
from tests.accounts import create_account


@pytest.fixture
async def archives(tmp_path):
    database = Database(":memory:")
    await database.initialize()
    owner, bearer = await create_account(database, extra_roots=[tmp_path])
    other, _ = await create_account(database, username="other-archive-owner", extra_roots=[tmp_path])
    manager = SessionManager()
    await manager.initialize(database)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                               headers={"Authorization": "Bearer " + bearer}) as client:
            yield database, manager, client, owner["id"], other["id"]
    finally:
        await manager.drain_turns()
        await database.close()


def history_queries(statements):
    # Authentication's bounded owner lookup and Agent admission's live-count
    # projection are separate from reading archive rows and their messages.
    return [sql for sql in statements if "FROM auth_tokens" not in sql and "FROM agents a" not in sql
            and re.search(r"\b(?:FROM|JOIN)\s+(?:sessions|messages)\b", sql, re.I)]


async def test_archive_list_round_trips_do_not_grow_with_owned_history(archives, tmp_path):
    database, manager, client, owner, other = archives
    agent = await database.get_system_agent(owner)
    foreign_agent = await database.get_system_agent(other)
    foreign = await manager.create_session(foreign_agent["id"], "Foreign archive", str(tmp_path))
    await manager.archive_session(foreign.id, replace=False)
    live = await manager.create_session(agent["id"], "Live session", str(tmp_path))
    statements = []
    await database.conn.set_trace_callback(statements.append)
    reads = []
    expected = []
    try:
        for size in (0, 1, 32):
            while len(expected) < size:
                session = await manager.create_session(agent["id"], f"Archive {len(expected)}", str(tmp_path))
                if expected:
                    await database.append_message(session.id, 2, "user", "text", "Earlier original")
                    await database.append_message(session.id, 7, "assistant", "text", "Later original")
                await manager.archive_session(session.id, replace=False)
                expected.append((session.id, 0 if not expected else 2))
            statements.clear()
            response = await client.get("/api/sessions?include_archived=true")
            assert response.status_code == 200
            rows = response.json()
            assert [row["id"] for row in rows if not row["archived"]] == [live.id]
            assert [(row["id"], row["message_count"]) for row in rows if row["archived"]] == expected
            assert all(row["transcript_integrity"] == "ok" for row in rows)
            reads.append(len(history_queries(statements)))
        assert reads[0] == reads[1] == reads[2], reads
    finally:
        await database.conn.set_trace_callback(None)


async def test_archive_reads_preserve_duplicate_sequence_counts_and_refusals(archives, tmp_path):
    database, manager, client, owner, _ = archives
    agent = await database.get_system_agent(owner)
    session = await manager.create_session(agent["id"], "Damaged transcript", str(tmp_path))
    await database.append_message(session.id, 2, "assistant", "text", "Original two")
    await database.append_message(session.id, 7, "assistant", "text", "Original seven")
    await manager.archive_session(session.id, replace=False)
    # Model retained corruption; production writes keep their unique index.
    async with database.transaction() as tx:
        await tx.execute("DROP INDEX messages_unique_sequence")
        await tx.execute("INSERT INTO messages(session_id,seq,role,type,content) VALUES(?,7,'assistant','text','\"Duplicate seven\"')", (session.id,))
    listed = (await client.get("/api/sessions?include_archived=true")).json()
    assert len(listed) == 1
    assert listed[0]["message_count"] == 3 and listed[0]["transcript_integrity"] == "duplicate_seq"
    for suffix in ("/info", ""):
        response = await client.get(f"/api/sessions/{session.id}{suffix}")
        assert response.status_code == 200
        assert response.json()["message_count"] == 3 and response.json()["transcript_integrity"] == "duplicate_seq"
        if not suffix:
            assert response.json()["messages"] == []
    assert (await client.get(f"/api/sessions/{session.id}/messages/7")).status_code == 409
    generation = (await client.get(f"/api/sessions/{session.id}/restoration")).json()["service_generation"]
    restored = await client.post(f"/api/sessions/{session.id}/unarchive", json={"operation_id": "damaged-restore", "service_generation": generation})
    assert restored.status_code == 200 and restored.json()["status_code"] is None
    current = (await client.get(f"/api/sessions/{session.id}")).json()
    assert current["archived"] is False and current["message_count"] == 3
    assert current["transcript_integrity"] == "duplicate_seq" and current["messages"] == []
    assert (await client.get(f"/api/sessions/{session.id}/messages/7")).status_code == 409


@pytest.mark.parametrize("operation", ["info", "detail", "restore"])
async def test_archive_single_target_reads_are_scoped_point_queries(archives, tmp_path, operation):
    database, manager, client, owner, other = archives
    agent = await database.get_system_agent(owner)
    targets = []
    for index in range(8):
        session = await manager.create_session(agent["id"], f"Owned {index}", str(tmp_path))
        await database.append_message(session.id, 7, "assistant", "text", f"Original {index}")
        await manager.archive_session(session.id, replace=False)
        targets.append(session)
    foreign_agent = await database.get_system_agent(other)
    foreign = await manager.create_session(foreign_agent["id"], "Foreign", str(tmp_path))
    await manager.archive_session(foreign.id, replace=False)
    target = targets[3]
    generation = (await client.get(f"/api/sessions/{target.id}/restoration")).json()["service_generation"]

    async def read(sid):
        path = f"/api/sessions/{sid}"
        if operation == "restore":
            return await client.post(path + "/unarchive", json={"operation_id": f"restore-{sid}", "service_generation": generation})
        return await client.get(path + ("/info" if operation == "info" else ""))

    statements = []
    await database.conn.set_trace_callback(statements.append)
    try:
        response = await read(target.id)
        assert response.status_code == 200
        detail = response.json()
        if operation == "restore":
            assert detail["state"] == "finished" and detail["status_code"] is None
            current = manager.get_session(target.id)
            assert current._message_count == 1 and current._next_message_seq == 8
        else:
            assert detail["id"] == target.id and detail["message_count"] == 1
        if operation == "detail":
            assert [row["content"] for row in detail["messages"]] == ["Original 3"]
        queries = history_queries(statements)
        assert queries and all(target.id in sql for sql in queries), "\n".join(queries)
        foreign_reply, missing_reply = await read(foreign.id), await read("missing-archive")
        assert foreign_reply.status_code == missing_reply.status_code == 404
        assert foreign_reply.json() == missing_reply.json()
        listed = (await client.get("/api/sessions?include_archived=true")).json()
        assert len(listed) == 8
        assert sum(not row["archived"] for row in listed) == (1 if operation == "restore" else 0)
    finally:
        await database.conn.set_trace_callback(None)
