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


@pytest.mark.parametrize("selected,expected", [(None, "auto"), ("off", None), ("read", "read")])
async def test_normal_turn_has_scoped_autonomous_wiki_and_off_remains_off(client, tmp_path, monkeypatch, selected, expected):
    vault = tmp_path / "vault"
    (vault / "Library").mkdir(parents=True)
    note = vault / "Library/note.md"
    note.write_text("# Evidence\nPreserve these words.\n")
    config = await wiki_api.configure(client, vault)
    manager = wiki_api.session_manager
    session = await manager.create_session(config["curator_agent_id"], "Autonomy", str(tmp_path), backend="codex")
    captured = []

    def create(run_config):
        entries = select_mcp_servers(run_config.mcp_servers, [], {}, session_id=session.id,
                                     user_id=session.user_id, wiki_task_id=run_config.wiki_task_id)
        wiki = next((entry for entry in entries if entry.key == "wiki"), None)
        captured.append((run_config, wiki))

        class Run(FakeRunBase):
            def set_idle_handler(self, handler):
                pass

            async def start(self, *args, **kwargs):
                pass

            async def stream(self):
                if expected is None:
                    assert wiki is None and run_config.wiki_task_id is None
                else:
                    assert wiki is not None
                    task = tasks.active[run_config.wiki_task_id]
                    assert task.mode == expected
                    headers = {"Authorization": f"Bearer {wiki.credential}"}
                    response = await client.get("/api/wiki/read", params={"doc": "Library/note.md"}, headers=headers)
                    assert response.status_code == 200, response.text
                    query_guide = await client.get("/api/wiki/guide", params={"module": "query"}, headers=headers)
                    assert query_guide.status_code == 200
                    writing_guide = await client.get("/api/wiki/guide", params={"module": "writing"}, headers=headers)
                    assert writing_guide.status_code == 403
                    response = await client.post("/api/wiki/maintain", headers=headers, json={
                        "idempotency_key": "autonomous-maintenance", "scope": "Preserve source; check the note", "paths": ["Library/note.md"],
                    })
                    assert response.status_code == (200 if expected == "auto" else 403), response.text
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
    receipt = await manager.start_message(session.id, "Use our knowledge to answer this question", **kwargs)
    completion = await receipt.wait()
    assert completion.outcome.status == "success", completion.outcome
    assert len(captured) == 1
    assert note.read_text() == "# Evidence\nPreserve these words.\n"
    run_config, wiki = captured[0]
    if wiki:
        assert run_config.wiki_task_id not in tasks.active
        expired = await client.get("/api/wiki/read", params={"doc": "Library/note.md"}, headers={"Authorization": f"Bearer {wiki.credential}"})
        assert expired.status_code == 403


async def test_native_job_navigation_avoids_embedding_large_payload_in_command(client, tmp_path):
    from server.wiki.jobs import WikiJobs
    vault = tmp_path / "vault"
    (vault / "Library").mkdir(parents=True)
    (vault / "Library/note.md").write_text("# Source\nOriginal material\n")
    await wiki_api.configure(client, vault)
    jobs = WikiJobs(None)
    original = jobs.maintain("large-scope", "", "scope " * 30000, ["Library/note.md"], None, [], "human")
    response = await client.get("/api/wiki/jobs", params={"view": "compact", "idempotency_key": "large-scope"}, headers=wiki_api.HEADERS)
    assert response.status_code == 200, response.text
    assert len(response.content) < 16000, "job navigation still carries the complete scope into shell argv"
    item = response.json()["items"][0]
    assert item["id"] == original["id"] and item["progress"]["total"] == 1
    assert "payload" not in item and "items" not in item
    assert jobs.get(original["id"])["payload"]["request"]["scope"] == "scope " * 30000
    missing = await client.get("/api/wiki/jobs", params={"view": "compact", "idempotency_key": "large"}, headers=wiki_api.HEADERS)
    assert missing.json()["items"] == []


async def test_native_full_jobs_capture_avoids_actual_e2big_and_preserves_evidence(client, tmp_path, monkeypatch):
    from server.config import settings
    from server.mcp_identity import mint, reset_current_scope, set_current_scope, verify
    from server.mcp_servers import wiki
    from server.wiki.jobs import WikiJobs
    from server.wiki.store import WikiError

    vault = tmp_path / "vault"
    (vault / "Library").mkdir(parents=True)
    (vault / "Library/note.md").write_text("# Source\nKeep this material.\n")
    config = await wiki_api.configure(client, vault)
    monkeypatch.setattr(settings, "default_working_dir", str(tmp_path / "workspace"))
    job = WikiJobs(None).maintain("capture-large", "", "scope " * 30000, ["Library/note.md"], None, [], "human")
    full = (await client.get(f"/api/wiki/jobs/{job['id']}", headers=wiki_api.HEADERS)).json()
    raw = json.dumps(full, ensure_ascii=False).encode()
    with pytest.raises(OSError) as oversized:
        subprocess.run([sys.executable, "-c", "", raw.decode()], check=True)
    assert oversized.value.errno == errno.E2BIG

    session = await wiki_api.session_manager.create_session(config["curator_agent_id"], "Capture", str(tmp_path), backend="codex")
    task_id = tasks.open(session, "auto")
    scope = verify(mint(session.id, wiki_task_id=task_id))
    context = set_current_scope(scope)
    monkeypatch.setattr(wiki, "call", lambda *args, **kwargs: full)
    try:
        # Large detail is captured automatically, rather than echoing its payload.
        captured = wiki.jobs(job_id=job["id"], view="full")
        assert len(json.dumps(captured).encode()) < 16000
        artifact = captured["artifact"]
        path = Path(artifact["path"])
        assert path.is_relative_to(tmp_path / "workspace")
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

    vault = tmp_path / "vault"
    vault.mkdir()
    config = await wiki_api.configure(client, vault)
    workspace = tmp_path / "workspace" if layout == "escaped-directory" else vault
    workspace.mkdir(exist_ok=True)
    monkeypatch.setattr(settings, "default_working_dir", str(workspace))
    outside = tmp_path / "outside"
    if layout == "escaped-directory":
        outside.mkdir()
        (workspace / "wiki-evidence").symlink_to(outside, target_is_directory=True)
    elif layout == "all-roots-in-vault":
        # Even the fallback state directory may be configured inside the vault.
        state = vault / "state"
        state.mkdir()
        (state / "config.json").write_bytes((tmp_path / "state/config.json").read_bytes())
        monkeypatch.setattr(settings, "wiki_dir", str(state))

    session = await wiki_api.session_manager.create_session(config["curator_agent_id"], "Confined evidence", str(tmp_path), backend="codex")
    task_id = tasks.open(session, "auto")
    scope = verify(mint(session.id, wiki_task_id=task_id))
    try:
        if layout == "workspace-is-vault":
            artifact = capture_jobs(scope, {"private": "job metadata"})
            path = Path(artifact["path"])
            assert path.is_relative_to(tmp_path / "state/tool-evidence")
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
