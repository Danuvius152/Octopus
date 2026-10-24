"""Content decisions, evidence gates, aliases and current link destinations."""

import json
from types import SimpleNamespace

import pytest

from server.mcp_identity import mint
from server.wiki.jobs import WikiJobs
from server.wiki.registry import registry
from server.wiki.sections import original_text
from server.wiki.tasks import tasks
from server.wiki.workflow import BOOTSTRAP, guide, instructions, load_guide, view
from tests import test_wiki_api as wiki_api

client = wiki_api.client


@pytest.fixture
async def notes(client, tmp_path):
    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    (root / "Library/note.md").write_text("# Note\n\nOriginal claims.\n")
    (root / "Library/source.md").write_text("# Source\n\n## Evidence\nA related claim. ^claim\n\n## Limits\nThe conditions differ.\n")
    config = await wiki_api.configure(client, root)
    return root, config


async def read(client, path, **params):
    response = await client.get("/api/wiki/read", params={"doc": path, **params}, headers=client.headers)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("summary", ["", "待补充。", "判断仅在条件 A 下成立，理由来自试验。"])
async def test_supplied_points_survive_existing_summary_heading(client, notes, summary):
    root, _ = notes
    original = f"# Note\n\n## 摘要\n{summary}\n\n## 正文\n原来的观点与条件。\n"
    (root / "Library/note.md").write_text(original)
    current = await read(client, "Library/note.md")
    result = await client.post("/api/wiki/organize", headers=client.headers, json={
        "path": current["path"], "expected_revision": current["revision"],
        "key_note": "必要补充，不能忽略条件。", "reason": "supplement",
    })
    assert result.status_code == 200, result.text
    text = (root / "Library/note.md").read_text()
    assert "必要补充，不能忽略条件。" in text
    with registry.store((await wiki_api.session_manager.db.get_system_agent())["user_id"]).connect() as db:
        regions = json.loads(db.execute("SELECT regions FROM managed_notes WHERE path=?", (current["path"],)).fetchone()[0])
    assert original_text(text, regions) == original


@pytest.mark.parametrize("key_note", [None, ""])
async def test_existing_points_require_explicit_reuse_decision(client, notes, key_note):
    root, _ = notes
    original = "# Note\n\n## 要点\n观点仅在条件 A 下成立。\n\n正文保留。\n"
    (root / "Library/note.md").write_text(original)
    current = await read(client, "Library/note.md")
    body = {"path": current["path"], "expected_revision": current["revision"], "key_note": key_note, "reason": "reuse"}
    rejected = await client.post("/api/wiki/organize", headers=client.headers, json=body)
    assert rejected.status_code == 422
    body["reuse_existing_summary"] = True
    reused = await client.post("/api/wiki/organize", headers=client.headers, json=body)
    assert reused.status_code == 200, reused.text
    assert (root / "Library/note.md").read_text() == original
    assert (await read(client, current["path"]))["metadata"]["opening_points_decision"] == "reuse"


async def test_native_and_curator_aliases_work_in_search_metadata_and_links(client, notes):
    root, _ = notes
    native = "---\naliases: [原有名称]\n---\n# Source\n\nA related claim.\n"
    (root / "Library/source.md").write_text(native)
    source = await read(client, "Library/source.md")
    added = await client.post("/api/wiki/organize", headers=client.headers, json={
        "path": source["path"], "expected_revision": source["revision"], "key_note": "来源说明。",
        "aliases": ["Reliable Name", "可靠名称"], "reason": "aliases",
    })
    assert added.status_code == 200, added.text
    for alias in ["原有名称", "Reliable Name", "可靠名称"]:
        found = await client.get("/api/wiki/search", params={"query": alias}, headers=client.headers)
        assert found.json()["items"][0]["path"] == source["path"]
    current = await read(client, source["path"])
    assert set(current["metadata"]["aliases"]) == {"原有名称", "Reliable Name", "可靠名称"}
    assert (root / "Library/source.md").read_text().startswith("---\naliases: [原有名称]\n---")
    target = await read(client, "Library/note.md")
    linked = await client.post("/api/wiki/organize", headers=client.headers, json={
        "path": target["path"], "expected_revision": target["revision"], "key_note": "参见 [[可靠名称]]。", "reason": "alias link",
    })
    assert linked.status_code == 200, linked.text
    graph = (await client.get("/api/wiki/graph", params={"doc": target["id"]}, headers=client.headers)).json()
    assert any(edge["to"] == source["id"] and edge["relation"] == "wikilink" for edge in graph["edges"])


@pytest.mark.parametrize("link,accepted", [
    ("[[Library/source#Evidence]]", True), ("[[Library/source#^claim]]", True),
    ("[[Library/source#absent]]", False), ("[[Library/source#^absent]]", False),
    ("[[Library/missing/source]]", False), ("[[#Key note]]", True),
    ("[[Library/note#Key note]]", True), ("[self](#absent)", False),
])
async def test_new_link_paths_and_anchors_are_checked_against_current_candidate(client, notes, link, accepted):
    root, _ = notes
    original = (root / "Library/note.md").read_text() + "\nKeep [[old-missing]].\n"
    (root / "Library/note.md").write_text(original)
    current = await read(client, "Library/note.md")
    result = await client.post("/api/wiki/organize", headers=client.headers, json={
        "path": current["path"], "expected_revision": current["revision"], "key_note": f"See {link}.", "reason": "anchor",
    })
    assert result.status_code == (200 if accepted else 422), result.text
    assert "Keep [[old-missing]]." in (root / "Library/note.md").read_text()
    if not accepted:
        assert (root / "Library/note.md").read_text() == original


async def test_relation_requires_current_cited_read_and_keeps_old_note_read_only(client, notes):
    root, config = notes
    target = await read(client, "Library/note.md")
    source = await read(client, "Library/source.md")
    before = (root / "Library/source.md").read_bytes()
    jobs = WikiJobs((await wiki_api.session_manager.db.get_system_agent())["user_id"])
    job = jobs.maintain("related", "", "organize", [target["path"]], None, [], "human")
    session = SimpleNamespace(id="verified-worker", user_id=(await wiki_api.session_manager.db.get_system_agent())["user_id"], agent_id=config["curator_agent_id"])
    jobs.update(job["id"], status="running", session_id=session.id)
    tasks.assign(session.id, job["id"], job["items"])
    token = tasks.open(session, "maintain")
    headers = {"Authorization": f"Bearer {mint(session.id, user_id=session.user_id, wiki_task_id=token)}"}
    body = {"path": target["path"], "expected_revision": target["revision"], "key_note": "保留原论点。", "reason": "strong relation", "relations": [{"doc": source["id"], "revision": source["revision"], "location": "L3-L4", "explanation": "同一具体论点，适用条件在来源中说明。"}]}
    try:
        await client.get("/api/wiki/read", params={"doc": target["id"]}, headers=headers)
        unread = await client.post("/api/wiki/organize", headers=headers, json=body)
        assert unread.status_code == 422 and "cited current passage" in unread.text
        await client.get("/api/wiki/read", params={"doc": source["id"], "length": 1}, headers=headers)
        unread = await client.post("/api/wiki/organize", headers=headers, json=body)
        assert unread.status_code == 422
        body["relations"][0]["location"] = "L9999-L10000"
        invalid = await client.post("/api/wiki/organize", headers=headers, json=body)
        assert invalid.status_code == 422 and "existing line" in invalid.text
        body["relations"][0]["location"] = "L3-L4"
        start = source["content"].index("## Evidence")
        end = source["content"].index("\n## Limits")
        await client.get("/api/wiki/read", params={"doc": source["id"], "start": start, "length": end - start}, headers=headers)
        linked = await client.post("/api/wiki/organize", headers=headers, json=body)
        assert linked.status_code == 200, linked.text
        assert "[[Library/source.md]]" in (root / "Library/note.md").read_text()
        assert (root / "Library/source.md").read_bytes() == before
    finally:
        tasks.close(token)
        tasks.unassign(session.id)


@pytest.mark.parametrize("text,identifier,expected", [
    ("# Evidence\n\nOnly under condition A.\nConclusion follows. ^claim\n\nOther paragraph.\n", "^claim", "Only under condition A.\nConclusion follows. ^claim\n\n"),
    ("# Evidence\n\nOnly under condition A.\nConclusion follows.\n\n^claim\n\nOther paragraph.\n", "^claim", "Only under condition A.\nConclusion follows.\n\n^claim\n\n"),
    ("# Evidence\n\n- Necessary condition A.\n\n- Limited conclusion.\n\n^claim\n", "^claim", "- Necessary condition A.\n\n- Limited conclusion.\n\n^claim\n"),
    ("# Evidence\n\n- Necessary condition A.\n\n  Limited conclusion.\n\n^claim\n", "^claim", "- Necessary condition A.\n\n  Limited conclusion.\n\n^claim\n"),
    ("# Evidence\n\n1. Necessary condition A.\n\n   Supporting argument.\n\n2. Conditional conclusion.\n\n   Remaining limitation.\n\n^claim\n", "^claim", "1. Necessary condition A.\n\n   Supporting argument.\n\n2. Conditional conclusion.\n\n   Remaining limitation.\n\n^claim\n"),
    ("# Evidence\n\nUnrelated paragraph.\n\n- Necessary condition A.\n\n  - Nested condition B.\n\n    Limited conclusion.\n\n^claim\n", "^claim", "- Necessary condition A.\n\n  - Nested condition B.\n\n    Limited conclusion.\n\n^claim\n"),
    ("# Evidence\n\n- Necessary condition A.\n\n  Conditional conclusion.\nThis is a lazy continuation.\n\n^claim\n", "^claim", "- Necessary condition A.\n\n  Conditional conclusion.\nThis is a lazy continuation.\n\n^claim\n"),
])
def test_block_locator_covers_multiline_paragraph_and_complete_list(text, identifier, expected):
    from server.wiki.links import source_span

    start, end = source_span(text, identifier)
    assert text[start:end] == expected


async def test_guides_follow_verified_job_role_instead_of_agent_name(client, notes):
    _, config = notes
    session = SimpleNamespace(id="guide-worker", user_id=(await wiki_api.session_manager.db.get_system_agent())["user_id"], agent_id=config["curator_agent_id"])
    entry_token = tasks.open(session, "maintain")
    try:
        entry = instructions("maintain", entry_token)
        assert entry == BOOTSTRAP and guide("entry") not in entry
        assert load_guide("entry", entry_token) == guide("entry")
        assert "worker" not in view(tasks.active[entry_token]).guides
    finally:
        tasks.close(entry_token)
    jobs = WikiJobs((await wiki_api.session_manager.db.get_system_agent())["user_id"])
    job = jobs.maintain("guide", "", "organize", ["Library/note.md"], None, [], "human")
    jobs.update(job["id"], status="running", session_id=session.id)
    tasks.assign(session.id, job["id"], job["items"])
    token = tasks.open(session, "maintain")
    try:
        worker = instructions("maintain", token)
        assert worker == BOOTSTRAP and guide("worker-v2") not in worker
        assert guide("worker-v2") in load_guide("worker", token)
        assert load_guide("writing", token) == guide("writing")
        assert load_guide("maintenance", token) == guide("maintenance-v2")
        assert guide("entry") not in worker
        modules = {"query", "retrieval", "entry", "worker", "maintenance", "article", "writing"}
        card = view(tasks.active[token]).card
        assert card["injected_guides"] == []
        assert "guides" not in card and "capabilities" not in card
        headers = {"Authorization": f"Bearer {mint(session.id, user_id=session.user_id, wiki_task_id=token)}"}
        for module in modules:
            response = await client.get("/api/wiki/guide", params={"module": module}, headers=headers)
            assert response.status_code == 200, response.text
            assert response.json()["content"] == load_guide(module, token)

        # Model and REST schemas must advertise the same actual module contract.
        from server.main import app
        from server.mcp_identity import reset_current_scope, set_current_scope, verify
        from server.mcp_servers.wiki import mcp

        scope = verify(headers["Authorization"].split(" ", 1)[1])
        context = set_current_scope(scope)
        try:
            tools = await mcp.list_tools()
        finally:
            reset_current_scope(context)
        schema = next(tool for tool in tools if tool.name == "load_guide").inputSchema
        assert set(schema["properties"]["module"]["enum"]) == modules
        parameters = app.openapi()["paths"]["/api/wiki/guide"]["get"]["parameters"]
        assert set(next(parameter for parameter in parameters if parameter["name"] == "module")["schema"]["enum"]) == modules
        assert (await client.get("/api/wiki/guide", params={"module": "missing"}, headers=headers)).status_code == 422

        jobs.update(job["id"], status="cancelled")
        ended = instructions("maintain", token)
        assert ended == BOOTSTRAP
        assert view(tasks.active[token]).card["actor_role"] == "ended_worker"
        for module in ("worker", "maintenance", "article", "writing"):
            denied = await client.get("/api/wiki/guide", params={"module": module}, headers=headers)
            assert denied.status_code == 403, denied.text
        assert (await client.get("/api/wiki/guide", params={"module": "query"}, headers=headers)).status_code == 200
    finally:
        tasks.close(token)
        tasks.unassign(session.id)
    assert (await client.get("/api/wiki/guide", params={"module": "query"}, headers=headers)).status_code == 403


async def test_graph_reads_only_selected_current_neighbors(client, notes, monkeypatch):
    root, _ = notes
    for number in range(20):
        (root / f"Library/unrelated-{number}.md").write_text(f"# Unrelated {number}\nOther evidence.\n")
    current = await read(client, "Library/note.md")
    store = registry.store((await wiki_api.session_manager.db.get_system_agent())["user_id"])
    store.refresh()
    from pathlib import Path

    original = Path.read_bytes
    opened = []

    def watch(path):
        if path.is_relative_to(root):
            opened.append(path.relative_to(root).as_posix())
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", watch)
    response = await client.get("/api/wiki/graph", params={"doc": current["id"]}, headers=client.headers)
    assert response.status_code == 200, response.text
    assert not any("unrelated-" in path for path in opened)
