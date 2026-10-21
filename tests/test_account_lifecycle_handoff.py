"""Account adoption joins the original lifecycle command, including file work."""

import asyncio
import shutil
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from server import deps
from server.attachments import save_upload
from server.config import settings
from server.database import Database
from server.harness import get_harness
from server.harness.profile import StdinMode
from server.main import app
from server.models import MessageContent
from server.routers import auth as auth_router
from server.session_manager import ForkError, SessionManager
from server.users import UserManager


@pytest.fixture
async def legacy_install(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "master_key", "lifecycle-test-master")
    monkeypatch.setattr(settings, "auth_token", "lifecycle-legacy-token")
    monkeypatch.setattr(settings, "agents_dir", str(tmp_path / "legacy-agents"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    database = Database(str(tmp_path / "lifecycle.db"))
    await database.initialize()
    manager = SessionManager()
    await manager.initialize(database)
    agent = await database.get_system_agent()
    monkeypatch.setattr(auth_router, "_db", database)
    deps.set_user_manager(UserManager(database))
    app.dependency_overrides[deps.get_session_manager] = lambda: manager
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield database, manager, agent, client
    finally:
        app.dependency_overrides.pop(deps.get_session_manager, None)
        await database.close()


@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_bootstrap_joins_duplicate_file_work_and_rejects_new_lifecycle_commands(
    legacy_install, tmp_path, monkeypatch, cancel_waiter,
):
    database, manager, agent, client = legacy_install
    source = tmp_path / "source"
    source.mkdir()
    original = b"Original workspace bytes\x00\xff\n"
    (source / "evidence.bin").write_bytes(original)
    parent = await manager.import_session(
        "Original", str(source), agent_id=agent["id"], backend="codex",
        messages=[MessageContent(role="user", type="text", content="Original request")],
    )
    archived = await manager.import_session(
        "Recovery evidence", str(source), agent_id=agent["id"],
        messages=[MessageContent(role="user", type="text", content="Retained archived request")],
    )
    await manager.archive_session(archived.id, replace=False)
    copying = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    copytree = shutil.copytree

    def held_copy(before, after, *args, **kwargs):
        if Path(before) == source:
            loop.call_soon_threadsafe(copying.set)
            if not release.wait(5):
                raise TimeoutError("duplicate copy was not released")
        return copytree(before, after, *args, **kwargs)

    monkeypatch.setattr(shutil, "copytree", held_copy)
    duplicate = asyncio.create_task(manager.duplicate_session(parent.id, label="Copied"))
    claim = None
    try:
        await asyncio.wait_for(copying.wait(), 2)
        claim = asyncio.create_task(client.post(
            "/api/auth/bootstrap",
            json={"username": "owner", "password": "password1"},
            headers={"Authorization": "Bearer lifecycle-legacy-token"},
        ))
        done, _ = await asyncio.wait({claim}, timeout=.2)
        assert not done, "account adoption overtook an unfinished filesystem command"
        assert await database.count_users() == 0, "C1 account commit must wait for the original command"

        if cancel_waiter:
            duplicate.cancel()
            with pytest.raises(asyncio.CancelledError):
                await duplicate
            done, _ = await asyncio.wait({claim}, timeout=.2)
            assert not done, "cancelled waiter released an unfinished filesystem command"
            assert await database.count_users() == 0

        with pytest.raises(ValueError, match="handoff"):
            await manager.create_session(agent["id"], working_dir=str(source))
        with pytest.raises(ValueError, match="handoff"):
            await manager.import_session("Too late", str(source), agent_id=agent["id"])
        with pytest.raises(ValueError, match="handoff"):
            await manager.unarchive_session(archived.id)
        with pytest.raises(ForkError) as fork_error:
            await manager.fork_session(parent.id, 0)
        assert fork_error.value.status_code == 409
        with pytest.raises(ForkError) as copy_error:
            await manager.duplicate_session(parent.id)
        assert copy_error.value.status_code == 409
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test",
        ) as http:
            refused_delete = await http.delete(
                f"/api/sessions/{parent.id}",
                headers={"Authorization": "Bearer lifecycle-legacy-token"},
            )
            refused_archive = await http.post(
                f"/api/sessions/{parent.id}/archive?replace=true",
                headers={"Authorization": "Bearer lifecycle-legacy-token"},
            )
            refused_hide = await http.post(
                f"/api/sessions/{parent.id}/archive?replace=false",
                headers={"Authorization": "Bearer lifecycle-legacy-token"},
            )
            refused_unarchive = await http.post(
                f"/api/sessions/{archived.id}/unarchive",
                headers={"Authorization": "Bearer lifecycle-legacy-token"},
            )
        assert refused_delete.status_code == 409, refused_delete.text
        assert "retry after account recovery" in refused_delete.json()["detail"]
        assert {
            "archive with replacement": refused_archive.status_code,
            "archive without replacement": refused_hide.status_code,
            "unarchive": refused_unarchive.status_code,
        } == {
            "archive with replacement": 409,
            "archive without replacement": 409,
            "unarchive": 409,
        }
        for response in (refused_archive, refused_hide, refused_unarchive):
            assert "retry after account recovery" in response.json()["detail"]
        assert manager.get_session(parent.id) is parent
        assert (source / "evidence.bin").read_bytes() == original
        assert [row["content"] for row in await database.load_messages(parent.id)] == ["Original request"]
        recovery = await manager.load_archived_session_detail(archived.id)
        assert recovery is not None
        assert [message.content for message in recovery.messages] == ["Retained archived request"]

        release.set()
        if not cancel_waiter:
            await asyncio.wait_for(duplicate, 3)
        response = await asyncio.wait_for(claim, 3)
        assert response.status_code == 200, response.text
        owner = response.json()["user_id"]
        children = [session for session in manager.list_sessions(owner) if session.forked_from_session_id == parent.id]
        assert len(children) == 1
        child = children[0]
        assert manager.get_session(child.id, owner) is child
        assert await database.get_session_owner(child.id) == owner
        assert (Path(child.working_dir) / "evidence.bin").read_bytes() == original
        assert (source / "evidence.bin").read_bytes() == original
        assert [row["content"] for row in await database.load_messages(child.id)] == ["Original request"]
        assert child.fork_status == "ready"
    finally:
        release.set()
        await asyncio.gather(duplicate, *([claim] if claim is not None else []), return_exceptions=True)


@pytest.mark.parametrize("cleanup_after_transition", [False, True])
async def test_bootstrap_joins_archive_retirement_and_its_original_replacement(
    legacy_install, tmp_path, monkeypatch, cleanup_after_transition,
):
    database, manager, agent, client = legacy_install
    retiring, release = tmp_path / "retiring", tmp_path / "release-child"
    program = tmp_path / "child.py"
    program.write_text(
        "import json,sys,time\n"
        "from pathlib import Path\n"
        "sys.stdin.readline()\n"
        "print(json.dumps({'type':'thread.started','thread_id':'archive-thread'}),flush=True)\n"
        "print(json.dumps({'type':'item.completed','item':{'id':'reply','type':'agent_message','text':'Original reply'}}),flush=True)\n"
        "print(json.dumps({'type':'turn.completed','usage':{}}),flush=True)\n"
        "sys.stdin.read()\n"
        "Path(sys.argv[1]).write_text('stdin closed; retirement remains pending')\n"
        "while not Path(sys.argv[2]).exists(): time.sleep(.01)\n"
    )
    harness = get_harness("codex")
    monkeypatch.setattr(harness, "profile", replace(
        harness.profile, binary=sys.executable, stdin_mode=StdinMode.STREAM_JSON,
        activity_observer=None, prepare_resume=None,
        build_turn_argv=lambda context: (
            [sys.executable, "-u", str(program), str(retiring), str(release)],
            {"cwd": context.working_dir},
        ),
    ))
    processes = []
    spawn = asyncio.create_subprocess_exec

    async def observe_spawn(*argv, **kwargs):
        process = await spawn(*argv, **kwargs)
        if str(program) in argv:
            processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observe_spawn)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "original.txt").write_text("Keep the original working files.\n")
    parent = await manager.create_session(agent["id"], "Original", str(workspace), backend="codex")
    publishing, finish_publication = asyncio.Event(), asyncio.Event()

    async def held_archive_publication(event):
        if cleanup_after_transition and event["type"] == "session_archived":
            publishing.set()
            await finish_publication.wait()

    manager.on_broadcast("archive-cleanup-publication", held_archive_publication)

    async def bootstrap():
        return await client.post(
            "/api/auth/bootstrap",
            json={"username": "owner", "password": "password1"},
            headers={"Authorization": "Bearer lifecycle-legacy-token"},
        )

    archive = claim = None
    try:
        receipt = await manager.start_message(parent.id, "Original request")
        completion = await asyncio.wait_for(receipt.wait(), 3)
        assert completion.outcome.status == "success"
        if cleanup_after_transition:
            claim = asyncio.create_task(bootstrap())
        else:
            archive = asyncio.create_task(manager.archive_session(parent.id))
        async with asyncio.timeout(2):
            while not retiring.exists():
                await asyncio.sleep(.01)
        assert processes[0].returncode is None
        if cleanup_after_transition:
            with pytest.raises(ValueError, match="handoff"):
                await manager.archive_session(parent.id, replace=False)
            with pytest.raises(ValueError, match="handoff"):
                await manager.delete_session(parent.id)
            archive = asyncio.create_task(manager.archive_session(
                parent.id, replace=False, only_request_id=receipt.request_id,
            ))
        else:
            claim = asyncio.create_task(bootstrap())
        done, _ = await asyncio.wait({claim}, timeout=.2)
        assert not done, "adoption overtook original archive retirement and replacement"
        assert await database.count_users() == 0
        release.write_text("finish the original archive")
        if cleanup_after_transition:
            await asyncio.wait_for(publishing.wait(), 3)
            done, _ = await asyncio.wait({claim}, timeout=.2)
            assert not done, "adoption lost cleanup registered by original request retirement"
            assert await database.count_users() == 0
            finish_publication.set()
        replacement = await asyncio.wait_for(archive, 3)
        response = await asyncio.wait_for(claim, 3)
        assert response.status_code == 200, response.text
        owner = response.json()["user_id"]
        assert processes[0].returncode == 0
        assert manager.get_session(parent.id) is None
        archived = await manager.load_archived_session_detail(parent.id, owner)
        assert archived is not None
        assert any(message.content == "Original request" for message in archived.messages)
        if cleanup_after_transition:
            assert replacement is None
        else:
            assert replacement is not None
            assert manager.get_session(replacement.id, owner) is replacement
            assert await database.get_session_owner(replacement.id) == owner
            assert replacement.working_dir == str(workspace)
        assert (workspace / "original.txt").read_text() == "Keep the original working files.\n"
    finally:
        finish_publication.set()
        release.write_text("finish cleanup")
        await asyncio.gather(*[task for task in (archive, claim) if task is not None], return_exceptions=True)
        manager.remove_broadcast("archive-cleanup-publication")
        for process in processes:
            if process.returncode is None:
                process.kill()
            await process.wait()


async def test_bootstrap_joins_deleted_session_file_cleanup_after_waiter_cancellation(
    legacy_install, tmp_path, monkeypatch,
):
    database, manager, agent, client = legacy_install
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = b"Preserve surviving workspace bytes\x00\xff"
    (workspace / "original.bin").write_bytes(original)
    parent = await manager.create_session(agent["id"], "Delete", str(workspace))
    survivor = await manager.create_session(agent["id"], "Keep", str(workspace))
    uploaded = save_upload(parent.id, "uploaded.txt", b"Deleted session attachment")
    removing = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    rmtree = shutil.rmtree

    def held_remove(path, *args, **kwargs):
        if Path(path) == uploaded.path.parent:
            loop.call_soon_threadsafe(removing.set)
            if not release.wait(5):
                raise TimeoutError("delete files were not released")
        return rmtree(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", held_remove)
    deletion = asyncio.create_task(manager.delete_session(parent.id))
    claim = None
    try:
        await asyncio.wait_for(removing.wait(), 2)
        deletion.cancel()
        with pytest.raises(asyncio.CancelledError):
            await deletion
        claim = asyncio.create_task(client.post(
            "/api/auth/bootstrap",
            json={"username": "owner", "password": "password1"},
            headers={"Authorization": "Bearer lifecycle-legacy-token"},
        ))
        done, _ = await asyncio.wait({claim}, timeout=.2)
        assert not done, "adoption overtook the deleted session's original file cleanup"
        assert await database.count_users() == 0
        assert uploaded.path.read_bytes() == b"Deleted session attachment"
        release.set()
        response = await asyncio.wait_for(claim, 3)
        assert response.status_code == 200, response.text
        owner = response.json()["user_id"]
        assert manager.get_session(parent.id) is None
        assert parent.id not in {row["id"] for row in await database.load_sessions(include_archived=True)}
        assert not uploaded.path.parent.exists()
        assert manager.get_session(survivor.id, owner) is survivor
        assert await database.get_session_owner(survivor.id) == owner
        assert (workspace / "original.bin").read_bytes() == original
    finally:
        release.set()
        await asyncio.gather(deletion, *([claim] if claim is not None else []), return_exceptions=True)


@pytest.mark.parametrize("operation", ["create", "import"])
async def test_cancelled_session_lifecycle_waiter_keeps_original_installation_for_adoption(
    legacy_install, tmp_path, monkeypatch, operation,
):
    database, manager, agent, client = legacy_install
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "original.txt").write_text("Retain original creation inputs.\n")
    inserting, release = asyncio.Event(), asyncio.Event()
    execute = database.conn.execute

    async def held_insert(sql, parameters=()):
        if sql.startswith("INSERT INTO sessions "):
            inserting.set()
            await release.wait()
        return await execute(sql, parameters)

    monkeypatch.setattr(database.conn, "execute", held_insert)
    if operation == "create":
        command = asyncio.create_task(manager.create_session(agent["id"], "Original", str(workspace)))
    else:
        command = asyncio.create_task(manager.import_session(
            "Original", str(workspace), agent_id=agent["id"],
            messages=[MessageContent(role="user", type="text", content="Imported original request")],
        ))
    claim = None
    try:
        await asyncio.wait_for(inserting.wait(), 2)
        command.cancel()
        done, _ = await asyncio.wait({command}, timeout=.2)
        assert command in done, "cancelled waiter remained attached to the original SQL command"
        with pytest.raises(asyncio.CancelledError):
            await command
        claim = asyncio.create_task(client.post(
            "/api/auth/bootstrap",
            json={"username": "owner", "password": "password1"},
            headers={"Authorization": "Bearer lifecycle-legacy-token"},
        ))
        done, _ = await asyncio.wait({claim}, timeout=.2)
        assert not done, "cancelled lifecycle waiter dropped the original session installation"
        release.set()
        response = await asyncio.wait_for(claim, 3)
        assert response.status_code == 200, response.text
        owner = response.json()["user_id"]
        installed = manager.list_sessions(owner)
        assert len(installed) == 1 and installed[0].name == "Original"
        session = installed[0]
        assert await database.get_session_owner(session.id) == owner
        assert session.working_dir == str(workspace)
        expected = ["Imported original request"] if operation == "import" else []
        assert [row["content"] for row in await database.load_messages(session.id)] == expected
        assert (workspace / "original.txt").read_text() == "Retain original creation inputs.\n"
    finally:
        release.set()
        await asyncio.gather(command, *([claim] if claim is not None else []), return_exceptions=True)
