"""Asynchronous jobs keep their own committed request and direct caller."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from server.bg_tasks import BgTaskManager, BgTaskRecord
from server.database import Database
from server.harness import Harness, HarnessEvent, get_harness
from server.harness.registry import _REGISTRY
from server.knowledge import AdmittedRequest, admit, decode_owned, derive, encode
from server.mcp_identity import (
    McpScope,
    close_work,
    register_run,
    revoke_run,
    scope_is_live,
)
from server.research.manager import ResearchManager
from server.session_manager import SessionManager
from tests.accounts import create_account
from tests.fake_run import FakeRunBase

CREATED_AT = "2026-10-19T12:00:00+00:00"


def _install_research_provider(monkeypatch, on_start):
    """Use the actual pipeline with a synthetic provider at its public seam."""
    profile = replace(
        get_harness("claude-code").profile, binary=sys.executable,
        build_oneshot_argv=lambda ctx: (
            [sys.executable, "-c", 'print(\'["one angle"]\')'], {"cwd": ctx.working_dir},
        ),
        parse_oneshot_stdout=lambda output: output,
    )
    harness = Harness(profile)
    monkeypatch.setitem(_REGISTRY, "claude-code", harness)

    def create(config):
        class Run(FakeRunBase):
            run_lease_id = uuid.uuid4().hex

            async def start(self, prompt, *args, **kwargs):
                if config.session_id is not None:
                    register_run(self.run_lease_id, config.session_id, config.user_id, config.work_id)
                await on_start(config, prompt, self)

            def set_idle_handler(self, handler):
                pass

            async def stream(self):
                yield HarnessEvent(type="result", is_error=False)

            async def stop(self):
                revoke_run(self.run_lease_id)

            async def interrupt(self):
                revoke_run(self.run_lease_id)

        return Run()

    monkeypatch.setattr(harness, "create_run", create)


@pytest.fixture
async def owned_database(tmp_path):
    db = Database(str(tmp_path / "async-jobs.db"))
    await db.initialize()
    user, _ = await create_account(db, extra_roots=[tmp_path])
    agent = await db.get_system_agent(user["id"])
    await db.save_session(
        "parent", "Parent", str(tmp_path), CREATED_AT,
        agent_id=agent["id"], user_id=user["id"],
    )
    try:
        yield db
    finally:
        await db.close()


async def test_background_contract_is_committed_before_process_launch(owned_database, tmp_path, monkeypatch):
    database = owned_database
    owner = (await database.get_session_binding("parent"))["user_id"]
    context = admit(
        session_id="parent", request_id="read-request", user_id=owner,
        prompt="/wiki Compare X and Y.",
    )
    owned = derive(AdmittedRequest(context.origin, context), "Run an offline check.")
    manager = BgTaskManager()
    delivered = asyncio.Queue()

    async def broadcast(event):
        pass

    manager.bind(database, delivered.put, broadcast)
    await manager.start()
    reader = Database(str(tmp_path / "async-jobs.db"))
    await reader.initialize()
    spawn = asyncio.create_subprocess_exec
    seen = []

    async def checked_spawn(*args, **kwargs):
        rows = await reader.list_bg_tasks_for_session("parent")
        assert len(rows) == 1
        seen.append(decode_owned(rows[0]["knowledge_context"]))
        return await spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", checked_spawn)
    try:
        started = await manager.start_task(
            session_id="parent", command="true", working_dir=str(tmp_path), owned_context=owned,
        )
        result = await asyncio.wait_for(delivered.get(), 5)
        assert seen == [owned]
        assert decode_owned(started.knowledge_context) == owned
        assert decode_owned(result.knowledge_context).return_binding.context == context
    finally:
        await manager.shutdown()
        await reader.close()


@pytest.mark.parametrize("committed", [False, True])
async def test_background_commit_failure_never_launches_a_process(tmp_path, monkeypatch, committed):
    path = tmp_path / "async-jobs.db"
    seed = Database(str(path))
    await seed.initialize()
    owner, _ = await create_account(seed, extra_roots=[tmp_path])
    await seed.close()
    context = admit(
        session_id="parent", request_id="background-request", user_id=owner["id"],
        prompt="/wiki Keep the original background request.",
    )
    owned = derive(AdmittedRequest(context.origin, context, "background-work"), "touch must-not-run")
    program = """
import asyncio
import sys
from pathlib import Path
from server.bg_tasks import BgTaskManager
from server.database import Database
from server.knowledge import decode_owned

async def main():
    path, working_dir, encoded, committed = sys.argv[1:5]
    root = Path(working_dir)
    database = Database(path)
    await database.initialize()
    owned = decode_owned(encoded)
    owner = owned.return_binding.reference.user_id
    assert owner is not None
    stamp = "2026-10-19T12:00:00+00:00"
    await database.save_agent(
        agent_id="background-agent", name="Background agent", created_at=stamp, updated_at=stamp,
        user_id=owner,
    )
    await database.save_session("parent", "Parent", working_dir, stamp, agent_id="background-agent", user_id=owner)
    await database.append_message(
        "parent", 0, "user", "text", "Original background request",
        tool_input={"request_context": {
            "request_id": "background-request", "work_id": "background-work",
        }},
    )
    await database.append_message("parent", 1, "assistant", "text", "Already acknowledged output")

    async def deliver(record):
        (root / "delivery-notice").write_text(record.id)
    async def broadcast(event):
        (root / "broadcast-notice").write_text(event["type"])
    manager = BgTaskManager()
    manager.bind(database, deliver, broadcast)
    await manager.start()

    spawn = asyncio.create_subprocess_exec
    async def observed_spawn(*args, **kwargs):
        (root / "spawn-attempted").touch()
        return await spawn(*args, **kwargs)
    asyncio.create_subprocess_exec = observed_spawn
    commit = database.conn.commit
    async def failed_commit():
        if committed == "True":
            await commit()
        raise OSError("background contract commit acknowledgement unavailable")
    database.conn.commit = failed_commit
    def record(sql):
        with (root / "storage-sql.log").open("a") as stream:
            stream.write(sql + "\\n")
    await database.conn.set_trace_callback(record)

    # The captured contract reaches the INSERT and its admission check in one
    # transaction, without an earlier internal-action session lookup commit.
    try:
        await manager.start_task(
            session_id="parent", command="touch must-not-run", working_dir=working_dir,
            work_id="background-work", owned_context=owned,
        )
    except BaseException:
        (root / "returned-storage-error").touch()
    else:
        (root / "published-success").touch()
    await manager.shutdown()
    await database.close()

asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(path), str(tmp_path), encode(owned), str(committed)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 70, result.stderr
    for marker in ("spawn-attempted", "must-not-run", "published-success", "returned-storage-error",
                   "delivery-notice", "broadcast-notice"):
        assert not (tmp_path / marker).exists(), marker
    statements = (tmp_path / "storage-sql.log").read_text().splitlines()
    inserts = [sql for sql in statements if sql.startswith("INSERT INTO bg_tasks ")]
    assert len(inserts) == 1
    assert statements.count("COMMIT") == int(committed)
    after_insert = statements[statements.index(inserts[0]) + 1:]
    assert not any(sql.startswith(("SELECT", "UPDATE", "ROLLBACK")) for sql in after_insert)

    notices = []
    async def notice(value):
        notices.append(value)
    spawn = asyncio.create_subprocess_exec
    async def observed_spawn(*args, **kwargs):
        (tmp_path / "spawn-attempted").touch()
        return await spawn(*args, **kwargs)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", observed_spawn)

    reopened = Database(str(path))
    await reopened.initialize()
    manager = BgTaskManager()
    manager.bind(reopened, notice, notice)
    try:
        assert await reopened.get_session_binding("parent") == {
            "id": "parent", "agent_id": "background-agent", "user_id": owner["id"], "archived": False,
        }
        messages = await reopened.load_messages("parent")
        assert [(row["seq"], row["role"], row["content"]) for row in messages] == [
            (0, "user", "Original background request"),
            (1, "assistant", "Already acknowledged output"),
        ]
        assert messages[0]["tool_input"] == {"request_context": {
            "request_id": "background-request", "work_id": "background-work",
        }}
        original = await reopened.list_bg_tasks_for_session("parent")
        assert len(original) == int(committed)
        if committed:
            row = original[0]
            assert f"'{row['id']}'" in inserts[0]
            assert (row["session_id"], row["work_id"], row["command"], row["working_dir"]) == (
                "parent", "background-work", "touch must-not-run", str(tmp_path),
            )
            assert (row["status"], row["exit_code"], row["completed_at"], row["stop_reason"]) == (
                "running", None, None, None,
            )
            assert decode_owned(row["knowledge_context"]) == owned

        # Startup classifies the old row as interrupted; it never replays it.
        await manager.start()
        restored = await manager.list_tasks("parent")
        assert len(restored) == int(committed)
        if committed:
            task = restored[0]
            assert (task.id, task.session_id, task.work_id, task.command) == (
                original[0]["id"], "parent", "background-work", "touch must-not-run",
            )
            assert (task.status, task.stop_reason, task.exit_code) == ("interrupted", "server_restart", None)
            assert task.completed_at is not None
            assert (task.stdout, task.stderr) == ("", "")
            assert decode_owned(task.knowledge_context) == owned
        settled = await reopened.list_bg_tasks_for_session("parent")
        assert await reopened.load_messages("parent") == messages
    finally:
        await manager.shutdown()
        await reopened.close()

    cold = Database(str(path))
    await cold.initialize()
    try:
        assert await cold.list_bg_tasks_for_session("parent") == settled
        assert await cold.load_messages("parent") == messages
    finally:
        await cold.close()
    assert notices == []
    assert not (tmp_path / "spawn-attempted").exists()
    assert not (tmp_path / "must-not-run").exists()


async def test_research_return_restores_original_sources_without_borrowing_wiki_registration(
    owned_database, tmp_path, monkeypatch,
):
    database = owned_database
    observed = asyncio.Queue()
    leaves = []

    async def on_start(config, prompt, run):
        if config.session_id is None:
            leaves.append(config)
        else:
            await observed.put((config, prompt))

    _install_research_provider(monkeypatch, on_start)
    sessions = SessionManager()
    await sessions.initialize(database)
    agent = await database.get_system_agent()
    session = await sessions.create_session(agent["id"], "Research", str(tmp_path))
    research = ResearchManager()
    research.bind(sessions, database)
    try:
        job = await research.start(session.id, "/wiki Compare X and Y.")
        owned = decode_owned(job["knowledge_context"])
        assert owned is not None
        assert owned.return_binding.context.mode == "auto"
        assert owned.return_binding.context.sources == ("wiki", "web")
        assert owned.return_binding.context.maintenance.may_submit is False
        config, prompt = await asyncio.wait_for(observed.get(), 5)
        assert config.wiki_mode is None  # This account has no registered vault.
        assert f"[deep-research:{job['id']}]" in prompt
        assert "/wiki Compare X and Y." in prompt
        assert leaves
        assert all(leaf.mcp_servers == [] and leaf.connectors == [] and leaf.memory_dir is None for leaf in leaves)
    finally:
        await research.shutdown()
        await sessions.drain_turns()


@pytest.mark.parametrize("return_state", ["retired_lease", "new_root", "closed_work", "missing_context", "unknown_context", "cancelled_work"])
async def test_research_return_uses_fixed_parent_after_lease_retires(
    owned_database, tmp_path, monkeypatch, return_state,
):
    database = owned_database
    sessions = SessionManager()
    await sessions.initialize(database)
    agent = await database.get_system_agent()
    session = await sessions.create_session(agent["id"], "Scoped research", str(tmp_path))
    research = ResearchManager()
    research.bind(sessions, database)
    launched = []
    resumed = asyncio.Queue()
    scopes = []
    work_id = None if return_state == "new_root" else "research-work-" + uuid.uuid4().hex
    leaf_started = asyncio.Event()
    release_leaf = asyncio.Event()

    async def on_start(config, prompt, run):
        if config.session_id is None:
            if return_state in {"cancelled_work", "new_root"}:
                leaf_started.set()
                await release_leaf.wait()
            return
        if launched:
            if f"[deep-research:{launched[0]['id']}]" in prompt:
                await resumed.put(config)
            return
        scope = McpScope(
            session.id, user_id=session.user_id, wiki_task_id=config.wiki_task_id,
            run_lease_id=run.run_lease_id, work_id=config.work_id,
        )
        scopes.append(scope)
        launched.append(await research.start(session.id, "External evidence only", scope=scope))

    _install_research_provider(monkeypatch, on_start)

    delivery = asyncio.Event()

    async def finished(event):
        if event["type"] == "research_delivery":
            delivery.set()
        if event["type"] != "research_completed":
            return
        if return_state == "closed_work":
            close_work(work_id)
        elif return_state in {"missing_context", "unknown_context"}:
            await database.update_research_job(
                event["research_id"], knowledge_context=(
                    None if return_state == "missing_context" else '{"version":999,"value":{}}'
                ),
            )

    sessions.on_broadcast("return-contract", finished)
    try:
        original = await sessions.start_message(session.id, "/wiki Compare X and Y.", work_id=work_id, wiki_mode="read")
        assert (await original.wait()).outcome.status == "success"
        assert len(launched) == 1
        assert not scope_is_live(scopes[0])
        if return_state == "cancelled_work":
            await asyncio.wait_for(leaf_started.wait(), 5)
            close_work(work_id)
            await research.cancel_work(work_id)
        elif return_state == "new_root":
            await asyncio.wait_for(leaf_started.wait(), 5)
            later = await sessions.start_message(session.id, "A separate ordinary request.")
            assert (await later.wait()).outcome.status == "success"
            release_leaf.set()
        async with asyncio.timeout(5):
            while True:
                row = await database.get_research_job(launched[0]["id"])
                if (row["status"] != "running" if return_state == "cancelled_work"
                        else row["injection_status"] != "pending"):
                    break
                await asyncio.sleep(0.01)
        if return_state != "cancelled_work":
            await asyncio.wait_for(delivery.wait(), 5)
        if return_state == "cancelled_work":
            assert row["status"] == "cancelled"
            assert row["report_path"] is None
            assert resumed.empty()
        elif return_state in {"retired_lease", "new_root"}:
            assert row["status"] == "completed"
            config = await asyncio.wait_for(resumed.get(), 5)
            assert config.wiki_mode == "read"
            assert config.work_id == work_id
            assert row["injection_status"] == "delivered"
        else:
            assert row["status"] == "completed"
            assert row["injection_status"] == "failed"
            assert resumed.empty()
            assert row["report_path"]
            inputs = [message for message in await database.load_messages(session.id) if message["role"] == "user"]
            assert len(inputs) == 1
            notices = [message for message in await database.load_messages(session.id) if message["type"] == "delivery_notice"]
            assert len(notices) == 1
            assert "retained as material" in notices[0]["content"]
            assert row["report_path"] in notices[0]["content"]
    finally:
        release_leaf.set()
        await research.shutdown()
        await sessions.drain_turns()


@pytest.mark.parametrize("contract", ["missing", "unknown", "closed"])
async def test_background_material_retention_is_visible_once_without_starting_a_turn(owned_database, tmp_path, contract):
    database = owned_database
    sessions = SessionManager()
    await sessions.initialize(database)
    owner = (await database.get_session_binding("parent"))["user_id"]
    context = admit(session_id="parent", request_id="original", user_id=owner, prompt="/wiki Read X")
    work_id = "closed-material-" + uuid.uuid4().hex if contract == "closed" else None
    owned = derive(AdmittedRequest(context.origin, context, work_id), "Check X")
    if work_id:
        close_work(work_id)
    rec = BgTaskRecord(
        "material-one", "parent", "true", None, "/tmp", "completed", 0,
        "retained output", "", False, CREATED_AT, CREATED_AT,
        work_id=work_id, knowledge_context=(
            None if contract == "missing" else '{"version":999,"value":{}}'
            if contract == "unknown" else encode(owned)
        ),
    )
    await database.create_bg_task(
        rec.id, rec.session_id, rec.command, rec.description, rec.working_dir, rec.started_at,
        work_id=rec.work_id, knowledge_context=rec.knowledge_context,
    )
    await database.update_bg_task(rec.id, status="completed", stdout=rec.stdout, exit_code=0)
    task_before = await database.get_bg_task(rec.id)
    reader = Database(str(tmp_path / "async-jobs.db"))
    await reader.initialize()
    try:
        assert await asyncio.gather(sessions.deliver_bg_result(rec), sessions.deliver_bg_result(rec)) == [False, False]
        messages = await reader.load_messages("parent")
        assert [message["type"] for message in messages] == ["delivery_notice"]
        assert messages[0]["role"] == "system"
        assert "material-one" in messages[0]["content"]
        assert "retained" in messages[0]["content"]
        assert sessions.dispatch_snapshot("parent")["items"] == []
        restored = SessionManager()
        await restored.initialize(database)
        assert await restored.deliver_bg_result(rec) is False
        assert await reader.load_messages("parent") == messages
        assert await database.get_bg_task(rec.id) == task_before
    finally:
        await reader.close()
