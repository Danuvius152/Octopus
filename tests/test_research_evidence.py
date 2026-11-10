"""Evidence delivered through the original pipeline, storage and public consumers."""

import asyncio
import json
from pathlib import Path

import pytest

from server.research.leaf import LeafResult
from server.research.orchestrator import ResearchLimits, run_research
from tests.native_app_server import native_runtime as native_runtime

SAMPLES = Path(__file__).parents[1] / "web/src/lib/researchEvidenceFixtures.json"


def matches_browser_sample(request, key, evidence):
    samples = json.loads(SAMPLES.read_text()) if SAMPLES.exists() else {}
    if request.config.getoption("--update-ws-fixtures"):
        samples[key] = evidence
        SAMPLES.write_text(json.dumps(samples, ensure_ascii=False, indent=2) + "\n")
    assert evidence == samples[key]


@pytest.fixture
async def evidence_runtime(native_runtime, monkeypatch):
    from server.research import orchestrator
    from server.research.manager import ResearchManager

    manager, database, parent, _, _, events = native_runtime
    await manager.interrupt(parent.id)
    research = ResearchManager()
    research.bind(manager, database)

    async def reason(*args, prompt, **kwargs):
        return LeafResult(text='["safety", "cost"]' if "Decompose" in prompt else "# Gap report")

    async def search(*args, **kwargs):
        return LeafResult(text="", error="controlled provider refusal")

    monkeypatch.setattr(orchestrator, "run_web_leaf", search)
    monkeypatch.setattr(orchestrator, "run_reason_leaf", reason)
    try:
        yield manager, database, parent, research, events
    finally:
        await research.shutdown()


async def test_original_manager_commits_summary_and_reloads_it_with_the_same_job(evidence_runtime, request):
    manager, database, parent, research, events = evidence_runtime
    job = await research.start(parent.id, "Assess safety")
    await asyncio.wait_for(asyncio.shield(research._tasks[job["id"]]), 3)
    saved = await database.get_research_job(job["id"])
    assert saved["status"] == "completed"
    projection = await research.public_job(saved)
    assert projection["evidence_state"] == "available"
    assert [s["status"] for s in projection["evidence"]["searches"]] == ["failed", "failed"]
    completed = next(event for event in events if event["type"] == "research_completed")
    assert completed["evidence"] == projection["evidence"]
    matches_browser_sample(request, "all_failed", projection["evidence"])
    assert "All searches failed" in Path(saved["report_path"]).read_text()
    # Admission is real and remains separate from evidence quality.
    assert saved["delivery_receipt"]["stage"] == "queued"
    assert "All searches failed" in manager.dispatch_snapshot(parent.id)["items"][0]["summary"]
    from server.research.manager import ResearchManager
    restarted = ResearchManager()
    restarted.bind(manager, database)
    try:
        assert (await restarted.public_job(saved))["evidence"] == projection["evidence"]
    finally:
        await restarted.shutdown()


@pytest.mark.parametrize("material", ["report", "evidence"])
async def test_save_refusal_never_publishes_success(evidence_runtime, monkeypatch, material):
    import builtins
    manager, database, parent, research, events = evidence_runtime
    original_open = builtins.open

    def refused(path, mode="r", *args, **kwargs):
        selected = str(path).endswith(".md" if material == "report" else "evidence.json")
        if selected and "w" in mode:
            raise PermissionError("controlled disk refusal")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", refused)
    job = await research.start(parent.id, "Do not publish unsaved evidence")
    await asyncio.wait_for(asyncio.shield(research._tasks[job["id"]]), 3)
    row = await database.get_research_job(job["id"])
    assert row["status"] == "failed" and "could not both be saved" in row["error"]
    assert not any(event["type"] == "research_completed" for event in events)
    assert manager.dispatch_snapshot(parent.id)["items"] == []


@pytest.mark.parametrize("material", ["report", "evidence"])
async def test_directory_refusal_after_leaves_finish_settles_as_save_failure(evidence_runtime, monkeypatch, material):
    import os

    from server.research import orchestrator
    from server.workspace import paths_for

    manager, database, parent, research, events = evidence_runtime
    root = paths_for(parent.user_id).research
    original_makedirs = os.makedirs
    leaves_finished = False
    refusals = []

    async def reason(*args, prompt, **kwargs):
        nonlocal leaves_finished
        if "Decompose" in prompt:
            return LeafResult(text='["safety"]')
        leaves_finished = True
        return LeafResult(text="# Gap report")

    def refused(path, *args, **kwargs):
        if leaves_finished and Path(path).is_relative_to(root):
            if material == "report" or any(root.glob("*.md")):
                refusals.append(path)
                raise PermissionError("controlled save directory refusal")
        return original_makedirs(path, *args, **kwargs)

    monkeypatch.setattr(orchestrator, "run_reason_leaf", reason)
    monkeypatch.setattr(os, "makedirs", refused)
    job = await research.start(parent.id, "Do not leave a job running after directory refusal")
    await asyncio.wait_for(asyncio.shield(research._tasks[job["id"]]), 3)
    row = await database.get_research_job(job["id"])
    assert leaves_finished and refusals
    assert row["status"] == "failed" and "could not both be saved" in row["error"]
    assert not any(event["type"] == "research_completed" for event in events)
    assert manager.dispatch_snapshot(parent.id)["items"] == []


async def test_synthesis_failure_preserves_the_summary_in_the_original_failure_artifact(evidence_runtime, monkeypatch):
    from server.research import orchestrator
    from server.workspace import paths_for

    _, database, parent, research, events = evidence_runtime

    async def reason(*args, prompt, **kwargs):
        return LeafResult(text='["safety"]') if "Decompose" in prompt else LeafResult(text="", error="synthesis refused")

    monkeypatch.setattr(orchestrator, "run_reason_leaf", reason)
    job = await research.start(parent.id, "Failed synthesis")
    await asyncio.wait_for(asyncio.shield(research._tasks[job["id"]]), 3)
    row = await database.get_research_job(job["id"])
    assert row["status"] == "failed" and row["error"] == "Report synthesis failed: synthesis refused"
    partial = json.loads((paths_for(parent.user_id).research / job["id"] / "synthesis-failure.json").read_text())
    assert partial["evidence"]["searches"][0]["reason"] == "controlled provider refusal"
    assert not any(event["type"] == "research_completed" for event in events)


@pytest.mark.parametrize("stage", ["serialize", "mkdir", "open", "write", "flush", "file_fsync", "directory_open", "directory_fsync"])
async def test_synthesis_diagnostic_failure_does_not_block_original_outcome(evidence_runtime, monkeypatch, caplog, stage):
    import builtins
    import os
    import stat

    from server.research import orchestrator
    from server.workspace import paths_for

    manager, database, parent, research, events = evidence_runtime
    root = paths_for(parent.user_id).research
    original_open, original_makedirs = builtins.open, os.makedirs
    original_fsync, original_os_open = os.fsync, os.open
    original_encode = json.JSONEncoder.iterencode
    synthesis_failed = diagnostic_opened = False

    def refuse(*args, **kwargs):
        raise OSError(f"diagnostic {stage} refused; token=synthetic-material-secret\x00")

    async def reason(*args, prompt, **kwargs):
        nonlocal synthesis_failed
        if "Decompose" in prompt:
            return LeafResult(text='["safety"]')
        synthesis_failed = True
        return LeafResult(text="", error="original synthesis refusal")

    def encode(encoder, payload, *args, **kwargs):
        if stage == "serialize" and isinstance(payload, dict) and "evidence" in payload and "error" in payload:
            raise TypeError("diagnostic serialize refused; token=synthetic-material-secret\x00")
        return original_encode(encoder, payload, *args, **kwargs)

    def makedirs(path, *args, **kwargs):
        if stage == "mkdir" and synthesis_failed and Path(path).is_relative_to(root):
            refuse()
        return original_makedirs(path, *args, **kwargs)

    def refused(path, mode="r", *args, **kwargs):
        nonlocal diagnostic_opened
        if str(path).endswith("synthesis-failure.json") and "w" in mode:
            if stage == "open":
                refuse()
            handle = original_open(path, mode, *args, **kwargs)
            diagnostic_opened = True
            if stage in {"write", "flush"}:
                monkeypatch.setattr(handle, stage, refuse)
            return handle
        return original_open(path, mode, *args, **kwargs)

    def fsync(fd):
        selected = "directory_fsync" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file_fsync"
        if diagnostic_opened and stage == selected:
            refuse()
        return original_fsync(fd)

    def directory_open(path, flags, *args, **kwargs):
        if diagnostic_opened and stage == "directory_open" and flags & os.O_DIRECTORY:
            refuse()
        return original_os_open(path, flags, *args, **kwargs)

    committed = []

    async def observe(event):
        if event["type"] == "research_failed":
            committed.append(await database.get_research_job(event["research_id"]))

    manager.on_broadcast("synthesis-failure-commit", observe)
    monkeypatch.setattr(orchestrator, "run_reason_leaf", reason)
    monkeypatch.setattr(builtins, "open", refused)
    monkeypatch.setattr(json.JSONEncoder, "iterencode", encode)
    monkeypatch.setattr(os, "makedirs", makedirs)
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "open", directory_open)
    job = await research.start(parent.id, "Keep the original synthesis failure")
    await asyncio.wait_for(research.wait_for_idle(), 3)
    row = await database.get_research_job(job["id"])
    assert row["status"] == "failed" and "original synthesis refusal" in row["error"]
    assert "Synthesis diagnostic material unavailable:" in row["error"]
    assert f"diagnostic {stage} refused" in row["error"]
    assert "token=[redacted]" in row["error"] and "synthetic-material-secret" not in row["error"]
    assert "\x00" not in row["error"]
    assert row["completed_at"] and row["report_path"] is None
    assert row["injection_status"] == "pending" and row["delivery_receipt"] is None
    assert committed == [row]
    card = await research.public_job(row)
    assert card["status"] == "failed" and card["error"] == row["error"]
    assert card["evidence_state"] == "unavailable"
    failed = [event for event in events if event["type"] == "research_failed"]
    assert len(failed) == 1 and failed[0]["error"] == row["error"]
    assert not any(event["type"] == "research_completed" for event in events)
    assert manager.dispatch_snapshot(parent.id)["items"] == []
    assert not research.drain_blockers()
    assert f"diagnostic {stage} refused" in caplog.text


@pytest.mark.parametrize("winner", ["failed", "cancelled"])
@pytest.mark.parametrize("material_available", [False, True])
async def test_synthesis_failure_and_cancel_share_one_committed_outcome(evidence_runtime, monkeypatch, winner, material_available):
    import builtins

    from server.research import orchestrator

    _, database, parent, research, events = evidence_runtime
    synthesizing, release_synthesis, entered, release_commit, material_attempted = (asyncio.Event() for _ in range(5))
    original_open, execute = builtins.open, database.conn._execute

    async def reason(*args, prompt, **kwargs):
        if "Decompose" in prompt:
            return LeafResult(text='["safety"]')
        synthesizing.set()
        await release_synthesis.wait()
        return LeafResult(text="", error="original synthesis refusal")

    def open_material(path, mode="r", *args, **kwargs):
        if str(path).endswith("synthesis-failure.json") and "w" in mode:
            material_attempted.set()
            if not material_available:
                raise OSError("diagnostic material refused during cancellation race")
        return original_open(path, mode, *args, **kwargs)

    async def gated_execute(function, *args, **kwargs):
        result = await execute(function, *args, **kwargs)
        if (args and isinstance(args[0], str) and args[0].startswith("UPDATE research_jobs SET status=")
                and args[1][0] == winner and not entered.is_set()):
            entered.set()
            await release_commit.wait()
        return result

    monkeypatch.setattr(orchestrator, "run_reason_leaf", reason)
    monkeypatch.setattr(builtins, "open", open_material)
    monkeypatch.setattr(database.conn, "_execute", gated_execute)
    job = await research.start(parent.id, "Cancel versus original synthesis failure")
    cancelling = None
    try:
        await asyncio.wait_for(synthesizing.wait(), 3)
        if winner == "failed":
            release_synthesis.set()
            await asyncio.wait_for(entered.wait(), 3)
            cancelling = asyncio.create_task(research.cancel(job["id"]))
        else:
            cancelling = asyncio.create_task(research.cancel(job["id"]))
            await asyncio.wait_for(entered.wait(), 3)
            release_synthesis.set()
            await asyncio.wait_for(material_attempted.wait(), 3)
        release_commit.set()
        response = await asyncio.wait_for(cancelling, 3)
        await asyncio.wait_for(research.wait_for_idle(), 3)
        row = await database.get_research_job(job["id"])
        assert response == row and row["status"] == winner and row["completed_at"]
        assert material_attempted.is_set()
        if winner == "cancelled":
            assert row["error"] == "cancelled by user"
        else:
            assert row["error"].startswith("Report synthesis failed: original synthesis refusal")
            if material_available:
                assert row["error"] == "Report synthesis failed: original synthesis refusal"
            else:
                assert "Synthesis diagnostic material unavailable: OSError: diagnostic material refused during cancellation race" in row["error"]
        terminal = [event for event in events if event["type"] in {"research_failed", "research_completed"}]
        assert len(terminal) == 1 and terminal[0]["status"] == winner
        assert row["report_path"] is None and row["delivery_receipt"] is None
        assert (await research.public_job(row))["status"] == winner
        assert not research.drain_blockers()
    finally:
        release_synthesis.set()
        release_commit.set()
        if cancelling is not None:
            await asyncio.gather(cancelling, return_exceptions=True)


@pytest.mark.parametrize("damage", ["none", "missing", "invalid", "version", "unversioned", "missing_reason", "missing_conditions", "missing_sources", "empty_object", "oversized", "foreign", "symlink"])
async def test_http_snapshots_read_bounded_owned_material_and_mark_unavailable(evidence_runtime, monkeypatch, tmp_path, damage):
    from httpx import ASGITransport, AsyncClient

    from server import deps
    from server.main import app
    from server.research import orchestrator
    from server.routers import research as routes
    from server.workspace import paths_for
    from tests.accounts import create_account

    manager, database, parent, research, _ = evidence_runtime
    if damage in {"missing_conditions", "missing_sources"}:
        async def checked_search(*args, prompt, **kwargs):
            if "Fact-check" in prompt:
                return LeafResult(text='{"refuted": true, "note": "Fails under load", "conditions": "above 100 requests", "sources": ["https://counter.test/load"]}')
            return LeafResult(text='[{"claim":"Always reliable", "url":"https://claim.test"}]')
        monkeypatch.setattr(orchestrator, "run_web_leaf", checked_search)
    job = await research.start(parent.id, "Read original evidence")
    await asyncio.wait_for(asyncio.shield(research._tasks[job["id"]]), 3)
    summary = paths_for(parent.user_id).research / job["id"] / "evidence.json"
    payload = json.loads(summary.read_text())
    if damage == "missing":
        summary.unlink()
    elif damage == "invalid":
        summary.write_text("not JSON")
    elif damage == "version":
        payload["evidence"]["version"] = 999
        summary.write_text(json.dumps(payload))
    elif damage in {"unversioned", "missing_reason", "missing_conditions", "missing_sources", "empty_object"}:
        if damage == "unversioned":
            del payload["evidence"]["version"]
        elif damage == "missing_reason":
            del payload["evidence"]["searches"][0]["reason"]
        elif damage in {"missing_conditions", "missing_sources"}:
            del payload["evidence"]["claims"][0]["votes"][0][damage.removeprefix("missing_")]
        else:
            payload["evidence"] = {}
        summary.write_text(json.dumps(payload))
    elif damage == "oversized":
        summary.write_text(" " * (256 * 1024 + 1))
    elif damage == "foreign":
        payload["session_id"] = "another-session"
        summary.write_text(json.dumps(payload))
    elif damage == "symlink":
        summary.unlink()
        outside = tmp_path / "outside.json"
        outside.write_text(json.dumps(payload))
        summary.symlink_to(outside)
    token = await deps.get_user_manager().issue_token(parent.user_id)
    _, other = await create_account(database, username="another-evidence-owner")
    monkeypatch.setattr(deps, "_singleton", manager)
    monkeypatch.setattr(routes, "research_manager", research)
    url = f"/api/sessions/{parent.id}/research"
    async with AsyncClient(transport=ASGITransport(app), base_url="http://test", headers={"Authorization": f"Bearer {token}"}) as client:
        detail = (await client.get(url + "/" + job["id"])).json()
        listing = (await client.get(url)).json()
        assert detail["status"] == "completed" and "knowledge_context" not in detail
        assert detail["evidence_state"] == ("available" if damage == "none" else "unavailable")
        assert detail["evidence"] == (payload["evidence"] if damage == "none" else None)
        assert listing[0]["evidence"] == detail["evidence"]
        for target in [url, url + "/" + job["id"]]:
            assert (await client.get(target, headers={"Authorization": f"Bearer {other}"})).status_code == 404


async def test_session_deletion_retires_job_but_preserves_account_research_material(evidence_runtime):
    from server.workspace import paths_for

    manager, database, parent, research, _ = evidence_runtime
    job = await research.start(parent.id, "Retained account materials")
    await asyncio.wait_for(asyncio.shield(research._tasks[job["id"]]), 3)
    row = await database.get_research_job(job["id"])
    summary = paths_for(parent.user_id).research / job["id"] / "evidence.json"
    original = summary.read_bytes()
    await research.shutdown()
    assert await manager.delete_session(parent.id)
    assert await database.get_research_job(job["id"]) is None
    assert summary.read_bytes() == original and Path(row["report_path"]).is_file()


async def test_summary_bounds_external_text_and_marks_omissions(tmp_path):
    from server.research.evidence import MAX_EVIDENCE_BYTES

    async def reason(prompt):
        return LeafResult(text=json.dumps(["角" * 3000] * 5)) if "Decompose" in prompt else LeafResult(text="report")

    async def search(prompt):
        if "Fact-check" in prompt:
            return LeafResult(text=json.dumps({"refuted": False, "verified": True,
                "note": "理" * 10000, "conditions": "件" * 10000,
                "sources": ["https://example.test/" + "路" * 10000] * 5}))
        return LeafResult(text=json.dumps([{"claim": str(i) + "大" * 10000, "url": "https://example.test/" + "路" * 10000} for i in range(6)]))

    result = await run_research("bounded", working_dir=str(tmp_path), reason=reason, search=search)
    assert result.evidence.omitted
    assert "shortened or omitted" in result.report
    assert len(result.evidence.model_dump_json().encode()) < MAX_EVIDENCE_BYTES
    assert len(result.evidence.claims[0].claim.encode()) <= 512
    assert result.evidence.claims[0].votes[0].sources == []


async def test_long_urls_are_explicitly_missing_instead_of_becoming_new_sources(tmp_path):
    prompts = []
    original_url = "https://source.test/" + "long-path" * 80

    async def reason(prompt):
        if "Decompose" in prompt:
            return LeafResult(text='["angle"]')
        prompts.append(prompt)
        return LeafResult(text="Report")

    async def search(prompt):
        if "Fact-check" in prompt:
            return LeafResult(text=json.dumps({"refuted": True, "note": "Counterevidence", "sources": [original_url]}))
        return LeafResult(text=json.dumps([{"claim": "Original claim", "url": original_url}]))

    result = await run_research("q", working_dir=str(tmp_path), search=search, reason=reason)
    assert result.evidence.omitted and result.evidence.claims[0].url == ""
    assert result.evidence.claims[0].votes[0].sources == []
    assert result.sources == []
    assert "https://source.test/" not in prompts[0] and "https://source.test/" not in result.report
    assert "claim source: not provided" in result.report and "Check sources: not provided" in result.report


async def test_account_transfer_preserves_material_without_restoring_jobs(tmp_path, monkeypatch):
    from server.config import settings
    from server.database import Database
    from server.portability import export_account, import_account
    from server.research import orchestrator
    from server.research.manager import ResearchManager
    from server.sessions import SessionManager
    from server.workspace import paths_for
    from tests.accounts import create_account
    from tests.test_service_runtime import installation_settings

    home = tmp_path / "source"
    home.mkdir()
    working = home / "workspace"
    working.mkdir()
    config, _ = installation_settings(home)
    for name, value in config.model_dump().items():
        monkeypatch.setattr(settings, name, value)
    database = Database(config.db_path)
    await database.initialize()
    owner, _ = await create_account(database, extra_roots=[working])
    manager = SessionManager()
    await manager.initialize(database)
    research = ResearchManager()
    research.bind(manager, database)

    async def reason(*args, prompt, **kwargs):
        return LeafResult(text='["one"]' if "Decompose" in prompt else "Gap report")

    async def search(*args, **kwargs):
        return LeafResult(text="[]")

    monkeypatch.setattr(orchestrator, "run_web_leaf", search)
    monkeypatch.setattr(orchestrator, "run_reason_leaf", reason)
    try:
        agent = await database.get_system_agent(owner["id"])
        session = await manager.create_session(agent["id"], "Source", str(working), backend="codex")
        await manager.interrupt(session.id)
        job = await research.start(session.id, "Preserve evidence")
        await asyncio.wait_for(asyncio.shield(research._tasks[job["id"]]), 3)
        row = await database.get_research_job(job["id"])
        files = [Path(row["report_path"]), paths_for(owner["id"]).research / job["id"] / "evidence.json"]
        contents = [path.read_bytes() for path in files]
    finally:
        await research.shutdown()
        await manager.close_session_owners()
        await database.close()
    package = tmp_path / "account.zip"
    export_account(config, owner["id"], package, home=home)
    target = tmp_path / "restored"
    restored = import_account(package, home=target)
    assert [ (target / path.relative_to(home)).read_bytes() for path in files] == contents
    copied = Database(restored.db_path)
    await copied.initialize()
    try:
        assert await copied.load_sessions(include_archived=True) == []
        assert await copied.get_research_job(job["id"]) is None
        assert await copied.list_research_jobs_for_session(session.id) == []
    finally:
        await copied.close()


async def test_all_failed_searches_reach_synthesis_and_report_as_evidence_gaps(tmp_path):
    synthesis = []

    async def reason(prompt):
        if "Decompose" in prompt:
            return LeafResult(text='["safety", "cost"]')
        synthesis.append(prompt)
        return LeafResult(text="# Gap report\nFurther research is needed.")

    async def search(prompt):
        # A failed leaf's parseable partial output is not a successful finding.
        return LeafResult(text='[{"claim":"unsafe partial","url":"https://partial"}]', error="provider timed out")

    result = await run_research("Can it be recommended?", working_dir=str(tmp_path), search=search, reason=reason)
    assert "provider timed out" in synthesis[0]
    assert "safety" in synthesis[0] and "cost" in synthesis[0]
    assert "All searches failed" in result.report
    assert "provider timed out" in result.report
    assert "unsafe partial" not in synthesis[0]
    assert result.findings == []
    assert [angle.status for angle in result.evidence.searches] == ["failed", "failed"]


async def test_counterevidence_sources_conditions_and_failed_votes_survive_synthesis(tmp_path, request):
    synthesis = []
    votes = iter([
        LeafResult(text=json.dumps({"refuted": True, "verified": False, "note": "Fails under load",
                                  "conditions": "above 100 requests", "sources": ["https://counter.test/load"]})),
        LeafResult(text=json.dumps({"refuted": True, "note": "Independent replication", "sources": []})),
        LeafResult(text='{"refuted": false, "verified": true}', error="lost response"),
    ])

    async def reason(prompt):
        if "Decompose" in prompt:
            return LeafResult(text='["performance"]')
        synthesis.append(prompt)
        return LeafResult(text="# Report\nDo not rely on this claim.")

    async def search(prompt):
        if "Fact-check" in prompt:
            assert '"sources"' in prompt and '"conditions"' in prompt
            return next(votes)
        return LeafResult(text='[{"claim":"Always reliable","url":"https://vendor.test/claim"}]')

    result = await run_research("Reliability?", working_dir=str(tmp_path), search=search, reason=reason,
                                limits=ResearchLimits(votes_per_claim=3))
    assert "Always reliable" in synthesis[0] and "[refuted]" in synthesis[0]
    assert "Fails under load" in synthesis[0] and "above 100 requests" in synthesis[0]
    assert "https://counter.test/load" in synthesis[0]
    assert result.findings == []
    claim = result.evidence.claims[0]
    assert (claim.refute, claim.failed, claim.support) == (2, 1, 0)
    assert claim.votes[2].status == "failed" and claim.votes[2].sources == []
    assert claim.votes[1].sources == []  # Never borrow the original claim URL.
    assert "Check sources: not provided" in result.report
    assert "https://counter.test/load" in result.sources
    matches_browser_sample(request, "refuted", result.evidence.model_dump())


async def test_empty_searches_parse_failures_and_partial_failures_are_distinct(tmp_path):
    async def reason(prompt):
        return LeafResult(text='["empty", "broken", "usable"]' if "Decompose" in prompt else "report")

    async def search(prompt):
        if "Fact-check" in prompt:
            return LeafResult(text='{"refuted": false, "verified": true, "note":"Measured", "sources":["https://measurement.test"]}')
        if "ANGLE: empty" in prompt:
            return LeafResult(text="[]")
        if "ANGLE: broken" in prompt:
            return LeafResult(text="not JSON")
        return LeafResult(text='[{"claim":"Measured claim", "url":"https://claim.test"}]')

    result = await run_research("q", working_dir=str(tmp_path), reason=reason, search=search)
    assert [s.status for s in result.evidence.searches] == ["empty", "failed", "findings"]
    assert "1/3 searches failed" in result.report and "All searches failed" not in result.report
    assert result.findings[0].verification == "verified"
    assert "Invalid search output" in result.report


@pytest.mark.parametrize("text", ["[]", '[{"broken":true}]', "garbage"])
async def test_no_material_never_becomes_positive_evidence(tmp_path, text):
    async def reason(prompt):
        return LeafResult(text='["one"]' if "Decompose" in prompt else "Gap report")

    async def search(prompt):
        return LeafResult(text=text)

    result = await run_research("q", working_dir=str(tmp_path), reason=reason, search=search)
    assert result.evidence.searches[0].status == ("empty" if text == "[]" else "failed")
    assert "Insufficient evidence" in result.report
