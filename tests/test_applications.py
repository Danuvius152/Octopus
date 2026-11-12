"""Tests for agent-built applications (docs/implemented/applications.md).

Covers:
  * Path helpers — identity-based allocation, entrypoint validation,
    and the traversal guard the static route depends on.
  * `ApplicationManager` — create wires a build session (origin='application',
    working_dir = the app dir) and fires the brief; request completion
    turns build sessions into ready/failed, while progress marks building; follow-up builds
    reuse the session (and rebuild one when it's gone); rename/entrypoint
    updates; delete removes the directory but only inside the managed root.
  * The REST routes under `/api/applications`.
  * The static route `/apps/{id}/…` — bearer / query / cookie auth, directory
    → entrypoint, traversal → 404, no-store.

No real harness ever runs: `start_message` is patched so the captured prompt
is what's asserted, exactly like the delegation suite.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

import pytest
from httpx import ASGITransport, AsyncClient

from server import deps
from server.agent_manager import AgentManager
from server.app_agent import app_agent_manager
from server.applications import (
    ApplicationError,
    ApplicationManager,
    allocate_app_dir,
    is_inside_root,
    is_safe_relative_path,
    resolve_within,
    slugify,
)
from server.applications import (
    application_manager as singleton_application_manager,
)
from server.config import settings
from server.database import Database
from server.main import app
from server.routers import agents as agents_mod
from server.routers import applications as applications_mod
from server.session_manager import SessionManager, session_manager
from server.users import UserManager
from tests.accounts import create_account
from tests.completion_models import CompletionModels
from tests.fake_run import FakeRunBase

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def models():
    return CompletionModels()


@pytest.fixture
def apps_root(tmp_path, monkeypatch):
    root = tmp_path / "applications"
    monkeypatch.setattr(settings, "applications_dir", str(root))
    return root


@pytest.fixture
async def db(tmp_path):
    d = Database(":memory:")
    await d.initialize()
    await create_account(d, extra_roots=[tmp_path])
    yield d
    await d.close()


@pytest.fixture
async def mgr(db):
    m = SessionManager()
    await m.initialize(db)
    yield m


@pytest.fixture
async def am(mgr, db, apps_root):
    """Per-test ApplicationManager bound to the per-test session manager."""
    m = ApplicationManager()
    m.bind(session_mgr=mgr, db=db)
    yield m
    m.shutdown()


@pytest.fixture
def sent(mgr, monkeypatch, models):
    """Capture `start_message` calls instead of spawning a harness."""
    calls: list[tuple[str, str]] = []

    async def fake_start_message(session_id, prompt, attachment_ids=None, *, source, **kwargs):
        assert source == "application"
        calls.append((session_id, prompt))
        return await models.start_message(mgr, session_id, prompt, source=source, **kwargs)

    monkeypatch.setattr(mgr, "start_message", fake_start_message)
    return calls


async def _make_agent(db, name: str = "Octo Builder") -> dict:
    owner = await db.get_system_agent()
    return await AgentManager(db).create_agent(name=name, user_id=owner["user_id"])


async def _create_app(am, db, *, name="Todo App", **extra) -> dict:
    agent = extra.pop("agent", None) or await _make_agent(db, extra.pop("agent_name", name + " Agent"))
    return await am.create_application(
        name=name,
        description=extra.pop("description", "A todo list with checkboxes"),
        agent_id=agent["id"],
        **extra,
    )


@pytest.mark.parametrize("action", ["archive", "delete"])
@pytest.mark.parametrize("history", ["builder", "rewind"])
async def test_build_history_retires_its_background_writers_before_losing_link(
    am, db, mgr, sent, models, monkeypatch, tmp_path, action, history,
):
    from server import bg_tasks

    background = bg_tasks.BgTaskManager()

    async def discard(_):
        from server.sessions.submission import ReturnRefused

        return ReturnRefused("source_withdrawn")

    background.bind(db, discard, discard)
    monkeypatch.setattr(bg_tasks, "bg_task_manager", background)
    row = await _create_app(am, db)
    session_id = row["session_id"]
    if history == "rewind":
        await models.emit(mgr, {"type": "result", "session_id": session_id})
        session_id = (await mgr.fork_session(session_id, 0)).id
    unrelated = await mgr.create_session(agent_id=row["agent_id"])
    own = await background.start_task(
        session_id=session_id, command="sleep 120", working_dir=row["app_dir"],
    )
    other = await background.start_task(
        session_id=unrelated.id, command="sleep 120", working_dir=row["app_dir"],
    )
    try:
        if action == "archive":
            await mgr.archive_session(session_id, replace=False)
        else:
            await mgr.delete_session(session_id)
        assert own.id not in background._running
        assert other.id in background._running
        assert (await db.get_application(row["id"])) is not None
    finally:
        await background.shutdown()


@pytest.mark.parametrize("backend", ["claude-code", "codex"])
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("builder_removed", [False, True])
async def test_rewind_writers_retire_before_application_files_are_deleted(
    am, db, mgr, sent, models, monkeypatch, backend, legacy, builder_removed,
):
    from server import bg_tasks
    from server.process_groups import group_exists

    background = bg_tasks.BgTaskManager()

    async def discard(_):
        from server.sessions.submission import ReturnRefused

        return ReturnRefused("source_withdrawn")

    background.bind(db, discard, discard)
    monkeypatch.setattr(bg_tasks, "bg_task_manager", background)
    owner = await db.get_system_agent()
    agent = await AgentManager(db).create_agent(name="Rewind builder", backend=backend, user_id=owner["user_id"])
    row = await _create_app(am, db, agent=agent)
    await models.emit(mgr, {"type": "result", "session_id": row["session_id"]})
    if legacy:
        mgr.get_session(row["session_id"]).app_id = None
        async with db.transaction(write=True) as tx:
            await tx.execute("UPDATE sessions SET app_id=NULL WHERE id=?", (row["session_id"],))
    first = await mgr.fork_session(row["session_id"], 0)
    await db.append_message(session_id=first.id, seq=0, role="user", type="text", content="New build")
    successor = await mgr.fork_session(first.id, 0)
    # The UI's current build selection does not follow rewind.
    assert (await am.get_application(row["id"]))["session_id"] == row["session_id"]
    if builder_removed:
        await mgr.delete_session(row["session_id"])
        assert (await am.get_application(row["id"]))["session_id"] is None
        await mgr.initialize(db)
        successor = mgr.get_session(successor.id)
        assert successor is not None and successor.app_id == row["id"]
    # Same-directory sessions carry no application relationship.
    unrelated = await mgr.create_session(agent_id=agent["id"], working_dir=row["app_dir"])
    own = await background.start_task(session_id=successor.id, command="sleep 120", working_dir=row["app_dir"])
    other = await background.start_task(session_id=unrelated.id, command="sleep 120", working_dir=row["app_dir"])
    own_group = background._running[own.id].pgid
    other_group = background._running[other.id].pgid
    try:
        await am.delete_application(row["id"])
        assert not group_exists(own_group), "Deletion returned while a rewind writer could recreate application files"
        assert group_exists(other_group)
        assert (await background.get_task(own.id)).status == "cancelled"
        assert not Path(row["app_dir"]).exists()
        assert await db.get_application(row["id"]) is None
        assert successor.app_id == row["id"]
        assert mgr.get_session(successor.id) is successor
        assert not successor._admission_closed
        # Successful cleanup releases the history for explicitly requested new work.
        Path(row["app_dir"]).mkdir()
        fresh = await background.start_task(session_id=successor.id, command="true", working_dir=row["app_dir"])
        assert fresh.id != own.id
    finally:
        await background.shutdown()


@pytest.mark.parametrize("committed", [False, True])
async def test_application_delete_and_archive_cannot_lose_a_preparing_rewind(
    am, db, mgr, sent, models, monkeypatch, committed,
):
    from server.db.applications import ApplicationDeleting
    from server.sessions.base import ForkError

    row = await _create_app(am, db)
    await models.emit(mgr, {"type": "result", "session_id": row["session_id"]})
    entered, release = asyncio.Event(), asyncio.Event()
    create = db.create_fork_session

    async def held_create(**kwargs):
        if committed:
            result = await create(**kwargs)
        entered.set()
        await release.wait()
        return result if committed else await create(**kwargs)

    monkeypatch.setattr(db, "create_fork_session", held_create)
    pending = asyncio.create_task(mgr.fork_session(row["session_id"], 0))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        with pytest.raises(ForkError, match="fork"):
            await mgr.archive_session(row["session_id"], replace=False)
        assert mgr.get_session(row["session_id"]) is not None
        with pytest.raises(ApplicationError, match="fork"):
            await am.delete_application(row["id"])
        assert Path(row["app_dir"]).is_dir()
        assert (await am.get_application(row["id"]))["deletion"]["phase"] == "retiring"
        release.set()
        if committed:
            successor = await asyncio.wait_for(pending, 3)
            assert successor.app_id == row["id"]
            assert successor.id in await db.application_session_ids(row["id"])
        else:
            with pytest.raises((ForkError, ApplicationDeleting), match="delet"):
                await asyncio.wait_for(pending, 3)
            assert await db.application_session_ids(row["id"]) == [row["session_id"]]
        await am.delete_application(row["id"])
        assert await db.get_application(row["id"]) is None
        assert not Path(row["app_dir"]).exists()
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)


async def test_independent_application_copy_and_its_rewind_survive_original_deletion(
    am, db, mgr, sent, models, monkeypatch, tmp_path,
):
    from server.sessions.forking import ForkingMixin

    monkeypatch.setattr(ForkingMixin, "_fork_copy_base", staticmethod(lambda: str(tmp_path / "copies")))
    row = await _create_app(am, db)
    await models.emit(mgr, {"type": "result", "session_id": row["session_id"]})
    (Path(row["app_dir"]) / "independent.txt").write_text("Keep this full copy")
    copy = await mgr.duplicate_session(row["session_id"])
    assert copy.app_id is None
    await db.append_message(session_id=copy.id, seq=2, role="user", type="text", content="Independent work")
    successor = await mgr.fork_session(copy.id, 2)
    assert successor.app_id is None
    assert successor.id not in await db.application_session_ids(row["id"])
    await am.delete_application(row["id"])
    assert Path(successor.working_dir, "independent.txt").read_text() == "Keep this full copy"
    assert mgr.get_session(successor.id) is successor
    assert not successor._admission_closed


async def test_finished_rewind_does_not_release_a_later_rewinds_reservation(am, db, mgr, sent, models, monkeypatch):
    from server.sessions.base import ForkError

    row = await _create_app(am, db)
    await models.emit(mgr, {"type": "result", "session_id": row["session_id"]})
    first_tail, finish_first = asyncio.Event(), asyncio.Event()
    second_insert, finish_second = asyncio.Event(), asyncio.Event()
    archive, create = mgr._archive_original, db.create_fork_session
    archives, inserts = 0, 0

    async def held_archive(*args, **kwargs):
        nonlocal archives
        archives += 1
        if archives == 1:
            first_tail.set()
            await finish_first.wait()
        return await archive(*args, **kwargs)

    async def held_insert(**kwargs):
        nonlocal inserts
        result = await create(**kwargs)
        inserts += 1
        if inserts == 2:
            second_insert.set()
            await finish_second.wait()
        return result

    monkeypatch.setattr(mgr, "_archive_original", held_archive)
    monkeypatch.setattr(db, "create_fork_session", held_insert)
    first = asyncio.create_task(mgr.fork_session(row["session_id"], 0))
    second = None
    try:
        await asyncio.wait_for(first_tail.wait(), 3)
        second = asyncio.create_task(mgr.fork_session(row["session_id"], 0))
        await asyncio.wait_for(second_insert.wait(), 3)
        finish_first.set()
        await asyncio.wait_for(first, 3)
        with pytest.raises(ForkError, match="fork"):
            await mgr.archive_session(row["session_id"], replace=False)
        with pytest.raises(ApplicationError, match="fork"):
            await am.delete_application(row["id"])
        assert Path(row["app_dir"]).exists()
        finish_second.set()
        successor = await asyncio.wait_for(second, 3)
        assert mgr.get_session(successor.id) is successor
        await am.delete_application(row["id"])
        assert not Path(row["app_dir"]).exists()
        assert await db.get_application(row["id"]) is None
    finally:
        finish_first.set()
        finish_second.set()
        await asyncio.gather(first, *([second] if second else []), return_exceptions=True)


@pytest.mark.asyncio
async def test_application_creation_reports_failed_build_when_admission_is_closed(am, db, mgr):
    mgr.close_admission()
    row = await _create_app(am, db)
    assert row["status"] == "failed"
    assert "closed" in row["error"].lower()
    assert (await db.get_application(row["id"]))["status"] == "failed"
    assert row["session_id"] is None
    assert await db.application_session_ids(row["id"]) == []
    assert not any(session.origin == "application" for session in mgr.sessions.values())


@pytest.mark.parametrize("legacy_state", ["live", "archived", "restored_during_build"])
async def test_delete_retires_replaced_legacy_builder_history(am, db, mgr, sent, models, monkeypatch, legacy_state):
    from server import bg_tasks

    background = bg_tasks.BgTaskManager()

    async def discard(_):
        from server.sessions.submission import ReturnRefused

        return ReturnRefused("source_withdrawn")

    background.bind(db, discard, discard)
    monkeypatch.setattr(bg_tasks, "bg_task_manager", background)
    row = await _create_app(am, db)
    await models.emit(mgr, {"type": "result", "session_id": row["session_id"]})
    # Builders created before this change have only applications.session_id.
    original = mgr.get_session(row["session_id"])
    if legacy_state == "live":
        original.app_id = None
    else:
        await mgr.archive_session(original.id, replace=False)
    # Construct the old persisted shape; ordinary session settings deliberately
    # cannot change this identity field.
    async with db.transaction(write=True) as tx:
        await tx.execute("UPDATE sessions SET app_id=NULL WHERE id=?", (original.id,))
    stored = next(item for item in await db.load_sessions(include_archived=True) if item["id"] == original.id)
    assert stored["app_id"] is None
    if legacy_state == "live":
        target = await mgr.archive_session(original.id, replace=True)
    else:
        restored = []
        if legacy_state == "restored_during_build":
            retain = db.retain_application_session_owner

            async def restore_before_retention(session_id):
                if not restored:
                    restored.append(await mgr.unarchive_session(original.id))
                return await retain(session_id)

            monkeypatch.setattr(db, "retain_application_session_owner", restore_before_retention)
        await am.request_build(row["id"], "Rebuild with a new development session")
        if restored:
            assert restored[0].app_id is None
            target = await mgr.archive_session(original.id, replace=True)
        else:
            target = await mgr.unarchive_session(original.id)
        assert (await am.get_application(row["id"]))["session_id"] != original.id
    assert target is not None
    task = await background.start_task(
        session_id=target.id, command="sleep 120", working_dir=row["app_dir"],
    )
    try:
        await am.delete_application(row["id"])
        assert task.id not in background._running
        assert (await background.get_task(task.id)).status == "cancelled"
        assert await db.get_application(row["id"]) is None
        assert not Path(row["app_dir"]).exists()
    finally:
        await background.shutdown()


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def test_slugify_is_filesystem_safe():
    assert slugify("My Todo App!") == "my-todo-app"
    assert slugify("  ---  ") == "app"
    assert slugify("") == "app"
    assert len(slugify("x" * 200)) <= 48


def test_allocate_app_dir_uses_the_complete_application_identity(apps_root):
    first = allocate_app_dir("Notes", "original-identity")
    os.makedirs(first)
    second = allocate_app_dir("Notes", "replacement-identity")
    assert os.path.basename(first) == "notes--original-identity"
    assert os.path.basename(second) == "notes--replacement-identity"
    assert allocate_app_dir("Notes", "original-identity") == first


def test_is_safe_relative_path():
    assert is_safe_relative_path("index.html")
    assert is_safe_relative_path("assets/app.js")
    assert not is_safe_relative_path("")
    assert not is_safe_relative_path("/etc/passwd")
    assert not is_safe_relative_path("../outside.html")
    assert not is_safe_relative_path("assets/../../outside.html")


def test_resolve_within_blocks_traversal(tmp_path):
    base = tmp_path / "app"
    base.mkdir()
    (base / "index.html").write_text("hi")
    (tmp_path / "secret.txt").write_text("nope")

    assert resolve_within(str(base), "index.html") == str(base / "index.html")
    assert resolve_within(str(base), "") == str(base)
    assert resolve_within(str(base), "../secret.txt") is None
    assert resolve_within(str(base), "/../secret.txt") is None
    assert resolve_within(str(base), "a/b/../../../secret.txt") is None


def test_resolve_within_blocks_escaping_symlink(tmp_path):
    base = tmp_path / "app"
    base.mkdir()
    (tmp_path / "secret.txt").write_text("nope")
    os.symlink(tmp_path / "secret.txt", base / "link.txt")
    assert resolve_within(str(base), "link.txt") is None


def test_is_inside_root_guards_the_managed_tree(apps_root, tmp_path):
    os.makedirs(apps_root, exist_ok=True)
    assert is_inside_root(str(apps_root / "todo"))
    # The root itself is not "inside" it — deleting it would take every app.
    assert not is_inside_root(str(apps_root))
    assert not is_inside_root(str(tmp_path / "elsewhere"))
    assert not is_inside_root("/")


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_makes_dir_row_and_build_session(am, db, mgr, sent):
    agent = await _make_agent(db)
    row = await am.create_application(
        name="Todo App",
        description="A todo list with checkboxes",
        agent_id=agent["id"],
        icon="✅",
    )

    assert row["status"] == "building"
    assert row["icon"] == "✅"
    assert row["entrypoint"] == "index.html"
    assert os.path.isdir(row["app_dir"])
    assert os.path.basename(row["app_dir"]) == "todo-app--" + row["id"]

    session = mgr.get_session(row["session_id"])
    assert session is not None
    assert session.origin == "application"
    assert session.working_dir == row["app_dir"]
    assert session.agent_id == agent["id"]
    assert session.name == "Build: Todo App"

    # The brief went out on that session and carries the rendering contract.
    assert len(sent) == 1
    sid, prompt = sent[0]
    assert sid == session.id
    assert "Todo App" in prompt
    assert "A todo list with checkboxes" in prompt
    assert row["app_dir"] in prompt
    assert "index.html" in prompt
    assert "STATIC FILES" in prompt


@pytest.mark.asyncio
async def test_create_appends_extra_instructions(am, db, sent):
    agent = await _make_agent(db)
    await am.create_application(
        name="Notes",
        description="A notepad",
        agent_id=agent["id"],
        instructions="Use a dark theme and a monospace font.",
    )
    assert "Use a dark theme and a monospace font." in sent[0][1]


@pytest.mark.asyncio
async def test_create_rejects_duplicate_name_case_insensitively(am, db, sent):
    agent = await _make_agent(db)
    await am.create_application(
        name="Todo", description="d", agent_id=agent["id"]
    )
    with pytest.raises(ApplicationError) as e:
        await am.create_application(
            name="  todo  ", description="d", agent_id=agent["id"]
        )
    assert e.value.status_code == 409


@pytest.mark.asyncio
async def test_create_requires_name_description_and_agent(am, db, sent):
    agent = await _make_agent(db)
    with pytest.raises(ApplicationError):
        await am.create_application(name="  ", description="d", agent_id=agent["id"])
    with pytest.raises(ApplicationError):
        await am.create_application(name="X", description=" ", agent_id=agent["id"])
    with pytest.raises(ApplicationError) as e:
        await am.create_application(name="X", description="d", agent_id="nope")
    assert e.value.status_code == 404


@pytest.mark.asyncio
async def test_create_rejects_escaping_entrypoint(am, db, sent):
    agent = await _make_agent(db)
    with pytest.raises(ApplicationError):
        await am.create_application(
            name="X", description="d", agent_id=agent["id"], entrypoint="../x.html"
        )


@pytest.mark.asyncio
async def test_create_marks_failed_when_the_build_session_wont_start(
    models, am, db, mgr, monkeypatch
):
    async def boom(session_id, prompt, attachment_ids=None, *, source, **kwargs):
        assert source == "application"
        raise RuntimeError("harness exploded")
        return await models.start_message(mgr, session_id, prompt, source=source)

    monkeypatch.setattr(mgr, "start_message", boom)
    agent = await _make_agent(db)
    row = await am.create_application(
        name="Doomed", description="d", agent_id=agent["id"]
    )
    assert row["status"] == "failed"
    assert "harness exploded" in row["error"]


# ---------------------------------------------------------------------------
# Status derivation from the build session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_result_with_entrypoint_flips_to_ready(models, am, db, mgr, sent):
    row = await _create_app(am, db)
    open(os.path.join(row["app_dir"], "index.html"), "w").write("<h1>hi</h1>")

    await models.emit(mgr, {"type": "result", "session_id": row["session_id"]})

    updated = await db.get_application(row["id"])
    assert updated["status"] == "ready"
    assert updated["error"] is None
    assert updated["last_built_at"]


@pytest.mark.asyncio
async def test_result_without_entrypoint_flips_to_failed(models, am, db, mgr, sent):
    row = await _create_app(am, db)
    await models.emit(mgr, {"type": "result", "session_id": row["session_id"]})
    updated = await db.get_application(row["id"])
    assert updated["status"] == "failed"
    assert "index.html" in updated["error"]


@pytest.mark.asyncio
async def test_error_event_flips_to_failed_with_the_message(models, am, db, mgr, sent):
    row = await _create_app(am, db)
    await models.emit(mgr,
        {"type": "error", "session_id": row["session_id"], "message": "credit exhausted"}
    )
    updated = await db.get_application(row["id"])
    assert updated["status"] == "failed"
    assert updated["error"] == "credit exhausted"


async def test_build_stays_running_through_recovery_events(models, am, db, mgr, sent):
    row = await _create_app(am, db)
    for event in (
        {"type": "result", "is_error": True, "retrying": True},
        {"type": "error", "code": "turn_timeout_retry", "retrying": True},
    ):
        await models.emit(mgr, {**event, "session_id": row["session_id"]})
        updated = await db.get_application(row["id"])
        assert updated["status"] == "building"
        assert updated["error"] is None
    open(os.path.join(row["app_dir"], "index.html"), "w").write("<h1>recovered</h1>")
    await models.emit(mgr, {"type": "result", "session_id": row["session_id"], "is_error": False})
    assert (await db.get_application(row["id"]))["status"] == "ready"


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint_exists", [True, False])
async def test_build_commit_failure_exits_and_cold_read_keeps_original_result(tmp_path, entrypoint_exists):
    artifacts = tmp_path / "application-commit"
    artifacts.mkdir()
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-s",
        str(Path(__file__).resolve()), "-o", "python_functions=_application_commit_child",
        "--basetemp", str(artifacts / "child-tests"),
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
             "OCTOPUS_TEST_APP_ENTRYPOINT": str(int(entrypoint_exists))},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    output = asyncio.create_task(process.communicate())
    try:
        stdout, stderr = await asyncio.wait_for(asyncio.shield(output), 12)
        assert process.returncode == 70, (stdout + stderr).decode(errors="replace")
        proof = json.loads((artifacts / "proof.json").read_text())
        assert (artifacts / "actual-commit-applied").exists()
        assert not (artifacts / "result-published").exists()
        assert not (artifacts / "application-updated").exists()
        assert len((artifacts / "starts.jsonl").read_text().splitlines()) == 1
        db = Database(proof["database"])
        manager = SessionManager()
        applications = ApplicationManager()
        try:
            await db.initialize()
            deps.set_user_manager(UserManager(db))
            await manager.initialize(db)
            applications.bind(manager, db)
            row = await applications.get_application(proof["application_id"])
            # A lost terminal acknowledgment cannot publish a derived badge.
            # This existing projection has no automatic cold rebuild contract.
            assert row["status"] == "building" and row["last_built_at"] is None
            assert row["error"] is None
            assert applications.is_built(row) is entrypoint_exists
            if entrypoint_exists:
                assert Path(row["app_dir"], "index.html").read_text() == "<h1>Verified application</h1>"
            messages = await db.load_messages(proof["session_id"])
            assert [message["content"] for message in messages if message["role"] == "user"] == [proof["prompt"]]
            outcomes = [message["content"] for message in messages if message["type"] == "result"]
            assert outcomes == [proof["outcome"]]
            assert outcomes[0]["request_id"] == proof["request_id"] and outcomes[0]["status"] == "success"
            assert manager.dispatch_snapshot(proof["session_id"])["items"] == []
            assert not manager.session_is_executing(manager.get_session(proof["session_id"]))
            assert len((artifacts / "starts.jsonl").read_text().splitlines()) == 1
        finally:
            applications.shutdown()
            await db.close()
    finally:
        if process.returncode is None:
            process.kill()
        await asyncio.wait_for(output, 5)


def application_model(manager, monkeypatch, *, entrypoint_exists, starts_path=None):
    from server.harness import HarnessEvent

    entered, release = asyncio.Event(), asyncio.Event()
    starts = []

    class Run(FakeRunBase):
        async def start(self, prompt, working_dir, resume_id=None, credential=None):
            starts.append(prompt)
            if starts_path is not None:
                with starts_path.open("a") as stream:
                    stream.write(json.dumps(prompt) + "\n")
            if entrypoint_exists:
                Path(working_dir, "index.html").write_text("<h1>Verified application</h1>")
            entered.set()

        async def stream(self):
            await release.wait()
            yield HarnessEvent(type="result", is_error=False)

        async def stop(self):
            release.set()

        async def interrupt(self):
            await self.stop()

    monkeypatch.setattr(manager, "_make_run", lambda *args, **kwargs: Run())
    return entered, release, starts


async def test_entrypoint_patch_during_active_build_preserves_building(am, db, mgr, monkeypatch):
    from server.app_backends import backend_supervisor

    entered, release, starts = application_model(mgr, monkeypatch, entrypoint_exists=True)
    hooks = []

    async def rebuilt(*args, **kwargs):
        hooks.append(args)

    monkeypatch.setattr(backend_supervisor, "on_rebuild", rebuilt)
    row = await _create_app(am, db)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        updated = await am.update_application(row["id"], row["user_id"], entrypoint="index.html")
        assert updated["status"] == "building" and updated["last_built_at"] is None
        assert hooks == []
    finally:
        release.set()
        await mgr.await_session_execution(row["session_id"], timeout=2)


async def test_entrypoint_patch_during_creation_keeps_preparation_with_original_owner(am, db, mgr, monkeypatch):
    from server.app_backends import backend_supervisor

    entered, release, _ = application_model(mgr, monkeypatch, entrypoint_exists=True)
    patches, hooks = [], []

    async def rebuilt(*args, **kwargs):
        hooks.append(args)

    async def patch_created(event):
        if event.get("type") == "application_created":
            row = event["application"]
            Path(row["app_dir"], "index.html").write_text("<p>Prepared files</p>")
            patches.append(await am.update_application(row["id"], entrypoint="index.html"))
            assert hooks == []

    monkeypatch.setattr(backend_supervisor, "on_rebuild", rebuilt)
    mgr.on_broadcast("patch-app-preparation", patch_created)
    row = await _create_app(am, db)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert len(patches) == 1 and patches[0]["status"] == "building" and patches[0]["last_built_at"] is None
    finally:
        release.set()
        await mgr.await_session_execution(row["session_id"], timeout=2)
        mgr.remove_broadcast("patch-app-preparation")


async def test_idle_direct_chat_publishes_building_while_preparing_input(am, db, mgr, monkeypatch):
    from server import fork_helpers

    entered, release, starts = application_model(mgr, monkeypatch, entrypoint_exists=True)
    ready, building = asyncio.Event(), asyncio.Event()

    async def observe(event):
        if event.get("type") == "application_updated":
            (ready if event["application"]["status"] == "ready" else building).set()

    mgr.on_broadcast("preparing-app-notice", observe)
    row = await _create_app(am, db)
    await asyncio.wait_for(entered.wait(), 2)
    release.set()
    await asyncio.wait_for(ready.wait(), 2)
    await mgr.await_session_execution(row["session_id"], timeout=2)
    preparing, proceed = asyncio.Event(), asyncio.Event()
    anchor = fork_helpers.capture_git_anchor

    async def hold_prepare(*args, **kwargs):
        preparing.set()
        await proceed.wait()
        return await anchor(*args, **kwargs)

    building.clear()
    monkeypatch.setattr(fork_helpers, "capture_git_anchor", hold_prepare)
    try:
        assert (await mgr.start_message(row["session_id"], "Build after preparation", source="human")).accepted
        await asyncio.wait_for(preparing.wait(), 2)
        await asyncio.wait_for(building.wait(), 2)
        assert len(starts) == 1
        patched = await am.update_application(row["id"], entrypoint="index.html")
        assert patched["status"] == "building"
    finally:
        proceed.set()
        await mgr.interrupt(row["session_id"])
        mgr.remove_broadcast("preparing-app-notice")


@pytest.mark.parametrize("paused", [False, True])
async def test_direct_chat_acceptance_projects_building_before_memory_handoff(am, db, mgr, monkeypatch, paused):
    entered, release, _ = application_model(mgr, monkeypatch, entrypoint_exists=True)
    ready, building = asyncio.Event(), asyncio.Event()

    async def observe(event):
        if event.get("type") == "application_updated":
            status = event["application"]["status"]
            if status == "ready":
                ready.set()
            if status == "building":
                building.set()

    mgr.on_broadcast("atomic-app-input", observe)
    row = await _create_app(am, db)
    await asyncio.wait_for(entered.wait(), 2)
    release.set()
    await asyncio.wait_for(ready.wait(), 2)
    await mgr.await_session_execution(row["session_id"], timeout=2)
    previous = await am.get_application(row["id"])
    if paused:
        await mgr.interrupt(row["session_id"])
    building.clear()
    committed, handoff = asyncio.Event(), asyncio.Event()
    admit = db.admit_client_submission

    async def hold_after_commit(*args, **kwargs):
        result = await admit(*args, **kwargs)
        committed.set()
        await handoff.wait()
        return result

    monkeypatch.setattr(db, "admit_client_submission", hold_after_commit)
    submitted = asyncio.create_task(mgr.start_message(row["session_id"], "Another build", source="bg"))
    try:
        await asyncio.wait_for(committed.wait(), 2)
        current = await am.get_application(row["id"])
        assert current["status"] == "building" and current["error"] is None
        assert current["last_built_at"] == previous["last_built_at"]
        assert mgr.dispatch_snapshot(row["session_id"])["items"] == []
        patched = await am.update_application(row["id"], entrypoint="index.html")
        assert patched["status"] == "building" and patched["last_built_at"] == previous["last_built_at"]
        handoff.set()
        assert (await asyncio.wait_for(submitted, 2)).accepted
        await asyncio.wait_for(building.wait(), 2)
        if paused:
            assert mgr.dispatch_snapshot(row["session_id"])["paused"]
            assert (await am.get_application(row["id"]))["status"] == "building"
    finally:
        handoff.set()
        await asyncio.wait_for(submitted, 2)
        await mgr.interrupt(row["session_id"])
        mgr.remove_broadcast("atomic-app-input")


async def test_old_and_carried_completions_cannot_repeat_backend_retirement(am, db, mgr, monkeypatch):
    from dataclasses import replace

    from server.app_backends import backend_supervisor

    entered, release, _ = application_model(mgr, monkeypatch, entrypoint_exists=True)
    completed = asyncio.get_running_loop().create_future()
    projected = asyncio.Event()
    hooks = []

    async def capture(sid, completion):
        if not completed.done():
            completed.set_result(completion)

    async def rebuilt(*args, **kwargs):
        hooks.append(args)

    async def observe(event):
        if event.get("type") == "application_updated" and event["application"]["status"] == "ready":
            projected.set()

    mgr.on_request_complete("capture-app-completion", capture)
    mgr.on_broadcast("observe-app-completion", observe)
    monkeypatch.setattr(backend_supervisor, "on_rebuild", rebuilt)
    row = await _create_app(am, db)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        release.set()
        completion = await asyncio.wait_for(completed, 2)
        await asyncio.wait_for(projected.wait(), 2)
        assert len(hooks) == 1
        await am._on_completion(row["session_id"], replace(completion, input_request_id="carried-steer"))
        await am._on_completion(row["session_id"], completion)
        assert len(hooks) == 1
    finally:
        release.set()
        await mgr.await_session_execution(row["session_id"], timeout=2)
        mgr.remove_request_complete("capture-app-completion")
        mgr.remove_broadcast("observe-app-completion")


@pytest.mark.parametrize("input_first", [True, False])
async def test_completion_and_new_input_are_ordered_by_the_store(am, db, mgr, monkeypatch, input_first):
    from server.app_backends import backend_supervisor

    entered, release, _ = application_model(mgr, monkeypatch, entrypoint_exists=True)
    evaluating, proceed, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    project = db.project_application_terminal
    hooks, notices = [], []

    async def hold_projection(*args, **kwargs):
        if input_first:
            evaluating.set()
            await proceed.wait()
        result = await project(*args, **kwargs)
        if not input_first:
            evaluating.set()
            await proceed.wait()
        finished.set()
        return result

    async def rebuilt(*args, **kwargs):
        hooks.append(args)

    async def observe(event):
        if event.get("type") == "application_updated":
            notices.append(event["application"]["status"])

    mgr.on_broadcast("ordered-app-projection", observe)
    monkeypatch.setattr(db, "project_application_terminal", hold_projection)
    monkeypatch.setattr(backend_supervisor, "on_rebuild", rebuilt)
    row = await _create_app(am, db)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        release.set()
        await asyncio.wait_for(evaluating.wait(), 2)
        await mgr.await_session_execution(row["session_id"], timeout=2)
        await mgr.interrupt(row["session_id"])
        receipt = await mgr.start_message(row["session_id"], "Later queued build", source="bg")
        assert receipt.accepted
        assert (await am.get_application(row["id"]))["status"] == "building"
        proceed.set()
        await asyncio.wait_for(finished.wait(), 2)
        # Wait for this consumer's publication without executing the queue.
        async with asyncio.timeout(2):
            while any(task.get_name().startswith("request-completion-") and not task.done()
                      for task in mgr._owned_tasks):
                await asyncio.sleep(0)
        current = await am.get_application(row["id"])
        assert current["status"] == "building"
        assert bool(current["last_built_at"]) is (not input_first)
        assert len(hooks) == (0 if input_first else 1)
        assert notices[-1] == "building"
    finally:
        release.set()
        proceed.set()
        await mgr.interrupt(row["session_id"])
        mgr.remove_broadcast("ordered-app-projection")


async def test_waiting_application_subscriber_finishes_on_current_building(am, db, mgr, monkeypatch):
    entered, release, _ = application_model(mgr, monkeypatch, entrypoint_exists=True)
    ready = asyncio.Event()

    async def initial(event):
        if event.get("type") == "application_updated" and event["application"]["status"] == "ready":
            ready.set()

    mgr.on_broadcast("initial-app-ready", initial)
    row = await _create_app(am, db)
    await asyncio.wait_for(entered.wait(), 2)
    release.set()
    await asyncio.wait_for(ready.wait(), 2)
    await mgr.await_session_execution(row["session_id"], timeout=2)
    await mgr.interrupt(row["session_id"])
    subscriber_entered, subscriber_release, queued_notice = asyncio.Event(), asyncio.Event(), asyncio.Event()
    notices = []

    async def waiting_subscriber(event):
        if event.get("type") == "queued":
            queued_notice.set()
        if event.get("type") != "application_updated":
            return
        status = event["application"]["status"]
        if status == "ready":
            subscriber_entered.set()
            await subscriber_release.wait()
        notices.append(status)

    mgr.on_broadcast("waiting-app-subscriber", waiting_subscriber)
    patched = asyncio.create_task(am.update_application(row["id"], entrypoint="index.html"))
    try:
        await asyncio.wait_for(subscriber_entered.wait(), 2)
        assert (await mgr.start_message(row["session_id"], "Later queued build", source="bg")).accepted
        assert (await am.get_application(row["id"]))["status"] == "building"
        # Let the original session publisher deliver its admission notice.
        await asyncio.wait_for(queued_notice.wait(), 2)
        subscriber_release.set()
        await asyncio.wait_for(patched, 2)
        assert notices[-1] == "building"
    finally:
        subscriber_release.set()
        await asyncio.gather(patched, return_exceptions=True)
        mgr.remove_broadcast("initial-app-ready")
        mgr.remove_broadcast("waiting-app-subscriber")


async def _application_commit_child(tmp_path, tmp_path_factory, monkeypatch):
    artifacts = tmp_path_factory.getbasetemp().parent
    monkeypatch.setattr(settings, "applications_dir", str(artifacts / "applications"))
    database_path = tmp_path / "application.db"
    db = Database(str(database_path))
    await db.initialize()
    await create_account(db, extra_roots=[artifacts])
    manager = SessionManager()
    await manager.initialize(db)
    applications = ApplicationManager()
    applications.bind(manager, db)
    entered, release, starts = application_model(
        manager, monkeypatch, entrypoint_exists=os.environ["OCTOPUS_TEST_APP_ENTRYPOINT"] == "1",
        starts_path=artifacts / "starts.jsonl",
    )
    proof = {"database": str(database_path)}
    terminal = None
    execute, commit = db.conn.execute, db.conn.commit

    async def terminal_sql(sql, parameters=()):
        nonlocal terminal
        cursor = await execute(sql, parameters)
        if sql.startswith("INSERT INTO messages ") and parameters[3] == "result":
            terminal = json.loads(parameters[4])
        return cursor

    async def lost_driver_acknowledgment():
        if terminal is None:
            return await commit()
        await commit()
        proof["outcome"] = terminal
        (artifacts / "proof.json").write_text(json.dumps(proof))
        (artifacts / "actual-commit-applied").touch()
        raise OSError("actual application terminal commit acknowledgment lost")

    async def observe(event):
        if event.get("type") == "result":
            (artifacts / "result-published").touch()
        if event.get("type") == "application_updated" and event["application"]["status"] != "building":
            (artifacts / "application-updated").touch()

    monkeypatch.setattr(db.conn, "execute", terminal_sql)
    monkeypatch.setattr(db.conn, "commit", lost_driver_acknowledgment)
    manager.on_broadcast("application-commit-proof", observe)
    row = await _create_app(applications, db)
    proof.update(application_id=row["id"], session_id=row["session_id"])
    await asyncio.wait_for(entered.wait(), 3)
    inputs = [message for message in await db.load_messages(row["session_id"]) if message["role"] == "user"]
    assert len(inputs) == 1
    proof.update(prompt=inputs[0]["content"], request_id=inputs[0]["tool_input"]["request_context"]["request_id"])
    assert len(starts) == 1
    release.set()
    await asyncio.wait_for(asyncio.Event().wait(), 8)


@pytest.mark.parametrize("entrypoint_exists", [True, False])
async def test_committed_build_result_derives_status_once(am, db, mgr, monkeypatch, entrypoint_exists):
    entered, release, starts = application_model(mgr, monkeypatch, entrypoint_exists=entrypoint_exists)
    terminal = asyncio.get_running_loop().create_future()
    updates = []
    application_updated = asyncio.Event()

    async def observe(event):
        if event.get("type") == "result" and not terminal.done():
            terminal.set_result(event)
        if event.get("type") == "application_updated" and event["application"]["status"] != "building":
            updates.append(event)
            application_updated.set()

    mgr.on_broadcast("committed-build-proof", observe)
    row = await _create_app(am, db)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        release.set()
        event = await asyncio.wait_for(terminal, 2)
        await mgr.await_session_execution(row["session_id"], timeout=2)
        await asyncio.wait_for(application_updated.wait(), 2)
        restored = await am.get_application(row["id"])
        assert event["content"]["status"] == "success"
        assert await db.get_committed_terminal(row["session_id"], event["request_id"]) == event["content"]
        assert restored["status"] == ("ready" if entrypoint_exists else "failed")
        if entrypoint_exists:
            assert restored["error"] is None and restored["last_built_at"]
        else:
            assert "index.html" in restored["error"] and "unconfirmed" not in restored["error"].lower()
        assert len(starts) == 1 and len(updates) == 1
        assert await mgr.resume_dispatch(row["session_id"], resume_queue=False)
        assert len(starts) == 1 and len(updates) == 1
    finally:
        release.set()
        await mgr.await_session_execution(row["session_id"], timeout=2)
        mgr.remove_broadcast("committed-build-proof")


async def test_conflicting_build_outcome_reports_domain_error_without_retrying_storage(am, db, mgr, monkeypatch):
    from server.models import TurnOutcome

    entered, release, starts = application_model(mgr, monkeypatch, entrypoint_exists=True)
    conflict = asyncio.get_running_loop().create_future()
    updates = []
    application_updated = asyncio.Event()

    async def observe(event):
        if event.get("type") == "recording_failed" and not conflict.done():
            conflict.set_result(event)
        if event.get("type") == "application_updated" and event["application"]["status"] != "building":
            updates.append(event)
            application_updated.set()

    mgr.on_broadcast("conflicting-build-proof", observe)
    row = await _create_app(am, db)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        messages = await db.load_messages(row["session_id"])
        assert len(messages) == 1 and messages[0]["role"] == "user"
        request_id = messages[0]["tool_input"]["request_context"]["request_id"]
        committed = TurnOutcome(request_id=request_id, status="failed", message="Previously committed original outcome").model_dump()
        await db.record_terminal(
            request_id=request_id, session_id=row["session_id"], seq=1, role="system", type="result",
            content=committed, is_error=True, created_at="2026-10-21T00:00:00+00:00",
        )
        release.set()
        event = await asyncio.wait_for(conflict, 2)
        assert event["request_id"] == request_id and event["terminal"] is False
        assert event["message"] == "This request conflicts with an already recorded outcome."
        await mgr.await_session_execution(row["session_id"], timeout=2)
        await asyncio.wait_for(application_updated.wait(), 2)
        failed = await am.get_application(row["id"])
        assert failed["status"] == "failed" and failed["last_built_at"] is None
        assert failed["error"] == event["message"]
        assert "saving" not in failed["error"].lower() and "retry" not in failed["error"].lower()
        assert await db.get_committed_terminal(row["session_id"], request_id) == committed
        assert len(starts) == 1 and len(updates) == 1
        assert await mgr.resume_dispatch(row["session_id"], resume_queue=False)
        assert await db.get_committed_terminal(row["session_id"], request_id) == committed
        assert len(starts) == 1 and len(updates) == 1
    finally:
        release.set()
        await mgr.await_session_execution(row["session_id"], timeout=2)
        mgr.remove_broadcast("conflicting-build-proof")


@pytest.mark.asyncio
async def test_a_written_app_stays_ready_even_if_the_turn_errored(models, am, db, mgr, sent):
    """The filesystem is the source of truth: an agent that wrote a perfectly
    good app and *then* hit an error still produced a working application."""
    row = await _create_app(am, db)
    open(os.path.join(row["app_dir"], "index.html"), "w").write("<h1>hi</h1>")
    await models.emit(mgr,
        {"type": "error", "session_id": row["session_id"], "message": "late failure"}
    )
    assert (await db.get_application(row["id"]))["status"] == "ready"


@pytest.mark.asyncio
async def test_running_status_returns_the_app_to_building(models, am, db, mgr, sent, monkeypatch):
    row = await _create_app(am, db)
    open(os.path.join(row["app_dir"], "index.html"), "w").write("<h1>hi</h1>")
    await models.emit(mgr, {"type": "result", "session_id": row["session_id"]})
    assert (await db.get_application(row["id"]))["status"] == "ready"

    # A change typed straight into the build session's chat — no REST call.
    monkeypatch.setattr(mgr, "start_message", models.start_for(mgr))
    await mgr.start_message(row["session_id"], "Change this application through chat.")
    await models.emit(mgr,
        {"type": "status", "session_id": row["session_id"], "status": "running"}
    )
    assert (await db.get_application(row["id"]))["status"] == "building"

    await models.emit(mgr,
        {"type": "status", "session_id": row["session_id"], "status": "idle"}
    )
    assert (await db.get_application(row["id"]))["status"] == "building"


@pytest.mark.asyncio
async def test_events_for_other_sessions_are_ignored(models, am, db, mgr, sent):
    row = await _create_app(am, db)
    agent = await _make_agent(db, "Bystander")
    other = await mgr.create_session(agent_id=agent["id"], name="unrelated")
    open(os.path.join(row["app_dir"], "index.html"), "w").write("x")
    await models.emit(mgr, {"type": "result", "session_id": other.id})
    assert (await db.get_application(row["id"]))["status"] == "building"


@pytest.mark.asyncio
async def test_broadcasts_carry_the_row(models, am, db, mgr, sent):
    seen: list[dict] = []

    mgr.on_broadcast("test-sink", lambda m: seen.append(m))
    row = await _create_app(am, db)
    created = [m for m in seen if m["type"] == "application_created"]
    assert created and created[0]["application"]["id"] == row["id"]

    open(os.path.join(row["app_dir"], "index.html"), "w").write("x")
    await models.emit(mgr, {"type": "result", "session_id": row["session_id"]})
    updated = [m for m in seen if m["type"] == "application_updated"]
    assert updated and updated[-1]["application"]["status"] == "ready"
    mgr.remove_broadcast("test-sink")


# ---------------------------------------------------------------------------
# Follow-up builds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_build_reuses_the_session(am, db, mgr, sent):
    row = await _create_app(am, db)
    sent.clear()

    updated = await am.request_build(row["id"], "Add a dark mode toggle")
    assert updated["status"] == "building"
    assert len(sent) == 1
    sid, prompt = sent[0]
    assert sid == row["session_id"]
    assert "Add a dark mode toggle" in prompt
    assert row["app_dir"] in prompt
    # A follow-up doesn't re-send the whole brief — the session has it.
    assert "STATIC FILES" not in prompt


@pytest.mark.asyncio
async def test_request_build_clears_a_previous_failure(models, am, db, mgr, sent):
    row = await _create_app(am, db)
    await models.emit(mgr, {"type": "result", "session_id": row["session_id"]})
    assert (await db.get_application(row["id"]))["status"] == "failed"

    await am.request_build(row["id"], "try again")
    after = await db.get_application(row["id"])
    assert after["status"] == "building"
    assert after["error"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_status,previous_error", [("ready", None), ("failed", "Previous build failed")])
async def test_closed_build_request_preserves_previous_application_state(am, db, mgr, sent, monkeypatch, previous_status, previous_error):
    row = await _create_app(am, db)
    await db.update_application(row["id"], status=previous_status, error=previous_error)
    monkeypatch.setattr(mgr, "start_message", SessionManager.start_message.__get__(mgr))
    mgr._admission_closed = True
    with pytest.raises(ApplicationError) as exc:
        await am.request_build(row["id"], "Add a dark mode toggle")
    assert exc.value.status_code == 409
    assert "closed" in exc.value.message.lower()
    stored = await db.get_application(row["id"])
    assert (stored["status"], stored["error"]) == (previous_status, previous_error)


@pytest.mark.asyncio
async def test_request_build_opens_a_new_session_when_the_old_one_is_gone(
    am, db, mgr, sent
):
    row = await _create_app(am, db)
    await mgr.delete_session(row["session_id"])
    sent.clear()

    updated = await am.request_build(row["id"], "Add a footer")
    assert updated["session_id"] != row["session_id"]
    assert mgr.get_session(updated["session_id"]) is not None
    # No transcript to inherit → the agent gets the full brief again.
    assert "STATIC FILES" in sent[0][1]
    assert "Add a footer" in sent[0][1]


@pytest.mark.asyncio
async def test_request_build_rejects_an_empty_prompt(am, db, sent):
    row = await _create_app(am, db)
    with pytest.raises(ApplicationError):
        await am.request_build(row["id"], "   ")


@pytest.mark.asyncio
async def test_request_build_on_a_missing_application_is_404(am, db, sent):
    with pytest.raises(ApplicationError) as e:
        await am.request_build("nope", "hi")
    assert e.value.status_code == 404


# ---------------------------------------------------------------------------
# Update / delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rename_keeps_the_directory(am, db, sent):
    row = await _create_app(am, db, name="Todo App")
    renamed = await am.update_application(row["id"], name="Task Board")
    assert renamed["name"] == "Task Board"
    assert renamed["app_dir"] == row["app_dir"]


@pytest.mark.asyncio
async def test_rename_to_an_existing_name_is_409(am, db, sent):
    agent = await _make_agent(db)
    a = await am.create_application(name="A", description="d", agent_id=agent["id"])
    await am.create_application(name="B", description="d", agent_id=agent["id"])
    with pytest.raises(ApplicationError) as e:
        await am.update_application(a["id"], name="b")
    assert e.value.status_code == 409


@pytest.mark.asyncio
async def test_entrypoint_change_re_derives_status(models, am, db, mgr, sent):
    row = await _create_app(am, db)
    open(os.path.join(row["app_dir"], "main.html"), "w").write("<h1>hi</h1>")
    await models.emit(mgr, {"type": "result", "session_id": row["session_id"]})
    assert (await db.get_application(row["id"]))["status"] == "failed"

    updated = await am.update_application(row["id"], entrypoint="main.html")
    assert updated["entrypoint"] == "main.html"
    assert updated["status"] == "ready"


@pytest.mark.asyncio
async def test_archive_hides_it_but_keeps_row_and_files(am, db, sent):
    row = await _create_app(am, db)
    open(os.path.join(row["app_dir"], "index.html"), "w").write("<h1>hi</h1>")

    archived = await am.set_archived(row["id"], True)
    assert archived["archived"] is True
    assert [a["id"] for a in await am.list_applications()] == []
    assert [a["id"] for a in await am.list_applications(only_archived=True)] == [
        row["id"]
    ]
    # The whole point of archiving over deleting: the app is still on disk.
    assert os.path.isfile(os.path.join(row["app_dir"], "index.html"))

    restored = await am.set_archived(row["id"], False)
    assert restored["archived"] is False
    assert [a["id"] for a in await am.list_applications()] == [row["id"]]


@pytest.mark.asyncio
async def test_archiving_twice_is_a_no_op(am, db, sent):
    row = await _create_app(am, db)
    await am.set_archived(row["id"], True)
    again = await am.set_archived(row["id"], True)
    assert again["archived"] is True


@pytest.mark.asyncio
async def test_restore_refuses_a_name_taken_since(am, db, sent):
    agent = await _make_agent(db)
    row = await am.create_application(
        name="Tracker", description="d", agent_id=agent["id"]
    )
    await am.set_archived(row["id"], True)
    # The unique index only covers live rows, so the name is free again.
    await am.create_application(
        name="Tracker", description="d2", agent_id=agent["id"]
    )
    with pytest.raises(ApplicationError) as e:
        await am.set_archived(row["id"], False)
    assert e.value.status_code == 409


@pytest.mark.asyncio
async def test_delete_removes_row_and_directory(am, db, sent):
    row = await _create_app(am, db)
    await am.delete_application(row["id"])
    assert await db.get_application(row["id"]) is None
    assert not os.path.exists(row["app_dir"])


@pytest.mark.asyncio
async def test_delete_can_keep_the_files(am, db, sent):
    row = await _create_app(am, db)
    await am.delete_application(row["id"], keep_files=True)
    assert await db.get_application(row["id"]) is None
    assert os.path.isdir(row["app_dir"])


async def test_same_name_creation_does_not_inherit_kept_data_or_runtime(am, db, sent):
    import shutil

    original = await _create_app(am, db)
    for suffix in (".data", ".runtime"):
        directory = Path(original["app_dir"] + suffix)
        directory.mkdir()
        (directory / "original.txt").write_text("retained" + suffix)
    await am.delete_application(original["id"], keep_files=True)
    shutil.rmtree(original["app_dir"])
    replacement = await _create_app(am, db, agent=await db.get_agent(original["agent_id"]))
    assert replacement["app_dir"] != original["app_dir"]
    for suffix in (".data", ".runtime"):
        assert not Path(replacement["app_dir"] + suffix).exists()
    await am.delete_application(replacement["id"])
    for suffix in (".data", ".runtime"):
        assert Path(original["app_dir"] + suffix, "original.txt").read_text() == "retained" + suffix


async def test_delete_retirement_failure_keeps_files_record_and_closed_admission(
    am, db, mgr, sent, monkeypatch,
):
    from server.app_backends import backend_supervisor

    row = await _create_app(am, db)
    events = []
    mgr.on_broadcast("delete-test", lambda message: _capture(events, message))

    async def fail_stop(_):
        raise RuntimeError("reader did not exit")

    monkeypatch.setattr(backend_supervisor, "stop", fail_stop)
    with pytest.raises(ApplicationError, match="reader did not exit"):
        await am.delete_application(row["id"])
    retained = await am.get_application(row["id"])
    assert retained["deletion"]["phase"] == "retiring"
    assert "reader did not exit" in retained["deletion"]["error"]
    assert Path(row["app_dir"]).is_dir()
    assert not any(event["type"] == "application_deleted" for event in events)
    with pytest.raises(ApplicationError, match="delet"):
        await am.request_build(row["id"], "keep writing")


async def test_delete_closes_other_sessions_when_one_builder_is_forking(am, db, mgr, sent, models):
    from server.sessions.owner_runtime import ReserveFork

    row = await _create_app(am, db)
    await models.emit(mgr, {"type": "result", "session_id": row["session_id"]})
    builder = mgr.get_session(row["session_id"])
    conversation = await mgr.create_session(agent_id=row["agent_id"], origin="app", app_id=row["id"])
    await mgr.interrupt(conversation.id)
    pending = await SessionManager.start_message(mgr, conversation.id, "queued app input", source="bg")
    owner = mgr._session_owner(builder)
    await owner.call_runtime(ReserveFork(True))
    try:
        with pytest.raises(ApplicationError, match="fork or archive"):
            await am.delete_application(row["id"])
        assert (await asyncio.wait_for(pending.wait(), .2)).state == "withdrawn"
        assert (await am.get_application(row["id"]))["deletion"]["phase"] == "retiring"
        assert Path(row["app_dir"]).is_dir()
    finally:
        await owner.call_runtime(ReserveFork(False))


async def _capture(events, message):
    events.append(message)


async def test_delete_waiter_cancellation_and_duplicate_requests_share_cleanup(am, db, mgr, sent, monkeypatch):
    from server.app_backends import backend_supervisor

    row = await _create_app(am, db)
    stopping, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def stop(_):
        nonlocal calls
        calls += 1
        stopping.set()
        await release.wait()

    monkeypatch.setattr(backend_supervisor, "stop", stop)
    first = asyncio.create_task(am.delete_application(row["id"]))
    await asyncio.wait_for(stopping.wait(), 2)
    second = asyncio.create_task(am.delete_application(row["id"]))
    with pytest.raises(ApplicationError, match="keep_files"):
        await am.delete_application(row["id"], keep_files=True)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert Path(row["app_dir"]).exists()
    assert (await am.get_application(row["id"]))["deletion"] is not None
    release.set()
    await asyncio.wait_for(second, 2)
    assert calls == 1
    assert await db.get_application(row["id"]) is None
    assert not Path(row["app_dir"]).exists()
    # Success releases the temporary admission block on retained history.
    session = mgr.get_session(row["session_id"])
    from server.sessions.base import QueuedPrompt
    from server.sessions.owner_runtime import Submit

    receipt = await mgr._session_owner(session).call_runtime(Submit(QueuedPrompt("new instruction", [])))
    assert receipt.accepted
    await mgr.interrupt(session.id)


async def test_partial_file_failure_retries_only_files_and_restart_keeps_admission_closed(
    am, db, mgr, sent, monkeypatch,
):
    from server import applications
    from server.app_backends import backend_supervisor
    from server.db.applications import ApplicationDeleting
    from server.sessions.base import QueuedPrompt
    from server.sessions.owner_runtime import Submit

    row = await _create_app(am, db)
    data = Path(row["app_dir"] + ".data")
    data.mkdir()
    (data / "keep.txt").write_text("still here")
    real_remove = applications.shutil.rmtree
    stops = 0

    async def stop(_):
        nonlocal stops
        stops += 1

    def remove(path):
        if path == str(data):
            raise PermissionError("data directory locked")
        return real_remove(path)

    monkeypatch.setattr(backend_supervisor, "stop", stop)
    monkeypatch.setattr(applications.shutil, "rmtree", remove)
    with pytest.raises(ApplicationError, match="locked"):
        await am.delete_application(row["id"])
    assert not Path(row["app_dir"]).exists()
    assert (data / "keep.txt").read_text() == "still here"
    assert (await am.get_application(row["id"]))["deletion"]["phase"] == "files"
    am.shutdown()
    await mgr.close_session_owners()
    restarted = SessionManager()
    await restarted.initialize(db, previous_writers_retired=True)
    restored = ApplicationManager()
    restored.bind(restarted, db)
    try:
        await restored.refresh_icons()
        assert data.exists()  # opening never resumes file deletion
        session = restarted.get_session(row["session_id"])
        receipt = await restarted._session_owner(session).call_runtime(Submit(QueuedPrompt("write again", [])))
        assert not receipt.accepted
        with pytest.raises(ApplicationDeleting):
            await db.require_application_session_writable(session.id)
        monkeypatch.setattr(applications.shutil, "rmtree", real_remove)
        await restored.delete_application(row["id"])
        assert stops == 1
        assert not data.exists()
        assert await db.get_application(row["id"]) is None
    finally:
        restored.shutdown()
        await restarted.close_session_owners()


async def test_cleanup_preserves_a_data_access_link_inside_code_until_data_has_been_removed(am, db, mgr, sent, models, monkeypatch):
    row = await _create_app(am, db)
    await models.emit(mgr, {"type": "result", "session_id": row["session_id"]})
    code = Path(row["app_dir"])
    data = code.parent / "actual-data"
    data.mkdir()
    (data / "original.txt").write_text("durable data")
    middle = code / "data-bridge"
    middle.symlink_to(data, target_is_directory=True)
    stored_data = Path(str(code) + ".data")
    stored_data.symlink_to(middle, target_is_directory=True)
    remove = shutil.rmtree

    def fail_data(path, *args, **kwargs):
        if Path(path) == data:
            raise PermissionError("original data is locked")
        return remove(path, *args, **kwargs)

    with monkeypatch.context() as faults:
        faults.setattr(shutil, "rmtree", fail_data)
        with pytest.raises(ApplicationError, match="locked"):
            await am.delete_application(row["id"])
    assert (stored_data / "original.txt").read_text() == "durable data"
    assert middle.is_symlink() and code.is_dir()
    await am.delete_application(row["id"])
    assert await db.get_application(row["id"]) is None
    assert not code.exists() and not data.exists() and not stored_data.is_symlink()


@pytest.mark.parametrize("relative", [False, True])
async def test_application_create_and_delete_resolve_parent_components_after_symlinks(am, db, mgr, sent, models, tmp_path, monkeypatch, relative):
    entry, actual = tmp_path / "entry", tmp_path / "actual"
    entry.mkdir()
    nested = actual / "nested"
    nested.mkdir(parents=True)
    (entry / "link").symlink_to(nested, target_is_directory=True)
    if relative:
        monkeypatch.chdir(tmp_path)
    users = os.path.join("entry" if relative else str(entry), "link", "..", "users")
    monkeypatch.setattr(settings, "users_root", users)
    row = await _create_app(am, db)
    await models.emit(mgr, {"type": "result", "session_id": row["session_id"]})
    expected = actual / "users" / row["user_id"] / "applications" / Path(row["app_dir"]).name
    assert Path(row["app_dir"]).resolve() == expected
    for suffix in ("", ".data", ".runtime"):
        directory = Path(str(expected) + suffix)
        directory.mkdir(exist_ok=True)
        (directory / "original.txt").write_text("resource" + suffix)
    await am.delete_application(row["id"])
    assert await db.get_application(row["id"]) is None
    for suffix in ("", ".data", ".runtime"):
        assert not Path(str(expected) + suffix).exists()
    assert (entry / "link").is_symlink()


@pytest.mark.parametrize("names", [("a", "b"), ("c", "d"), ("outer", "inner")])
@pytest.mark.parametrize("failed_link", ["inner", "outer"])
async def test_cleanup_retries_an_inner_link_failure_without_losing_its_stored_entry(am, db, mgr, sent, models, monkeypatch, names, failed_link):
    row = await _create_app(am, db)
    await models.emit(mgr, {"type": "result", "session_id": row["session_id"]})
    code = Path(row["app_dir"])
    outer, inner = (code.parent / name for name in names)
    inner.symlink_to(code, target_is_directory=True)
    outer.symlink_to(inner, target_is_directory=True)
    async with db.transaction(write=True) as tx:
        await tx.execute("UPDATE applications SET app_dir=? WHERE id=?", (str(outer), row["id"]))
    unlink = Path.unlink

    def fail_inner(path, *args, **kwargs):
        if path == (inner if failed_link == "inner" else outer):
            raise PermissionError("inner access link is locked")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as faults:
        faults.setattr(Path, "unlink", fail_inner)
        with pytest.raises(ApplicationError, match="locked"):
            await am.delete_application(row["id"])
    assert outer.is_symlink()
    assert inner.is_symlink() is (failed_link == "inner")
    assert (await db.get_application(row["id"]))["deletion"]["phase"] == "files"
    await am.delete_application(row["id"])
    assert await db.get_application(row["id"]) is None
    assert not code.exists() and not outer.is_symlink() and not inner.is_symlink()


@pytest.mark.parametrize("restart", [False, True])
async def test_partial_deletion_does_not_reuse_or_remove_replacement_resources(
    am, db, mgr, sent, models, monkeypatch, restart,
):
    from server import applications

    original = await _create_app(am, db)
    await am.set_archived(original["id"], True)
    original_data = Path(original["app_dir"] + ".data")
    original_data.mkdir()
    (original_data / "original.txt").write_text("original data")
    remove = applications.shutil.rmtree

    def refuse_data(path):
        if path == str(original_data):
            raise PermissionError("original data is locked")
        return remove(path)

    monkeypatch.setattr(applications.shutil, "rmtree", refuse_data)
    with pytest.raises(ApplicationError, match="locked"):
        await am.delete_application(original["id"])
    assert not Path(original["app_dir"]).exists()

    current = am
    restarted = None
    if restart:
        am.shutdown()
        await mgr.close_session_owners()
        restarted = SessionManager()
        await restarted.initialize(db, previous_writers_retired=True)
        current = ApplicationManager()
        current.bind(restarted, db)
        async def start(session_id, prompt, **kwargs):
            return await models.start_message(restarted, session_id, prompt, **kwargs)

        monkeypatch.setattr(restarted, "start_message", start)
    try:
        replacement = await _create_app(current, db, agent=await db.get_agent(original["agent_id"]))
        assert replacement["app_dir"] != original["app_dir"]
        for suffix in ("", ".data", ".runtime"):
            directory = Path(replacement["app_dir"] + suffix)
            directory.mkdir(exist_ok=True)
            (directory / "replacement.txt").write_text("replacement" + suffix)
        monkeypatch.setattr(applications.shutil, "rmtree", remove)
        await current.delete_application(original["id"])
        with pytest.raises(ApplicationError, match="not found"):
            await current.get_application(original["id"])
        for suffix in ("", ".data", ".runtime"):
            assert Path(replacement["app_dir"] + suffix, "replacement.txt").read_text() == "replacement" + suffix
    finally:
        if restarted is not None:
            current.shutdown()
            await restarted.close_session_owners()


@pytest.mark.parametrize("relation", ["shared", "data", "runtime", "nested", "alias"])
@pytest.mark.parametrize("archived", [False, True])
@pytest.mark.parametrize("keep_files", [False, True])
async def test_delete_refuses_overlapping_retained_application_resources(
    am, db, sent, relation, archived, keep_files,
):
    row = await _create_app(am, db)
    code = Path(row["app_dir"])
    (code / "original.txt").write_text("original code")
    if relation == "shared":
        other_code = code
    elif relation in {"data", "runtime"}:
        other_code = Path(str(code) + "." + relation)
        other_code.mkdir()
    elif relation == "nested":
        other_code = code / "nested"
        other_code.mkdir()
    else:
        other_code = code.parent / "legacy-alias"
        other_code.symlink_to(code, target_is_directory=True)
    (other_code / "other.txt").write_text("other code")
    await db.save_application(app_id="legacy-other", name="Legacy other", app_dir=str(other_code),
                              created_at="time", updated_at="time", user_id=row["user_id"])
    if archived:
        await am.set_archived("legacy-other", True)

    with pytest.raises(ApplicationError, match="legacy-other") as refused:
        await am.delete_application(row["id"], keep_files=keep_files)
    assert row["id"] in str(refused.value)
    assert str(code) in str(refused.value)
    retained = await am.get_application(row["id"])
    assert retained["deletion"]["phase"] == "retiring"
    assert (code / "original.txt").read_text() == "original code"
    assert (other_code / "other.txt").read_text() == "other code"
    assert (await am.get_application("legacy-other"))["app_dir"] == str(other_code)


@pytest.mark.parametrize("phase", ["retiring", "files"])
@pytest.mark.parametrize("archived", [False, True])
@pytest.mark.parametrize("chained", [False, True])
async def test_parent_alias_ownership_blocks_deleting_another_apps_required_link(
    am, db, sent, phase, archived, chained,
):
    row = await _create_app(am, db)
    code = Path(row["app_dir"])
    target = code.parent / "other-resources"
    target.mkdir()
    alias = code / "alias"
    alias.symlink_to(target, target_is_directory=True)
    stored_parent = alias
    if chained:
        stored_parent = code.parent / "outer-alias"
        stored_parent.symlink_to(alias, target_is_directory=True)
    other_code = stored_parent / "other-app"
    for suffix in ("", ".data", ".runtime"):
        resource = Path(str(other_code) + suffix)
        resource.mkdir()
        (resource / "original.txt").write_text("other app" + suffix)
    await db.save_application(app_id="parent-alias-owner", name="Other", app_dir=str(other_code),
                              created_at="time", updated_at="time", user_id=row["user_id"])
    if archived:
        await am.set_archived("parent-alias-owner", True)
    await db.set_application_deletion(row["id"], {"phase": phase, "keep_files": False, "error": None})

    with pytest.raises(ApplicationError, match="parent-alias-owner") as refused:
        await am.delete_application(row["id"])
    assert row["id"] in str(refused.value) and str(alias) in str(refused.value)
    assert (await am.get_application(row["id"]))["deletion"]["phase"] == phase
    assert (await am.get_application("parent-alias-owner"))["app_dir"] == str(other_code)
    for suffix in ("", ".data", ".runtime"):
        assert Path(str(other_code) + suffix, "original.txt").read_text() == "other app" + suffix


async def test_parent_alias_ownership_allows_independent_apps_sharing_a_parent_link(am, db, sent):
    row = await _create_app(am, db)
    code = Path(row["app_dir"])
    alias = code.parent / "shared-parent"
    alias.symlink_to(code.parent, target_is_directory=True)
    stored_code = str(alias / code.name)
    async with db.transaction(write=True) as tx:
        await tx.execute("UPDATE applications SET app_dir=? WHERE id=?", (stored_code, row["id"]))
    other_code = alias / "other-app"
    other_code.mkdir()
    (other_code / "original.txt").write_text("other app")
    await db.save_application(app_id="parent-alias-owner", name="Other", app_dir=str(other_code),
                              created_at="time", updated_at="time", user_id=row["user_id"])

    assert (await am.get_application(row["id"]))["app_dir"] == stored_code
    assert (await am.get_application("parent-alias-owner"))["app_dir"] == str(other_code)
    await am.delete_application(row["id"])
    assert (other_code / "original.txt").read_text() == "other app"
    assert alias.is_symlink()
    assert not code.exists()


@pytest.mark.parametrize("occupancy", ["archived-row", "deleting-row", "data", "runtime", "dangling-link"])
async def test_creation_refuses_resources_retained_without_a_code_directory(
    am, db, sent, monkeypatch, occupancy,
):
    from types import SimpleNamespace

    from server import applications
    from server.workspace import paths_for

    agent = await _make_agent(db)
    candidate = paths_for(agent["user_id"]).applications / "notes--0123456789ab"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    if occupancy.endswith("row"):
        await db.save_application(app_id="legacy-owner", name="Previous notes", app_dir=str(candidate),
                                  created_at="time", updated_at="time", user_id=agent["user_id"])
        if occupancy == "archived-row":
            await am.set_archived("legacy-owner", True)
        else:
            await db.set_application_deletion("legacy-owner", {"phase": "files", "keep_files": False, "error": "locked"})
    elif occupancy == "dangling-link":
        candidate.symlink_to(candidate.parent / "missing")
    else:
        Path(str(candidate) + "." + occupancy).mkdir()
    monkeypatch.setattr(applications.uuid, "uuid4", lambda: SimpleNamespace(hex="0123456789abcdef"))

    with pytest.raises(ApplicationError, match="resource"):
        await am.create_application(name="Notes", description="Independent notes", agent_id=agent["id"])
    assert not candidate.is_dir()
    assert sent == []
    assert len(await am.list_applications(include_archived=True, user_id=agent["user_id"])) == int(occupancy.endswith("row"))


async def test_delete_joins_background_process_handle_that_arrives_late(am, db, mgr, sent, monkeypatch):
    from server import bg_tasks

    row = await _create_app(am, db)
    background = bg_tasks.BgTaskManager()

    async def discard(_):
        from server.sessions.submission import ReturnRefused

        return ReturnRefused("source_withdrawn")

    background.bind(db, discard, discard)
    monkeypatch.setattr(bg_tasks, "bg_task_manager", background)
    real_spawn = asyncio.create_subprocess_exec
    spawned, release = asyncio.Event(), asyncio.Event()
    processes = []

    async def delayed_spawn(*args, **kwargs):
        proc = await real_spawn(*args, **kwargs)
        processes.append(proc)
        spawned.set()
        await release.wait()
        return proc

    monkeypatch.setattr(bg_tasks.asyncio, "create_subprocess_exec", delayed_spawn)
    startup = asyncio.create_task(background.start_task(
        session_id=row["session_id"], command="sleep 120", working_dir=row["app_dir"],
    ))
    deletion = None
    try:
        await asyncio.wait_for(spawned.wait(), 2)
        deletion = asyncio.create_task(am.delete_application(row["id"]))
        # Observe the persisted boundary, not an assumed scheduling delay.
        async with asyncio.timeout(2):
            while (await am.get_application(row["id"]))["deletion"] is None:
                await asyncio.sleep(0)
        assert Path(row["app_dir"]).exists()
        assert not deletion.done()
        release.set()
        await startup
        await asyncio.wait_for(deletion, 5)
        assert processes[0].returncode is not None
        assert not Path(row["app_dir"]).exists()
    finally:
        release.set()
        await asyncio.gather(startup, *([deletion] if deletion else []), return_exceptions=True)
        await background.shutdown()


@pytest.mark.parametrize("phase", ["retiring", "files"])
async def test_account_snapshot_keeps_unfinished_deletion_without_execution_authority(
    am, db, sent, tmp_path, phase,
):
    import sqlite3
    from contextlib import closing

    from server.crypto import master_key
    from server.db.portability import snapshot_account
    from server.db.schema import CURRENT_VERSION
    from server.storage_verification import verify_main

    row = await _create_app(am, db)
    state = {"phase": phase, "keep_files": True, "error": "original cleanup failure"}
    await db.set_application_deletion(row["id"], state)
    target = tmp_path / "export.sqlite3"
    with closing(sqlite3.connect(":memory:")) as source:
        source.row_factory = sqlite3.Row
        await db.conn.backup(source)
        snapshot_account(source, target, row["user_id"], key=master_key())
    with closing(sqlite3.connect(target)) as copy:
        assert verify_main(copy) == CURRENT_VERSION
        assert copy.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        saved = copy.execute("SELECT session_id,deletion FROM applications WHERE id=?", (row["id"],)).fetchone()
        assert saved[0] is None
        assert json.loads(saved[1]) == state
    restored = Database(str(target))
    await restored.initialize()
    try:
        assert (await restored.get_application(row["id"]))["deletion"] == state
    finally:
        await restored.close()


@pytest.mark.asyncio
async def test_delete_refuses_to_touch_a_dir_outside_the_root(
    am, db, tmp_path, sent
):
    row = await _create_app(am, db)
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keepme.txt").write_text("hi")
    async with db.transaction(write=True) as tx:
        await tx.execute("UPDATE applications SET app_dir=? WHERE id=?", (str(outside), row["id"]))
    assert (await db.get_application(row["id"]))["app_dir"] == str(outside)

    with pytest.raises(ApplicationError, match="outside"):
        await am.delete_application(row["id"])
    assert (await db.get_application(row["id"]))["deletion"]["phase"] == "retiring"
    assert (outside / "keepme.txt").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("destination", ["other-account", "installation"])
async def test_owned_application_cannot_delete_another_managed_roots_files(
    am, db, sent, apps_root, destination,
):
    from server.workspace import paths_for

    row = await _create_app(am, db)
    if destination == "other-account":
        other, _ = await create_account(db, username="another-owner")
        root = paths_for(other["id"]).applications
    else:
        root = apps_root
    outside = root / "precious"
    outside.mkdir(parents=True)
    (outside / "keep.txt").write_text("other resources")
    async with db.transaction(write=True) as tx:
        await tx.execute("UPDATE applications SET app_dir=? WHERE id=?", (str(outside), row["id"]))
    assert (await db.get_application(row["id"]))["app_dir"] == str(outside)

    with pytest.raises(ApplicationError, match="outside"):
        await am.delete_application(row["id"], user_id=row["user_id"])
    assert (await db.get_application(row["id"]))["deletion"]["phase"] == "retiring"
    assert (outside / "keep.txt").read_text() == "other resources"


@pytest.mark.asyncio
async def test_delete_keeps_the_build_session(am, db, mgr, sent):
    row = await _create_app(am, db)
    await am.delete_application(row["id"])
    assert mgr.get_session(row["session_id"]) is not None


async def test_reorder_reports_its_committed_rows_when_membership_changes_after_commit(am, db, mgr, sent, monkeypatch):
    first = await _create_app(am, db, name="First")
    second = await _create_app(am, db, name="Second")
    reorder = db.reorder_application_pins
    events = []
    mgr.on_broadcast("order-test", lambda message: _capture(events, message))

    async def interleaved(ids, **kwargs):
        result = await reorder(ids, **kwargs)
        await db.save_application(app_id="late", name="Late", app_dir=first["app_dir"] + "-late",
                                  created_at="later", updated_at="later", user_id=first["user_id"])
        return result

    monkeypatch.setattr(db, "reorder_application_pins", interleaved)
    result = await am.reorder_pins([second["id"], first["id"]], user_id=first["user_id"])
    assert {row["id"] for row in result} == {first["id"], second["id"]}
    assert {event["application_id"] for event in events} == {first["id"], second["id"]}
    assert (await db.get_application("late"))["pinned"]


# ---------------------------------------------------------------------------
# REST routes
# ---------------------------------------------------------------------------


@pytest.fixture
async def client(models, apps_root, monkeypatch, db):
    """The real app wired to a fresh DB + the singleton managers, with
    `start_message` stubbed so no harness ever spawns."""
    owner = await db.get_system_agent()
    bearer = await deps.get_user_manager().issue_token(owner["user_id"])
    session_manager.sessions.clear()
    session_manager._admission_closed = False
    await session_manager.initialize(db)

    agents_mod.set_manager(AgentManager(db))
    singleton_application_manager.bind(session_mgr=session_manager, db=db)
    app_agent_manager.bind(session_mgr=session_manager, db=db, app_mgr=singleton_application_manager)
    applications_mod.set_manager(singleton_application_manager)

    calls: list[tuple[str, str]] = []

    async def fake_start_message(session_id, prompt, attachment_ids=None, *, source, **kwargs):
        assert source == "application"
        calls.append((session_id, prompt))
        return await models.start_message(session_manager, session_id, prompt, source=source, **kwargs)

    monkeypatch.setattr(session_manager, "start_message", fake_start_message)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {bearer}"}) as c:
        c.sent = calls  # type: ignore[attr-defined]
        yield c

    app_agent_manager.shutdown()
    singleton_application_manager.shutdown()


async def _api_create(client, name="Todo App", **extra) -> dict:
    agents = (await client.get("/api/agents", headers=client.headers)).json()
    body = {
        "name": name,
        "description": "A todo list",
        "agent_id": extra.pop("agent_id", agents[0]["id"]),
        **extra,
    }
    resp = await client.post("/api/applications", json=body, headers=client.headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_api_auth_required(client):
    assert (await client.get("/api/applications", headers={"Authorization": ""})).status_code in (401, 403)


@pytest.mark.asyncio
async def test_api_create_list_get(client):
    created = await _api_create(client)
    assert created["status"] == "building"
    assert created["session_id"]

    listed = (await client.get("/api/applications", headers=client.headers)).json()
    assert [a["id"] for a in listed] == [created["id"]]

    one = await client.get(f"/api/applications/{created['id']}", headers=client.headers)
    assert one.status_code == 200
    assert one.json()["name"] == "Todo App"

    assert (
        await client.get("/api/applications/missing", headers=client.headers)
    ).status_code == 404


async def test_api_archived_builder_is_refused_before_application_effects(client, db):
    from server.workspace import paths_for

    owner = await db.get_system_agent()
    builder = await AgentManager(db).create_agent(name="Closed builder", user_id=owner["user_id"])
    await session_manager.archive_agent(builder["id"])
    directory = paths_for(owner["user_id"]).applications
    before = sorted(str(path) for path in directory.rglob("*")) if directory.exists() else []
    response = await client.post("/api/applications", json={"name": "Refused app", "description": "Must not build",
        "agent_id": builder["id"]})
    assert response.status_code == 409, response.text
    assert (await client.get("/api/applications")).json() == []
    assert (sorted(str(path) for path in directory.rglob("*")) if directory.exists() else []) == before
    assert client.sent == []


@pytest.mark.asyncio
async def test_api_create_validation(client):
    agents = (await client.get("/api/agents", headers=client.headers)).json()
    # Pydantic rejects the empty name/description before the manager sees them.
    resp = await client.post(
        "/api/applications",
        json={"name": "", "description": "d", "agent_id": agents[0]["id"]},
        headers=client.headers,
    )
    assert resp.status_code == 422
    resp = await client.post(
        "/api/applications",
        json={"name": "X", "description": "d", "agent_id": "ghost"},
        headers=client.headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_api_duplicate_name_is_409(client):
    await _api_create(client, name="Dup")
    agents = (await client.get("/api/agents", headers=client.headers)).json()
    resp = await client.post(
        "/api/applications",
        json={"name": "dup", "description": "d", "agent_id": agents[0]["id"]},
        headers=client.headers,
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_api_patch_and_build_and_delete(client):
    created = await _api_create(client)

    patched = await client.patch(
        f"/api/applications/{created['id']}",
        json={"name": "Renamed", "icon": "🧮"},
        headers=client.headers,
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "Renamed"
    assert patched.json()["icon"] == "🧮"

    client.sent.clear()  # type: ignore[attr-defined]
    built = await client.post(
        f"/api/applications/{created['id']}/build",
        json={"prompt": "make it blue"},
        headers=client.headers,
    )
    assert built.status_code == 200
    assert built.json()["status"] == "building"
    assert "make it blue" in client.sent[0][1]  # type: ignore[attr-defined]

    resp = await client.delete(
        f"/api/applications/{created['id']}", headers=client.headers
    )
    assert resp.status_code == 204
    assert (await client.get("/api/applications", headers=client.headers)).json() == []


@pytest.mark.asyncio
async def test_api_archive_and_restore(client):
    created = await _api_create(client, name="Archivable")

    resp = await client.post(
        f"/api/applications/{created['id']}/archive", headers=client.headers
    )
    assert resp.status_code == 200
    assert resp.json()["archived"] is True
    assert (await client.get("/api/applications", headers=client.headers)).json() == []
    archived = await client.get(
        "/api/applications", params={"archived": "true"}, headers=client.headers
    )
    assert [a["id"] for a in archived.json()] == [created["id"]]

    resp = await client.post(
        f"/api/applications/{created['id']}/unarchive", headers=client.headers
    )
    assert resp.status_code == 200
    assert resp.json()["archived"] is False
    live = (await client.get("/api/applications", headers=client.headers)).json()
    assert [a["id"] for a in live] == [created["id"]]


@pytest.mark.asyncio
async def test_api_build_requires_a_prompt(client):
    created = await _api_create(client)
    resp = await client.post(
        f"/api/applications/{created['id']}/build", json={"prompt": ""}, headers=client.headers
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Static serving
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_static_serves_the_entrypoint_for_the_app_root(client):
    created = await _api_create(client, name="Served")
    with open(os.path.join(created["app_dir"], "index.html"), "w") as f:
        f.write("<h1>Hello Octopus</h1>")

    resp = await client.get(f"/apps/{created['id']}/", headers=client.headers)
    assert resp.status_code == 200
    assert "Hello Octopus" in resp.text
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_static_serves_nested_assets(client):
    created = await _api_create(client, name="Assets")
    os.makedirs(os.path.join(created["app_dir"], "assets"))
    with open(os.path.join(created["app_dir"], "assets", "app.js"), "w") as f:
        f.write("console.log(1)")

    resp = await client.get(f"/apps/{created['id']}/assets/app.js", headers=client.headers)
    assert resp.status_code == 200
    assert resp.text == "console.log(1)"
    assert "javascript" in resp.headers["content-type"]


@pytest.mark.asyncio
async def test_static_accepts_query_token_and_cookie(client):
    token = client.headers["Authorization"].removeprefix("Bearer ")
    created = await _api_create(client, name="Authy")
    with open(os.path.join(created["app_dir"], "index.html"), "w") as f:
        f.write("ok")

    # No credentials at all.
    client.headers.pop("Authorization")
    assert (await client.get(f"/apps/{created['id']}/")).status_code == 401
    # ?token= — what an "open in a new tab" link uses.
    assert (
        await client.get(f"/apps/{created['id']}/", params={"token": token})
    ).status_code == 200
    # The cookie the SPA sets before mounting the iframe. Set on the client
    # rather than per request: httpx deprecated per-request `cookies=` because
    # what it should do about persistence is ambiguous, and a browser sends the
    # cookie jar it has — which is what setting it on the client models.
    client.cookies.set("octopus_app_token", token)
    assert (await client.get(f"/apps/{created['id']}/")).status_code == 200
    client.cookies.set("octopus_app_token", "wrong")
    assert (await client.get(f"/apps/{created['id']}/")).status_code == 401
    client.cookies.clear()


@pytest.mark.asyncio
async def test_static_cookie_survives_a_token_javascript_encodes(client, monkeypatch):
    """A token containing `@` must still open the iframe.

    The SPA writes this cookie with `encodeURIComponent`, and has to: a raw `;`
    or `,` in the value would truncate the header. Starlette's cookie parser
    strips quoting but does not decode percent escapes, so `@` reached the check
    as `%40`, matched nothing, and every `/apps/*` request 401'd — the iframe,
    its scripts and its own fetches with it.

    Nothing caught it because every token in the suite, and the one on the box
    that found it, happened to encode to itself. It appears the first time
    someone rotates to a token that does not — which is exactly how it appeared.
    """
    from server import users as users_module

    created = await _api_create(client, name="Encoded")
    with open(os.path.join(created["app_dir"], "index.html"), "w") as f:
        f.write("ok")

    # Any token with a character JS encodes reproduces it; never a real one.
    rotated = "an@encoded-token!"
    monkeypatch.setattr(users_module, "new_token", lambda: rotated)
    owner = await session_manager.db.get_system_agent()
    assert await deps.get_user_manager().issue_token(owner["user_id"]) == rotated
    client.headers.pop("Authorization")
    # Character for character what the browser stores: encodeURIComponent
    # leaves !*()' alone and percent-encodes the rest.
    encoded = quote(rotated, safe="!*()'")
    assert encoded != rotated, "pick a token JS actually encodes"

    client.cookies.set("octopus_app_token", encoded)
    assert (await client.get(f"/apps/{created['id']}/")).status_code == 200
    client.cookies.set("octopus_app_token", quote("nope@wrong", safe="!*()'"))
    assert (await client.get(f"/apps/{created['id']}/")).status_code == 401
    client.cookies.clear()


@pytest.mark.asyncio
async def test_static_traversal_is_a_404(client, tmp_path):
    """Encoded traversal is what actually reaches the server — HTTP clients
    (httpx here, every browser in the wild) collapse literal `..` segments
    before sending. Both shapes are covered: the encoded ones must 404, and
    none of them may ever return the file outside the app."""
    created = await _api_create(client, name="Locked")
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")

    for path in ("..%2Fsecret.txt", "%2e%2e/secret.txt", "....//secret.txt"):
        resp = await client.get(f"/apps/{created['id']}/{path}", headers=client.headers)
        assert resp.status_code == 404, path

    for path in ("../secret.txt", "a/../../secret.txt"):
        resp = await client.get(f"/apps/{created['id']}/{path}", headers=client.headers)
        assert "top secret" not in resp.text, path


@pytest.mark.asyncio
async def test_static_refuses_a_symlink_out_of_the_app(client, tmp_path):
    created = await _api_create(client, name="Linked")
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret")
    os.symlink(str(secret), os.path.join(created["app_dir"], "link.txt"))

    resp = await client.get(f"/apps/{created['id']}/link.txt", headers=client.headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_static_missing_file_and_missing_app_are_404(client):
    created = await _api_create(client, name="Empty")
    assert (
        await client.get(f"/apps/{created['id']}/", headers=client.headers)
    ).status_code == 404
    assert (await client.get("/apps/ghost/x.html", headers=client.headers)).status_code == 404


@pytest.mark.asyncio
async def test_static_bare_app_url_redirects_to_a_trailing_slash(client):
    created = await _api_create(client, name="Redirected")
    resp = await client.get(f"/apps/{created['id']}", headers=client.headers)
    assert resp.status_code in (307, 308)
    assert resp.headers["location"] == f"/apps/{created['id']}/"


# --------------------------------------------------------------------------- #
# Icon discovery — an application supplying its own icon
# --------------------------------------------------------------------------- #


def _app(tmp_path, name="app"):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def test_icon_found_by_convention_at_the_root(tmp_path):
    """`icon.svg` at the root is what compose_build_prompt asks agents for."""
    from server.applications import discover_icon_src

    d = _app(tmp_path)
    (Path(d) / "index.html").write_text("<html></html>")
    (Path(d) / "icon.svg").write_text("<svg/>")
    assert discover_icon_src(d, "index.html") == "icon.svg"


def test_convention_candidates_are_tried_in_priority_order(tmp_path):
    from server.applications import discover_icon_src

    d = _app(tmp_path)
    (Path(d) / "index.html").write_text("<html></html>")
    (Path(d) / "favicon.png").write_text("x")
    (Path(d) / "apple-touch-icon.png").write_text("x")
    assert discover_icon_src(d, "index.html") == "favicon.png"
    (Path(d) / "icon.svg").write_text("<svg/>")
    assert discover_icon_src(d, "index.html") == "icon.svg"


def test_icon_found_via_the_link_tag_in_a_subdirectory(tmp_path):
    """The case a convention-only implementation misses. Real apps declare
    their mark with <link rel="icon"> and keep it in a subdirectory — both apps
    on the machine this feature was requested from do exactly that."""
    from server.applications import discover_icon_src

    d = _app(tmp_path)
    (Path(d) / "assets").mkdir()
    (Path(d) / "assets" / "mark.svg").write_text("<svg/>")
    (Path(d) / "index.html").write_text(
        '<html><head><link rel="icon" href="assets/mark.svg" '
        'type="image/svg+xml" /></head></html>'
    )
    assert discover_icon_src(d, "index.html") == "assets/mark.svg"


def test_inline_data_uri_icon_is_kept_verbatim(tmp_path):
    """An inline SVG favicon IS the app's real mark. Rendered through <img
    src>, so scripts inside the SVG don't execute."""
    from server.applications import discover_icon_src

    d = _app(tmp_path)
    uri = "data:image/svg+xml,%3Csvg%20xmlns='http://www.w3.org/2000/svg'%3E%3C/svg%3E"
    (Path(d) / "index.html").write_text(
        f'<html><head><link rel="icon" href="{uri}"></head></html>'
    )
    assert discover_icon_src(d, "index.html") == uri


def test_non_image_data_uri_is_rejected(tmp_path):
    from server.applications import discover_icon_src

    d = _app(tmp_path)
    (Path(d) / "index.html").write_text(
        '<html><head><link rel="icon" href="data:text/html,<script>alert(1)</script>">'
        "</head></html>"
    )
    assert discover_icon_src(d, "index.html") is None


def test_remote_icon_is_not_adopted(tmp_path):
    """Not ours to serve, and not always reachable."""
    from server.applications import discover_icon_src

    d = _app(tmp_path)
    (Path(d) / "index.html").write_text(
        '<html><head><link rel="icon" href="https://example.com/i.png"></head></html>'
    )
    assert discover_icon_src(d, "index.html") is None


def test_icon_escaping_the_app_dir_is_rejected(tmp_path):
    """Same guard as the static route: `..` and symlinks pointing out are
    refused, so an app can't nominate a file it was never given."""
    from server.applications import discover_icon_src

    outside = tmp_path / "secret.svg"
    outside.write_text("<svg/>")
    d = _app(tmp_path, "escaper")
    (Path(d) / "index.html").write_text(
        '<html><head><link rel="icon" href="../secret.svg"></head></html>'
    )
    assert discover_icon_src(d, "index.html") is None

    # ...and via a symlink that points outside.
    d2 = _app(tmp_path, "linker")
    (Path(d2) / "index.html").write_text(
        '<html><head><link rel="icon" href="link.svg"></head></html>'
    )
    os.symlink(str(outside), str(Path(d2) / "link.svg"))
    assert discover_icon_src(d2, "index.html") is None


def test_oversized_icon_file_is_skipped(tmp_path):
    """A stray large asset isn't a sidebar icon, and would be fetched on every
    render of every row."""
    from server.applications import _MAX_ICON_BYTES, discover_icon_src

    d = _app(tmp_path)
    (Path(d) / "index.html").write_text("<html></html>")
    (Path(d) / "icon.png").write_bytes(b"x" * (_MAX_ICON_BYTES + 1))
    assert discover_icon_src(d, "index.html") is None


def test_oversized_inline_icon_is_skipped(tmp_path):
    """An inline icon ships in every API response, so it has to stay small."""
    from server.applications import _MAX_DATA_ICON_CHARS, discover_icon_src

    d = _app(tmp_path)
    huge = "data:image/svg+xml," + ("a" * (_MAX_DATA_ICON_CHARS + 1))
    (Path(d) / "index.html").write_text(
        f'<html><head><link rel="icon" href="{huge}"></head></html>'
    )
    assert discover_icon_src(d, "index.html") is None


def test_no_icon_anywhere_is_none(tmp_path):
    from server.applications import discover_icon_src

    d = _app(tmp_path)
    (Path(d) / "index.html").write_text("<html><head></head></html>")
    assert discover_icon_src(d, "index.html") is None


def test_missing_entrypoint_does_not_throw(tmp_path):
    """Discovery runs even for a failed build, where the entrypoint may never
    have been written."""
    from server.applications import discover_icon_src

    d = _app(tmp_path)
    assert discover_icon_src(d, "index.html") is None
    (Path(d) / "icon.svg").write_text("<svg/>")
    assert discover_icon_src(d, "index.html") == "icon.svg"


async def test_current_reopen_preserves_a_missing_discovered_icon(tmp_path):
    """Opening a complete baseline keeps the original app and nullable icon."""
    db_file = str(tmp_path / "old.db")
    seed = Database(db_file)
    await seed.initialize()
    owner, _ = await create_account(seed, extra_roots=[tmp_path])
    await seed.save_application(app_id="a1", name="Legacy", app_dir="/tmp/legacy",
                                entrypoint="index.html", status="ready", created_at="t",
                                updated_at="t", user_id=owner["id"])
    expected = await seed.get_application("a1", owner["id"])
    await seed.close()

    db = Database(db_file)
    await db.initialize()
    try:
        row = await db.get_application("a1")
        assert row is not None
        assert row == expected
        assert row["user_id"] == owner["id"]
        assert row["name"] == "Legacy"    # the old row survived
        assert row["icon_src"] is None    # and simply has no icon yet
    finally:
        await db.close()


async def test_evaluate_discovers_and_clears_the_icon(am, db, mgr, sent, models):
    """The icon refreshes on every evaluation: it appears when the agent writes
    one, and clears when the file is removed — without ever touching the emoji
    a user typed."""
    app = await _create_app(am, db, name="Iconic")
    app_dir = app["app_dir"]
    Path(app_dir, "index.html").write_text("<html></html>")
    Path(app_dir, "icon.svg").write_text("<svg/>")

    await models.emit(mgr, {"type": "result", "session_id": app["session_id"]})
    assert (await db.get_application(app["id"]))["icon_src"] == "icon.svg"

    # Agent removes it in a later build → the record must not keep pointing at
    # a file that no longer exists.
    os.remove(Path(app_dir, "icon.svg"))
    await am._evaluate(app["id"], broadcast=False)
    assert (await db.get_application(app["id"]))["icon_src"] is None


async def test_discovered_icon_never_overwrites_a_user_emoji(am, db, mgr, sent, models):
    """`icon` is the user's; `icon_src` is the app's. A rebuild touches only
    the second."""
    app = await _create_app(am, db, name="Emoji Keeper", icon="💠")
    Path(app["app_dir"], "index.html").write_text("<html></html>")
    Path(app["app_dir"], "icon.svg").write_text("<svg/>")

    await models.emit(mgr, {"type": "result", "session_id": app["session_id"]})
    row = await db.get_application(app["id"])
    assert row["icon"] == "💠"          # untouched
    assert row["icon_src"] == "icon.svg"  # and the file was still found


async def test_refresh_icons_backfills_existing_applications(am, db, sent):
    """Discovery otherwise only runs after a build, which would leave every
    application that already exists showing the generic fallback until someone
    happened to rebuild it. The acceptance criterion is "with no user action"."""
    app = await _create_app(am, db, name="Already Built")
    Path(app["app_dir"], "index.html").write_text("<html></html>")
    Path(app["app_dir"], "icon.svg").write_text("<svg/>")
    assert (await db.get_application(app["id"]))["icon_src"] is None

    assert await am.refresh_icons() == 1
    assert (await db.get_application(app["id"]))["icon_src"] == "icon.svg"

    # Idempotent: nothing changed, nothing rewritten.
    assert await am.refresh_icons() == 0


async def test_refresh_icons_leaves_the_emoji_and_status_alone(am, db, sent):
    app = await _create_app(am, db, name="Untouched", icon="💠")
    Path(app["app_dir"], "index.html").write_text("<html></html>")
    Path(app["app_dir"], "icon.svg").write_text("<svg/>")
    before = await db.get_application(app["id"])

    await am.refresh_icons()
    after = await db.get_application(app["id"])
    assert after["icon"] == "💠"
    assert after["status"] == before["status"]
    assert after["icon_src"] == "icon.svg"


# --------------------------------------------------------------------------- #
# Backends (application-backends.md)
# --------------------------------------------------------------------------- #


def _script(app_dir: str, name: str, body: str, executable: bool = True) -> None:
    import stat

    path = Path(app_dir) / name
    path.write_text(body)
    if executable:
        path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_a_backend_is_declared_by_an_executable_start_script(tmp_path):
    """Presence of `start.sh` IS the declaration — there is no manifest. A
    non-executable one reports as absent rather than being run, so a forgotten
    chmod fails as "no backend" instead of a confusing exec error."""
    from server.applications import has_backend

    d = tmp_path / "app"
    d.mkdir()
    assert has_backend(str(d)) is False

    _script(str(d), "start.sh", "#!/bin/sh\nsleep 1\n", executable=False)
    assert has_backend(str(d)) is False

    _script(str(d), "start.sh", "#!/bin/sh\nsleep 1\n")
    assert has_backend(str(d)) is True


def test_script_environment_excludes_the_servers_own(monkeypatch):
    """The server's environment holds the Octopus token, credentials and tunnel
    config. A backend has no business seeing any of it, and inheriting it
    wholesale is invisible until it isn't."""
    from server.app_backends import script_env

    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "super-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    env = script_env("a1", "/apps/demo", port=4100)

    assert "OCTOPUS_AUTH_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert env["APP_DATA_DIR"] == "/apps/demo.data"
    assert env["APP_RUNTIME_DIR"] == "/apps/demo.runtime"
    assert env["PORT"] == "4100"


def test_data_and_runtime_are_siblings_not_subdirectories(tmp_path):
    """The data directory must survive a rebuild rewriting the code directory,
    which a subdirectory of it would not."""
    from server.applications import data_dir_for, runtime_dir_for

    app = str(tmp_path / "demo")
    assert data_dir_for(app) == str(tmp_path / "demo.data")
    assert runtime_dir_for(app) == str(tmp_path / "demo.runtime")
    for d in (data_dir_for(app), runtime_dir_for(app)):
        assert not d.startswith(app + os.sep)


@pytest.mark.asyncio
@pytest.mark.parametrize("query_contract", ["baseline", "multiplicity", "credentials"])
async def test_a_real_backend_answers_through_the_proxy(client, query_contract):
    """The whole feature, end to end: an application ships `start.sh`, Octopus
    starts it, and `/apps/{id}/api/…` reaches it.

    The backend here is a real HTTP server in a real subprocess — a stub would
    prove the routing and none of the contract (port binding, foreground
    execution, readiness, teardown).
    """
    from server.app_backends import backend_supervisor

    app_row = await _api_create(client, name="With Backend")
    app_dir = app_row["app_dir"]
    Path(app_dir, "index.html").write_text("<html>ui</html>")
    _script(
        app_dir,
        "start.sh",
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "cat > \"$APP_RUNTIME_DIR/serve.py\" <<'EOF'\n"
        "import json, os\n"
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "class H(BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        body = json.dumps({'path': self.path,\n"
        "                           'authorization': self.headers.get('authorization'),\n"
        "                           'cookie': self.headers.get('cookie'),\n"
        "                           'data_dir': os.environ['APP_DATA_DIR']}).encode()\n"
        "        self.send_response(200)\n"
        "        self.send_header('content-type', 'application/json')\n"
        "        self.end_headers()\n"
        "        self.wfile.write(body)\n"
        "    def log_message(self, *a):\n"
        "        pass\n"
        "HTTPServer(('127.0.0.1', int(os.environ['PORT'])), H).serve_forever()\n"
        "EOF\n"
        'exec python3 "$APP_RUNTIME_DIR/serve.py"\n',
    )
    os.makedirs(Path(app_dir + ".runtime"), exist_ok=True)

    try:
        resp = await client.get(
            f"/apps/{app_row['id']}/api/notes?limit=2", headers=client.headers
        )
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        # The prefix is preserved, so a backend routes on the path it declared.
        assert payload["path"] == "/api/notes?limit=2"
        # And it ran with the data directory the contract promises.
        assert payload["data_dir"] == app_dir + ".data"

        if query_contract == "multiplicity":
            pairs = [
                ("tag", "alpha"), ("limit", "2"), ("tag", "beta"), ("empty", ""),
                ("text", "中文 + & ="), ("limit", "3"), ("token_hint", "visible"), ("Token", "ordinary-value"),
            ]
            # Build the actual incoming URL: httpx's params helper itself groups
            # repeated keys, hiding the interleaving this contract protects.
            echoed = await client.get(f"/apps/{app_row['id']}/api/notes?{urlencode(pairs)}", headers=client.headers)
            assert echoed.status_code == 200, echoed.text
            assert parse_qsl(urlsplit(echoed.json()["path"]).query, keep_blank_values=True) == pairs
        elif query_contract == "credentials":
            from server.applications import app_scope_token

            cases = [
                (client.headers, ["discarded-first", "discarded-last"]),
                ({"Authorization": ""}, ["discarded-first", client.headers["Authorization"].removeprefix("Bearer ")]),
                ({"Authorization": "", "Cookie": f"octopus_app_token={client.headers['Authorization'].removeprefix('Bearer ')}; private_cookie=private"}, [""]),
                ({"Authorization": "", "X-Octopus-App-Token": app_scope_token(app_row["id"])}, ["discarded-first", "discarded-last"]),
            ]
            for headers, tokens in cases:
                pairs = [("tag", "visible"), *[("token", token) for token in tokens], ("token_hint", "kept")]
                echoed = await client.get(f"/apps/{app_row['id']}/api/notes?{urlencode(pairs)}", headers=headers)
                assert echoed.status_code == 200, echoed.text
                observed = echoed.json()
                assert parse_qsl(urlsplit(observed["path"]).query, keep_blank_values=True) == [("tag", "visible"), ("token_hint", "kept")]
                assert observed["authorization"] is None and observed["cookie"] is None

        # Static files still come from the code directory, unproxied.
        page = await client.get(f"/apps/{app_row['id']}/index.html", headers=client.headers)
        assert page.status_code == 200
        assert "ui" in page.text

        # The row reports the backend as running, with its port.
        detail = (await client.get(
            f"/api/applications/{app_row['id']}", headers=client.headers
        )).json()
        assert detail["backend"]["state"] == "running"
        assert detail["backend"]["port"]
    finally:
        await backend_supervisor.stop_all()


@pytest.mark.asyncio
async def test_proxy_is_404_when_the_app_has_no_backend(client):
    app_row = await _api_create(client, name="Static Only")
    Path(app_row["app_dir"], "index.html").write_text("<html></html>")
    resp = await client.get(f"/apps/{app_row['id']}/api/anything", headers=client.headers)
    assert resp.status_code == 404
    assert "no backend" in resp.text.lower()


@pytest.mark.asyncio
async def test_proxy_reports_a_broken_backend_rather_than_hanging(client):
    """A backend that exits immediately is a 503 naming the reason, not a 500
    and not a wait — "it doesn't work" with no output is the failure this
    feature exists to avoid."""
    from server.app_backends import backend_supervisor

    app_row = await _api_create(client, name="Broken Backend")
    _script(app_row["app_dir"], "start.sh", "#!/bin/sh\necho 'boom' >&2\nexit 7\n")
    try:
        resp = await client.get(f"/apps/{app_row['id']}/api/x", headers=client.headers)
        assert resp.status_code == 503
        assert "exited 7" in resp.text

        detail = (await client.get(
            f"/api/applications/{app_row['id']}", headers=client.headers
        )).json()
        assert detail["backend"]["state"] == "failed"
        assert any("boom" in line for line in detail["backend"]["log_tail"])
    finally:
        await backend_supervisor.stop_all()


@pytest.mark.asyncio
async def test_proxy_requires_auth(client):
    app_row = await _api_create(client, name="Guarded")
    _script(app_row["app_dir"], "start.sh", "#!/bin/sh\nsleep 5\n")
    resp = await client.get(f"/apps/{app_row['id']}/api/x", headers={"Authorization": ""})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_deleting_an_application_stops_its_backend(client):
    """A running server must not outlive its application. Nothing points at it
    afterwards, so neither the reaper nor shutdown could ever reach it — it
    would keep its port and hold files open under a deleted directory."""
    from server.app_backends import RUNNING, backend_supervisor

    app_row = await _api_create(client, name="Doomed Backend")
    _script(
        app_row["app_dir"],
        "start.sh",
        '#!/usr/bin/env bash\nexec python3 -m http.server "$PORT" --bind 127.0.0.1\n',
    )
    try:
        resp = await client.get(f"/apps/{app_row['id']}/api/x", headers=client.headers)
        assert resp.status_code in (200, 404)  # the stub server's own answer
        state = backend_supervisor.get(app_row["id"])
        assert state and state.state == RUNNING
        proc = state.process
        assert proc is not None and proc.returncode is None

        await client.delete(f"/api/applications/{app_row['id']}", headers=client.headers)

        await asyncio.sleep(0.3)
        assert proc.returncode is not None, "the backend process outlived its app"
    finally:
        await backend_supervisor.stop_all()


@pytest.mark.asyncio
async def test_deleting_an_application_removes_its_data_and_runtime(client):
    """The data and runtime directories are siblings, so removing the code
    directory leaves them behind unless we say otherwise."""
    app_row = await _api_create(client, name="Sibling Cleanup")
    app_dir = app_row["app_dir"]
    Path(app_dir, "index.html").write_text("<html></html>")
    os.makedirs(app_dir + ".data", exist_ok=True)
    os.makedirs(app_dir + ".runtime", exist_ok=True)
    Path(app_dir + ".data", "notes.json").write_text("[]")

    await client.delete(f"/api/applications/{app_row['id']}", headers=client.headers)

    assert not os.path.exists(app_dir)
    assert not os.path.exists(app_dir + ".data")
    assert not os.path.exists(app_dir + ".runtime")


def test_build_prompt_states_the_home_screen_icon_rules():
    """The four rules an agent gets wrong otherwise. Each one produces a
    silently broken icon rather than an error, which is why they're spelled out
    rather than left to "ship an icon"."""
    prompt = ApplicationManager.compose_build_prompt(
        name="X", description="d", app_dir="/tmp/x"
    )
    assert "apple-touch-icon.png" in prompt
    assert "PNG" in prompt and "SVG" in prompt      # Safari ignores SVG here
    assert "opaque" in prompt                        # alpha composites to black
    assert "180x180" in prompt
    assert "full-bleed" in prompt                    # iOS applies its own mask
    # And the trap specific to us: apps live under /apps/<id>/, so an absolute
    # href resolves against Octopus instead of the app.
    assert "RELATIVE" in prompt


def test_apple_touch_icon_is_discovered_for_the_sidebar_too(tmp_path):
    """The same file doubles as the sidebar icon when an app ships nothing
    else — it's already in the candidate list, ahead of favicon.ico."""
    from server.applications import discover_icon_src

    d = tmp_path / "app"
    d.mkdir()
    (d / "index.html").write_text("<html></html>")
    (d / "apple-touch-icon.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (d / "favicon.ico").write_bytes(b"\x00")
    assert discover_icon_src(str(d), "index.html") == "apple-touch-icon.png"


# ---------------------------------------------------------------------------
# Sidebar pins (sidebar-pins.md)
# ---------------------------------------------------------------------------


def _pinned_ids(apps: list[dict]) -> list[str]:
    return [a["id"] for a in sorted(
        (a for a in apps if a["pinned"]), key=lambda a: a["pin_order"]
    )]


@pytest.mark.asyncio
async def test_new_applications_are_pinned_at_the_bottom(am, db, sent):
    first = await _create_app(am, db, name="First")
    second = await _create_app(am, db, name="Second")
    assert first["pinned"] and second["pinned"]
    assert _pinned_ids(await db.load_applications()) == [first["id"], second["id"]]


@pytest.mark.asyncio
async def test_unpin_and_repin_broadcast_and_append(am, db, mgr, sent, monkeypatch):
    a = await _create_app(am, db, name="A")
    b = await _create_app(am, db, name="B")
    events: list[dict] = []

    # `**_` so the double keeps matching `_broadcast`, which now takes the
    # frame's owner as a keyword (multi-tenancy.md §7). A stub that does not
    # accept what the real thing is called with is how the Telegram
    # Allow/Deny bug shipped green.
    async def capture(msg, **_):
        events.append(msg)

    monkeypatch.setattr(mgr, "_broadcast", capture)

    unpinned = await am.set_pinned(a["id"], False)
    assert unpinned["pinned"] is False
    # Still live — listed, servable — just not a shortcut.
    assert [x["id"] for x in await db.load_applications()] == [a["id"], b["id"]]
    assert events[-1]["type"] == "application_updated"
    assert events[-1]["application"]["pinned"] is False

    await am.set_pinned(a["id"], True)
    assert _pinned_ids(await db.load_applications()) == [b["id"], a["id"]]


@pytest.mark.asyncio
async def test_pinning_an_archived_application_is_refused(am, db, sent):
    a = await _create_app(am, db, name="Shelved")
    await am.set_archived(a["id"], True)
    with pytest.raises(ApplicationError, match="Restore"):
        await am.set_pinned(a["id"], True)


@pytest.mark.asyncio
async def test_restoring_an_application_pins_it(am, db, sent):
    a = await _create_app(am, db, name="Returning")
    await am.set_pinned(a["id"], False)
    await am.set_archived(a["id"], True)
    restored = await am.set_archived(a["id"], False)
    assert restored["pinned"] is True


@pytest.mark.asyncio
async def test_reorder_broadcasts_only_the_rows_that_moved(
    am, db, mgr, sent, monkeypatch
):
    a = await _create_app(am, db, name="A")
    b = await _create_app(am, db, name="B")
    c = await _create_app(am, db, name="C")
    events: list[dict] = []

    # `**_` so the double keeps matching `_broadcast`, which now takes the
    # frame's owner as a keyword (multi-tenancy.md §7). A stub that does not
    # accept what the real thing is called with is how the Telegram
    # Allow/Deny bug shipped green.
    async def capture(msg, **_):
        events.append(msg)

    monkeypatch.setattr(mgr, "_broadcast", capture)

    # [A, B, C] -> [A, C, B]: A keeps position 1, so only B and C travel.
    rows = await am.reorder_pins([a["id"], c["id"], b["id"]])
    assert _pinned_ids(rows) == [a["id"], c["id"], b["id"]]
    assert sorted(e["application_id"] for e in events) == sorted([b["id"], c["id"]])


@pytest.mark.asyncio
async def test_reorder_rejects_unpinned_unknown_and_repeated(am, db, sent):
    a = await _create_app(am, db, name="A")
    b = await _create_app(am, db, name="B")
    await am.set_pinned(b["id"], False)
    for ids in ([a["id"], b["id"]], [a["id"], "ghost"], [a["id"], a["id"]]):
        with pytest.raises(ApplicationError):
            await am.reorder_pins(ids)


@pytest.mark.asyncio
async def test_api_pin_unpin_and_order(client):
    one = await _api_create(client, name="One")
    two = await _api_create(client, name="Two")

    resp = await client.post(
        f"/api/applications/{one['id']}/unpin", headers=client.headers
    )
    assert resp.status_code == 200
    assert resp.json()["pinned"] is False
    # The response carries live backend state like every other read.
    assert resp.json()["backend"]["state"] == "absent"

    resp = await client.post(f"/api/applications/{one['id']}/pin", headers=client.headers)
    assert resp.json()["pinned"] is True

    resp = await client.put(
        "/api/applications/pin-order",
        json={"ids": [one["id"], two["id"]]},
        headers=client.headers,
    )
    assert resp.status_code == 200
    assert _pinned_ids(resp.json()) == [one["id"], two["id"]]

    resp = await client.put(
        "/api/applications/pin-order", json={"ids": ["ghost"]}, headers=client.headers
    )
    assert resp.status_code == 400
    assert (
        await client.post("/api/applications/ghost/pin", headers=client.headers)
    ).status_code == 404
