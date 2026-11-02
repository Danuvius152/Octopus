"""The four existing save boundaries keep their distinct native-data scopes."""

import asyncio
import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server.config import settings
from server.database import Database
from server.deps import get_session_manager, get_user_manager
from server.portability import export_account, import_account
from server.question_resolution import AnswerSource
from server.routers.sessions import router
from server.sessions import SessionManager
from server.sessions.client_submissions import submission_status, submit_client_message
from tests import native_app_server as native_tests
from tests.native_app_server import ROOT, child_start, inject, item, terminal, until
from tests.session_runtime import wait_dispatch_idle
from tests.test_service_runtime import installation_settings

native_runtime = native_tests.native_runtime


@pytest.fixture(autouse=True)
def preservation_settings(tmp_path, monkeypatch):
    config, _ = installation_settings(tmp_path)
    config.db_path = str(tmp_path / "product.db")
    for name, value in config.model_dump().items():
        monkeypatch.setattr(settings, name, value)
    return config


@pytest.fixture
async def saved_native(native_runtime, tmp_path):
    manager, database, session, mode, _, _ = native_runtime
    mode[0] = "control_question"
    first = await submit_client_message(manager, session.id, "Original saved input", [], "first", wiki_mode=None)
    await until(lambda: manager.get_pending_questions(session.id))
    question = manager.get_pending_questions(session.id)[0]
    assert await manager.answer_question(session.id, question.question_id, [{"selected": ["B"]}],
        source=AnswerSource("account", user_id=session.user_id))
    await until(lambda: not manager.get_pending_questions(session.id))
    await submit_client_message(manager, session.id, "Accepted steering without processing proof", [], "steer", wiki_mode=None)
    async with asyncio.timeout(3):
        while not (await database.get_client_submission(session.id, "steer"))["native_inputs"]:
            await asyncio.sleep(.01)
        while (await database.get_client_submission(session.id, "steer"))["native_inputs"][0]["state"] != "accepted":
            await asyncio.sleep(.01)
    inject(tmp_path, child_start(), item("child", "child-turn", "partial", "Retained helper partial"),
           item(ROOT, "turn-1", "answer", "Bound root answer"), terminal())
    await wait_dispatch_idle(manager, session.id)
    (tmp_path / "native-injections.jsonl").unlink()
    mode[0] = "unknown"
    unknown = await submit_client_message(manager, session.id, "Do not replay uncertain input", [], "unknown", wiki_mode=None)
    await wait_dispatch_idle(manager, session.id)
    assert (await submission_status(manager, session.id, "unknown"))["execution"]["state"] == "unknown"
    return manager, database, session, first["request_id"], unknown["request_id"]


async def test_transcript_export_keeps_native_message_rows_without_exporting_submission_table(saved_native):
    manager, database, session, first, _ = saved_native
    before = await database.load_messages(session.id)
    token = await get_user_manager().issue_token(session.user_id)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session_manager] = lambda: manager
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Authorization": "Bearer " + token}) as client:
        response = await client.get(f"/api/sessions/{session.id}/transcript-export")
    assert response.status_code == 200
    rows = [json.loads(line) for line in response.text.splitlines()]
    assert len(rows) == len(before)
    root = next(row for row in rows if row["type"] == "text" and json.loads(row["content"]) == "Bound root answer")
    assert json.loads(root["runtime_metadata"])["native"]["thread_id"] == ROOT
    child = next(row for row in rows if row["type"] == "text" and json.loads(row["content"]) == "Retained helper partial")
    assert json.loads(child["runtime_metadata"])["native"]["thread_id"] == "child"
    selected = next(json.loads(row["content"]) for row in rows if row["type"] == "result"
                    and json.loads(row["content"])["request_id"] == first)
    assert selected["final_answer"]["message_seqs"] == [root["seq"]]
    assert any(row["type"] == "question_answer" for row in rows)
    assert all("native_inputs" not in row and "payload_sha256" not in row for row in rows)
    assert await database.load_messages(session.id) == before


async def test_database_and_referenced_file_backup_preserves_results_without_restoring_execution(saved_native, tmp_path, monkeypatch):
    manager, database, session, first, _ = saved_native
    root = tmp_path / "backup"
    root.mkdir()
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("Preserved effect\n")
    for source, name in ((artifact, "artifact.txt"), (tmp_path / "native-history.jsonl", "native-history.jsonl"),
                         (Path(settings.master_key_file), "master.key")):
        shutil.copyfile(source, root / name)

    def snapshot():
        with closing(sqlite3.connect(Path(database._db_path).as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(root / "product.db")) as target:
                source.backup(target)

    await asyncio.to_thread(snapshot)
    before = await database.load_messages(session.id)
    monkeypatch.setattr(settings, "master_key_file", str(root / "master.key"))
    restored = Database(str(root / "product.db"))
    await restored.initialize()
    replacement = SessionManager()
    try:
        await replacement.initialize(restored, previous_writers_retired=True)
        result = await restored.read_session_result(session.id, first)
        assert result.reply_text() == "Bound root answer"
        assert await restored.load_messages(session.id) == before
        row = await restored.get_client_submission(session.id, "unknown")
        assert row["execution_state"] == "unknown" and row["native_inputs"][0]["state"] == "unknown"
        accepted = await restored.get_client_submission(session.id, "first")
        assert accepted["native_inputs"][0]["state"] == "accepted"
        steer = await restored.get_client_submission(session.id, "steer")
        assert steer["native_inputs"][0]["kind"] == "steer"
        assert steer["native_inputs"][0]["state"] == "accepted"
        assert steer["native_inputs"][0]["target_turn_id"] == "turn-1"
        cards = [row["tool_input"] for row in before if row["type"] == "native_subagent"]
        assert cards[-1]["stop_reason"] == "parent_done"
        assert replacement.dispatch_snapshot(session.id)["active_request_id"] is None
        assert not replacement.get_pending_questions(session.id)
        assert len((tmp_path / "native-business.jsonl").read_text().splitlines()) == 2
        assert (root / "native-history.jsonl").read_bytes() == (tmp_path / "native-history.jsonl").read_bytes()
        assert (root / "artifact.txt").read_text() == "Preserved effect\n"
        detail = await restored.read_native_details(session.id, session.user_id,
            next(row["seq"] for row in before if row["type"] == "native_subagent"))
        assert [row.content for row in detail.messages] == ["Retained helper partial"]
    finally:
        await replacement.close_session_owners()
        await restored.close()


async def test_account_archive_excludes_native_chat_and_input_facts_without_changing_source(
    saved_native, preservation_settings, tmp_path,
):
    _, database, session, _, _ = saved_native
    before = await database.load_messages(session.id)
    workspace = Path(settings.users_root) / session.user_id / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "ordinary.txt").write_text("Keep ordinary account files")
    archive = tmp_path / "account.zip"
    await asyncio.to_thread(export_account, preservation_settings, session.user_id, archive, home=tmp_path.parent)
    target = tmp_path / "imported"
    copied_settings = await asyncio.to_thread(import_account, archive, home=target)
    copied = Database(copied_settings.db_path)
    await copied.initialize()
    try:
        assert await copied.load_sessions(include_archived=True) == []
        assert await copied.get_client_submission(session.id, "first") is None
        assert await copied.get_client_submission(session.id, "unknown") is None
        assert await copied.get_client_submission(session.id, "steer") is None
        assert (Path(copied_settings.users_root) / session.user_id / "workspace" / "ordinary.txt").read_text() == "Keep ordinary account files"
        assert await database.load_messages(session.id) == before
        assert (await database.get_client_submission(session.id, "unknown"))["native_inputs"][0]["state"] == "unknown"
    finally:
        await copied.close()
