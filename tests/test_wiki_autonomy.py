"""Normal dispatched turns can use their own Wiki without a human command."""

import errno
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from server.harness import HarnessEvent, get_harness
from server.harness.assembly import select_mcp_servers
from server.wiki.tasks import tasks
from tests import test_wiki_api as wiki_api
from tests.fake_run import FakeRunBase

client = wiki_api.client


@pytest.mark.parametrize("selected,expected,restore_old", [
    (None, "auto", False), ("auto", "auto", False), ("read", "read", False),
    ("maintain", "maintain", False), (None, "read", True),
])
@pytest.mark.parametrize("actor", ["reader", "curator"])
async def test_current_modes_and_restored_off_keep_lookup_and_enforce_maintenance_limits(
    client, tmp_path, monkeypatch, selected, expected, restore_old, actor,
):
    vault = tmp_path / "vault"
    (vault / "Library").mkdir(parents=True)
    note = vault / "Library/note.md"
    note.write_text("# Evidence\nPreserve these words.\n")
    from server.agent_manager import AgentManager

    config = await wiki_api.configure(client, vault)
    manager = wiki_api.session_manager
    owner = (await manager.db.get_system_agent())["user_id"]
    reader = await AgentManager(manager.db).create_agent(name="Reader", backend="codex", mcp_servers=[], user_id=owner)
    assert reader["id"] != config["curator_agent_id"]
    actor_id = reader["id"] if actor == "reader" else config["curator_agent_id"]
    session = await manager.create_session(actor_id, "Autonomy", str(tmp_path), backend="codex")
    captured = []

    def create(run_config):
        from server.harness.assembly import compose_system_prompt
        from server.wiki.workflow import BOOTSTRAP, guide, view

        entries = select_mcp_servers(run_config.mcp_servers, [], {}, session_id=session.id,
                                     user_id=session.user_id, wiki_task_id=run_config.wiki_task_id)
        wiki = next((entry for entry in entries if entry.key == "wiki"), None)
        captured.append((run_config, wiki))
        prompt = compose_system_prompt(run_config.system_prompt, "", [], wiki_mode=run_config.wiki_mode,
                                       wiki_capability=True, wiki_task_id=run_config.wiki_task_id)
        assert BOOTSTRAP in prompt and guide("query-v2") not in prompt
        assert guide("retrieval-v2") not in prompt and guide("maintenance-v2") not in prompt
        if expected == "read":
            assert view(tasks.active[run_config.wiki_task_id]).card["may_submit"] is False

        class Run(FakeRunBase):
            def set_idle_handler(self, handler):
                pass

            async def start(self, *args, **kwargs):
                pass

            async def stream(self):
                assert wiki is not None
                task = tasks.active[run_config.wiki_task_id]
                assert task.mode == expected
                from server.mcp_identity import (
                    reset_current_scope,
                    set_current_scope,
                    verify,
                )
                from server.mcp_servers.wiki import mcp

                scope = verify(wiki.credential)
                assert scope is not None
                context = set_current_scope(scope)
                try:
                    names = {tool.name for tool in await mcp.list_tools()}
                finally:
                    reset_current_scope(context)
                assert {"search", "read", "read_many", "load_guide"} <= names
                assert ("maintain" in names) == (expected in {"auto", "maintain"})
                assert not {"organize", "publish", "revise", "restructure", "blocks"} & names
                headers = {"Authorization": f"Bearer {wiki.credential}"}
                response = await client.get("/api/wiki/read", params={"doc": "Library/note.md"}, headers=headers)
                assert response.status_code == 200, response.text
                query_guide = await client.get("/api/wiki/guide", params={"module": "query"}, headers=headers)
                assert query_guide.status_code == 200
                retrieval_guide = await client.get("/api/wiki/guide", params={"module": "retrieval"}, headers=headers)
                assert retrieval_guide.status_code == 200
                for module in ("maintenance", "article", "writing"):
                    worker_guide = await client.get("/api/wiki/guide", params={"module": module}, headers=headers)
                    assert worker_guide.status_code == 403
                response = await client.post("/api/wiki/maintain", headers=headers, json={
                    "idempotency_key": "autonomous-maintenance", "scope": "Preserve source; check the note", "paths": ["Library/note.md"],
                })
                assert response.status_code == (200 if expected in {"auto", "maintain"} else 403), response.text
                # Autonomous submission still cannot publish or impersonate a human editor.
                response = await client.put("/api/wiki/document", headers=headers, json={
                    "path": "Library/note.md", "content": "Overwrite", "reason": "Attempt direct write", "expected_revision": None,
                })
                assert response.status_code == 403
                yield HarnessEvent(type="result", content="done", is_error=False)

            async def stop(self):
                pass

            async def interrupt(self):
                pass

        return Run()

    monkeypatch.setattr(get_harness("codex"), "create_run", create)
    kwargs = {"wiki_mode": selected} if selected is not None else {}
    prompt = "/wiki maintain Library/note.md\nUse our knowledge" if selected == "maintain" else "Use our knowledge to answer this question"
    if restore_old:
        from tests.test_wiki_mode_protocol import historical_read_input
        await historical_read_input(manager, session)
        kwargs.update(client_request_id="restored-off", recovery_of_client_id="old-client")
    receipt = await manager.start_message(session.id, prompt, **kwargs)
    completion = await receipt.wait()
    assert completion.outcome.status == "success", completion.outcome
    assert len(captured) == 1
    assert note.read_text() == "# Evidence\nPreserve these words.\n"
    run_config, wiki = captured[0]
    if wiki:
        assert run_config.wiki_task_id not in tasks.active
        expired = await client.get("/api/wiki/read", params={"doc": "Library/note.md"}, headers={"Authorization": f"Bearer {wiki.credential}"})
        assert expired.status_code == 403


def test_unregistered_wiki_does_not_claim_available_tools_and_read_keeps_lookup():
    from server.harness.assembly import compose_system_prompt
    from server.wiki.workflow import BOOTSTRAP, guide

    missing = compose_system_prompt(None, "", [], wiki_capability=True)
    assert "unavailable" in missing
    assert guide("access") not in missing and guide("query-v2") not in missing
    read = compose_system_prompt(None, "", [], wiki_capability=True, wiki_mode="read")
    assert BOOTSTRAP in read and guide("query-v2") not in read


async def test_native_job_navigation_avoids_embedding_large_payload_in_command(client, tmp_path):
    from server.wiki.jobs import WikiJobs
    vault = tmp_path / "vault"
    (vault / "Library").mkdir(parents=True)
    (vault / "Library/note.md").write_text("# Source\nOriginal material\n")
    await wiki_api.configure(client, vault)
    owner = (await wiki_api.session_manager.db.get_system_agent())["user_id"]
    jobs = WikiJobs(owner)
    original = jobs.maintain("large-scope", "", "scope " * 30000, ["Library/note.md"], None, [], "human")
    response = await client.get("/api/wiki/jobs", params={"view": "compact", "idempotency_key": "large-scope"}, headers=client.headers)
    assert response.status_code == 200, response.text
    assert len(response.content) < 16000, "job navigation still carries the complete scope into shell argv"
    item = response.json()["items"][0]
    assert item["id"] == original["id"] and item["progress"]["total"] == 1
    assert "payload" not in item and "items" not in item
    assert jobs.get(original["id"])["payload"]["request"]["scope"] == "scope " * 30000
    missing = await client.get("/api/wiki/jobs", params={"view": "compact", "idempotency_key": "large"}, headers=client.headers)
    assert missing.json()["items"] == []


async def test_native_full_jobs_capture_avoids_actual_e2big_and_preserves_evidence(client, tmp_path, monkeypatch):
    from server.mcp_identity import mint, reset_current_scope, set_current_scope, verify
    from server.mcp_servers import wiki
    from server.wiki.jobs import WikiJobs
    from server.wiki.store import WikiError
    from server.workspace import paths_for

    vault = tmp_path / "vault"
    (vault / "Library").mkdir(parents=True)
    (vault / "Library/note.md").write_text("# Source\nKeep this material.\n")
    config = await wiki_api.configure(client, vault)
    owner = (await wiki_api.session_manager.db.get_system_agent())["user_id"]
    job = WikiJobs(owner).maintain("capture-large", "", "scope " * 30000, ["Library/note.md"], None, [], "human")
    full = (await client.get(f"/api/wiki/jobs/{job['id']}", headers=client.headers)).json()
    raw = json.dumps(full, ensure_ascii=False).encode()
    with pytest.raises(OSError) as oversized:
        subprocess.run([sys.executable, "-c", "", raw.decode()], check=True)
    assert oversized.value.errno == errno.E2BIG

    session = await wiki_api.session_manager.create_session(config["curator_agent_id"], "Capture", str(tmp_path), backend="codex")
    task_id = tasks.open(session, "auto")
    scope = verify(mint(session.id, user_id=session.user_id, wiki_task_id=task_id))
    context = set_current_scope(scope)
    monkeypatch.setattr(wiki, "call", lambda *args, **kwargs: full)
    try:
        # Large detail is captured automatically, rather than echoing its payload.
        captured = wiki.jobs(job_id=job["id"], view="full")
        assert len(json.dumps(captured).encode()) < 16000
        artifact = captured["artifact"]
        path = Path(artifact["path"])
        assert path.is_relative_to(paths_for(owner).workspace)
        assert path.stat().st_mode & 0o777 == 0o600
        content = path.read_bytes()
        assert len(content) == artifact["bytes"]
        assert hashlib.sha256(content).hexdigest() == artifact["sha256"]
        assert json.loads(content) == full
        assert captured["summary"]["id"] == job["id"]
        # A short command reads the stored evidence without carrying it in argv.
        program = "import json,sys;print(len(json.load(open(sys.argv[1]))['payload']['request']['scope']))"
        saved = subprocess.run([sys.executable, "-c", program, str(path)], capture_output=True, text=True, check=True)
        assert saved.stdout.strip() == str(len("scope " * 30000))
        tasks.close(task_id)
        with pytest.raises(WikiError):
            wiki.jobs(job_id=job["id"], view="full", export=True)
        assert list(path.parent.glob("*.json")) == [path]
    finally:
        tasks.close(task_id)
        reset_current_scope(context)


@pytest.mark.parametrize("layout", ["escaped-directory", "workspace-is-vault", "all-roots-in-vault"])
async def test_job_evidence_stays_confined_and_outside_canonical_vault(client, tmp_path, monkeypatch, layout):
    from server.config import settings
    from server.mcp_identity import mint, verify
    from server.wiki.evidence import capture_jobs
    from server.wiki.store import WikiError
    from server.workspace import paths_for

    owner = (await wiki_api.session_manager.db.get_system_agent())["user_id"]
    vault = tmp_path / "vault"
    if layout == "all-roots-in-vault":
        # Put both account workspace and Wiki state inside the vault before
        # registration; moving config alone would discard its ledger.
        monkeypatch.setattr(settings, "users_root", str(vault / "users"))
    workspace = paths_for(owner).workspace
    if layout == "workspace-is-vault":
        vault = workspace
    vault.mkdir(parents=True, exist_ok=True)
    config = await wiki_api.configure(client, vault)
    workspace.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    if layout == "escaped-directory":
        outside.mkdir()
        (workspace / "wiki-evidence").symlink_to(outside, target_is_directory=True)
    session = await wiki_api.session_manager.create_session(config["curator_agent_id"], "Confined evidence", str(tmp_path), backend="codex")
    task_id = tasks.open(session, "auto")
    scope = verify(mint(session.id, user_id=session.user_id, wiki_task_id=task_id))
    try:
        if layout == "workspace-is-vault":
            artifact = capture_jobs(scope, {"private": "job metadata"})
            path = Path(artifact["path"])
            from server.wiki.registry import registry

            assert path.is_relative_to(registry.state(owner) / "tool-evidence")
            assert not path.is_relative_to(vault)
            assert json.loads(path.read_text()) == {"private": "job metadata"}
        else:
            with pytest.raises(WikiError) as rejected:
                capture_jobs(scope, {"private": "job metadata"})
            assert rejected.value.status == (403 if layout == "escaped-directory" else 409)
            assert not list(outside.glob("*.json"))
            assert not list(vault.rglob("jobs-*.json"))
    finally:
        tasks.close(task_id)
