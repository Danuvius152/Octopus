"""Opt-in synthetic workload matrix; measurements are evidence, not latency SLAs."""

import asyncio
import itertools
import json
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path
from time import perf_counter

import pytest

from server.config import settings
from server.wiki import materials
from server.wiki.curation import WikiCuration
from server.wiki.history import WikiHistory
from server.wiki.jobs import WikiJobs
from server.wiki.maintenance_state import status
from server.wiki.registry import registry
from server.wiki.store import WikiStore, revision
from tests import wiki_api as wiki_api
from tests.wiki_job_runtime import active_worker

pytestmark = pytest.mark.wiki_perf
client = wiki_api.client


def require(condition):
    assert condition


def corpus(root, count):
    for number in range(count):
        folder = root / "Library" / ("Selected" if number % 10 == 0 else "Other")
        folder.mkdir(parents=True, exist_ok=True)
        special = "uniqueneedle 检索练习 alphaevidence" if number == 0 else "Ordinary evidence"
        alias = "needle-alias" if number == 0 else f"alias-{number}"
        (folder / f"note-{number:05}.md").write_text(
            f"---\naliases: [{alias}]\ntags: [group-{number % 10}]\n---\n"
            f"# Note {number}\n\nsharedmarker 知识记录。\n\n## Evidence\n{special}.\n"
            + "Measured conditions and limitations. " * 12
            + "\n\n## Limits\n" + ("omegalimit" if number == 0 else "Bounded conclusion") + ".\n"
        )
    return "Library/Selected/note-00000.md"


@contextmanager
def opened(root, state):
    store = WikiStore(root, state, str(uuid.uuid4()), create=True)
    try:
        yield store
    finally:
        store.close()


@pytest.mark.parametrize("count", [25, 100, 250, 2500])
def test_corpus_size_and_query_shapes(tmp_path, cost_probe, count):
    root = tmp_path / "vault"
    target = corpus(root, count)
    created = []

    def build():
        store = WikiStore(root, tmp_path / f"state-{len(created)}", str(uuid.uuid4()), create=True)
        created.append(store)
        return store

    def built(store):
        try:
            assert store.documents(limit=1)["total"] == count
        finally:
            store.close()

    cost_probe.measure("initial_index", build, built, roots=[root], dimensions={"documents": count}, repeat=3)
    previous = created[-1]
    cost_probe.measure("reopen_index", lambda: WikiStore(root, previous.state, previous.vault_id), built,
                       roots=[root], dimensions={"documents": count})
    store = WikiStore(root, previous.state, previous.vault_id)
    try:
        options = {"roots": [root], "dimensions": {"documents": count}}
        query_cases = [
            ("exact_path", target, {}, lambda row: row["total"] == 1 and row["items"][0]["path"] == target),
            ("rare_english", "uniqueneedle", {}, lambda row: row["total"] == 1),
            ("chinese", "检索练习", {}, lambda row: row["items"][0]["path"] == target),
            ("alias", "needle-alias", {}, lambda row: row["items"][0]["path"] == target),
            ("cross_section", "alphaevidence omegalimit", {}, lambda row: len(row["items"][0]["sections"]) == 2),
            ("broad", "sharedmarker", {}, lambda row: row["total"] == count),
            ("miss", "absentquasar", {}, lambda row: row["total"] == 0),
            ("filtered", "sharedmarker", {"directory": "Library/Selected"}, lambda row: row["total"] == (count + 9) // 10),
            ("deep_offset", "sharedmarker", {"offset": count // 2}, lambda row: row["total"] == count and bool(row["items"])),
            ("compact", "sharedmarker", {"view": "compact"}, lambda row: row["total"] == count),
        ]
        for name, query, kwargs, check in query_cases:
            def operation(query=query, kwargs=kwargs):
                return store.search(query, limit=10, **kwargs)
            require(check(operation()))
            cost_probe.measure("search_" + name, operation, lambda row, check=check: require(check(row)), **options)
        for name, kwargs in (("first", {}), ("deep", {"offset": count // 2}), ("compact", {"view": "compact"})):
            cost_probe.measure("catalog_" + name, lambda kwargs=kwargs: store.documents(limit=10, **kwargs),
                               lambda row: require(row["total"] == count and bool(row["items"])), **options)
        cost_probe.measure("synchronize_unchanged", store.synchronize, lambda _: None, **options)
        cost_probe.measure("refresh_unchanged", store.refresh, lambda _: None, **options)
        raw = (root / target).read_bytes()

        def find(content):
            with WikiHistory(store).locked():
                return materials.find(store, content, markdown_only=True)

        cost_probe.measure("material_hit", lambda: find(raw), lambda row: require(row["path"] == target), **options)
        cost_probe.measure("material_miss", lambda: find(b"# New unmatched material\nAbsent facts.\n"),
                           lambda row: require(row is None), **options)
    finally:
        store.close()


@pytest.mark.parametrize("count", [25, 100, 2500])
def test_incremental_changes(tmp_path, cost_probe, count, request):
    root = tmp_path / "vault"
    target = corpus(root, count)
    with opened(root, tmp_path / "state") as store:
        identity = store.read(target, length=1)["id"]
        options = {"roots": [root], "dimensions": {"documents": count}, "repeat": 14 if count == 100 else 3}
        if count == 100:
            configured = request.config.getoption('wiki_baseline_reference')
            options.update(baseline=True, reference=json.loads(Path(configured).read_text()) if configured else None)
        expected = {}

        def verify(_):
            with store.connect() as db:
                for path, digest in expected.items():
                    row = db.execute("SELECT revision FROM documents WHERE path=?", (path,)).fetchone()
                    assert (row[0] if row else None) == digest

        def modify(number):
            raw = f"# Modified {number}\nChanged evidence.\n".encode()
            (root / target).write_bytes(raw)
            expected.clear()
            expected[target] = revision(raw)

        cost_probe.measure("one_file_modify", store.synchronize, verify, prepare=modify, **options)
        paths = sorted(root.rglob("*.md"))[:min(50, count)]

        def burst(number):
            expected.clear()
            for index, path in enumerate(paths):
                raw = f"# Batch {number} / {index}\nUpdated conditions.\n".encode()
                path.write_bytes(raw)
                expected[str(path.relative_to(root))] = revision(raw)

        cost_probe.measure("burst_modify", store.synchronize, verify, prepare=burst,
                           **(options | {"dimensions": {"documents": count, "changed": len(paths)}}))
        current = [target]

        def rename(number):
            destination = "Library/renamed.md" if current[0] == target else target
            raw = (root / current[0]).read_bytes()
            (root / current[0]).rename(root / destination)
            expected.clear()
            expected[current[0]], expected[destination] = None, revision(raw)
            current[0] = destination

        def renamed(result):
            verify(result)
            with store.connect() as db:
                assert db.execute("SELECT path FROM documents WHERE id=?", (identity,)).fetchone()[0] == current[0]

        cost_probe.measure("one_file_rename", store.synchronize, renamed, prepare=rename, **options)
        added = root / "Library/added.md"

        def add(number):
            added.unlink(missing_ok=True)
            store.synchronize()
            raw = f"# Added {number}\nNew evidence.\n".encode()
            added.write_bytes(raw)
            expected.clear()
            expected["Library/added.md"] = revision(raw)

        cost_probe.measure("one_file_add", store.synchronize, verify, prepare=add, **options)

        def delete(number):
            added.write_text(f"# Added {number}\n")
            store.synchronize()
            added.unlink()
            expected.clear()
            expected["Library/added.md"] = None

        cost_probe.measure("one_file_delete", store.synchronize, verify, prepare=delete, **options)


@pytest.mark.parametrize("sections,body_size", [(100, 128), (1000, 128), (1000, 2048)])
def test_long_document_access(tmp_path, cost_probe, sections, body_size):
    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    body = ("甲😀é conditions and evidence. " * (body_size // 20 + 1))[:body_size]
    text = "# Long note\n\n" + "".join(f"## Section {number}\n\n{body}\n\n" for number in range(sections))
    for name in ("a", "b"):
        (root / f"Library/{name}.md").write_text(text + name)
    with opened(root, tmp_path / "state") as store:
        curation = WikiCuration(store)
        options = {"roots": [root], "dimensions": {"sections": sections, "body_characters": body_size,
                                                   "document_bytes": len((text + "a").encode())}, "repeat": 3}
        def operation():
            return curation.blocks("Library/a.md", limit=25)

        def check(row):
            assert row["blocks"] and row["revision"] == revision((text + "a").encode())
        cost_probe.measure("raw_first_page_cold_coordinates", operation, check,
                           prepare=lambda _: setattr(store, "_raw_coordinates", None), memory=True, **options)
        cost_probe.measure("raw_first_page_warm_coordinates", operation, check, **options)
        for length in (1, 16000):
            cost_probe.measure("read_slice", lambda length=length: store.read("Library/a.md", length=length),
                               lambda row, length=length: require(row["content"] == (text + "a")[:length]),
                               **(options | {"dimensions": options["dimensions"] | {"requested_characters": length}}))

        def pages(limit):
            parts, offset = [], 0
            while True:
                page = curation.blocks("Library/a.md", offset=offset, limit=limit)
                parts.extend(page["blocks"])
                if page["next_offset"] is None:
                    return parts
                assert page["next_offset"] > offset
                offset = page["next_offset"]

        for limit in (25, 100):
            cost_probe.measure("raw_all_pages", lambda limit=limit: pages(limit),
                               lambda parts: require("".join(part["content"] for part in parts) == text + "a"),
                               **(options | {"dimensions": options["dimensions"] | {"page_blocks": limit}}))
        for alternating in (False, True):
            def reads(alternating=alternating):
                return [curation.blocks(f"Library/{'b' if alternating and index % 2 else 'a'}.md", offset=index, limit=1)
                        for index in range(10)]

            cost_probe.measure("raw_interleaved_documents", reads,
                               lambda rows: require(len(rows) == 10 and all(row["blocks"] for row in rows)),
                               **(options | {"dimensions": options["dimensions"] | {"alternating": alternating}}))


@pytest.mark.parametrize("suffix", [".md", ".txt", ".html"])
@pytest.mark.parametrize("kilochars", [16, 256])
def test_format_and_text_size(tmp_path, cost_probe, suffix, kilochars):
    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    body = ("uniqueneedle 中文😀 conditions. " * (kilochars * 1024 // 20))[:kilochars * 1024]
    text = f"<html><body><h1>Evidence</h1><p>{body}</p></body></html>" if suffix == ".html" else "# Evidence\n\n" + body
    path = "Library/note" + suffix
    (root / path).write_text(text)
    with opened(root, tmp_path / "state") as store:
        expected = store.read(path, length=24000)["content"]
        options = {"roots": [root], "dimensions": {"suffix": suffix, "body_kilochars": kilochars}, "repeat": 3}
        for length in (100, 24000):
            cost_probe.measure("format_read", lambda length=length: store.read(path, length=length),
                               lambda row, length=length: require(row["content"] == expected[:length]),
                               **(options | {"dimensions": options["dimensions"] | {"requested_characters": length}}))
        # Ordinary search deliberately admits Markdown only. HTML/text keep
        # explicit reading and change-detection costs without becoming hits.
        cost_probe.measure("format_search", lambda: store.search("uniqueneedle", view="compact"),
                           lambda row: require(row["items"][0]["path"] == path if suffix == ".md" else not row["items"]), **options)

        def edit(number):
            changed = text.replace("uniqueneedle", "uniqueneedle " + str(number), 1)
            (root / path).write_text(changed)

        cost_probe.measure("format_changed_read", lambda: store.read(path, length=100),
                           lambda row: require("uniqueneedle" in row["content"]), prepare=edit, **options)


@pytest.mark.parametrize("count", [25, 100, 250, 1000])
def test_job_and_projection_scaling(tmp_path, monkeypatch, cost_probe, count):
    root = tmp_path / "vault"
    corpus(root, count + 1)
    monkeypatch.setattr(settings, "wiki_dir", str(tmp_path / "state"))
    registry.configure(None, root, "curator")
    jobs = WikiJobs(None)
    with jobs.store.connect() as db:
        docs = [dict(row) for row in db.execute("SELECT id,path,revision FROM documents ORDER BY path")]
    items = [{"doc": doc["id"], "path": doc["path"], "revision": doc["revision"], "status": "pending"} for doc in docs[:count]]
    sequence, admitted = itertools.count(), []
    options = {"roots": [root], "dimensions": {"job_members": count}, "repeat": 3}

    def enqueue():
        job = jobs.enqueue(f"matrix-{next(sequence)}", {"kind": "curation"}, items)
        admitted.append(job["id"])
        return job

    cost_probe.measure("job_admission", enqueue, lambda job: require(len(job["items"]) == count), **options)
    job_id = admitted[0]
    jobs.update(job_id, status="running")
    def observe():
        return status(jobs.store, jobs.registration, None, limit=20)

    def check(row):
        assert row["ready"] and row["total"] == count + 1

    cost_probe.measure("maintenance_initial_projection", observe, check, diagnose=False, **(options | {"repeat": 1}))
    cost_probe.measure("maintenance_warm_page", observe, check, **options)
    cost_probe.measure("maintenance_one_document", lambda: status(jobs.store, jobs.registration, None, doc=docs[0]["id"]),
                       lambda row: require(row["ready"] and len(row["items"]) == 1), **options)
    cost_probe.measure("job_get", lambda: jobs.get(job_id), lambda row: require(len(row["items"]) == count), **options)
    for view in ("compact", "full"):
        cost_probe.measure("job_list_" + view, lambda view=view: jobs.list(view=view),
                           lambda row: require(bool(row["items"])), **options)
    cost_probe.measure("job_unchanged_update", lambda: jobs.update(job_id, status="running"),
                       lambda row: require(row["status"] == "running"), **options)
    current = {}

    def progress(number):
        current["items"] = jobs.get(job_id)["items"]
        current["items"][number]["status"] = "done"

    cost_probe.measure("job_one_item_progress", lambda: jobs.update(job_id, items=current["items"]),
                       lambda row: require(row["progress"]["completed"] > 0), prepare=progress, **options)

    def dirty(number):
        updated = jobs.get(job_id)["items"]
        updated[-1]["status"] = "done" if number % 2 == 0 else "pending"
        jobs.update(job_id, items=updated)

    cost_probe.measure("maintenance_after_one_item_progress", observe, check, prepare=dirty, **options)


@pytest.mark.parametrize("links", [10, 100, 500])
def test_graph_and_history(tmp_path, cost_probe, links):
    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    hub = "# Hub\n\n" + "".join(f"[Note {number}](note-{number}.md)\n" for number in range(links))
    (root / "Library/hub.md").write_text(hub)
    for number in range(links):
        (root / f"Library/note-{number}.md").write_text(f"# Note {number}\n\n[Hub](hub.md)\n")
    with opened(root, tmp_path / "state") as store:
        options = {"roots": [root], "dimensions": {"links": links}}
        for path in ("Library/hub.md", "Library/note-0.md"):
            cost_probe.measure("graph_page", lambda path=path: store.graph(path, limit=20),
                               lambda row: require(bool(row["edges"])),
                               **(options | {"dimensions": {"links": links, "hub": path.endswith("hub.md")}}))
        history = WikiHistory(store)
        raw = b"# Snapshot\nStable retained evidence.\n"
        for number in range(links):
            history.record("Library/note-0.md", raw, raw, "human", f"Synthetic history {number}", "applied")
        for offset in (0, max(0, links - 10)):
            cost_probe.measure("history_page", lambda offset=offset: history.history("Library/note-0.md", offset=offset, limit=10),
                               lambda row: require(len(row["items"]) == 10),
                               **(options | {"dimensions": {"history_records": links, "offset": offset}}))
        cost_probe.measure("read_with_history", lambda: store.read("Library/note-0.md", length=10),
                           lambda row: require(row["content"].startswith("# Note")), **options)


@pytest.mark.parametrize("accounts", [4, 40])
def test_registered_vault_store_retention(tmp_path, monkeypatch, cost_probe, accounts):
    monkeypatch.setattr(settings, "wiki_dir", str(tmp_path / "state"))
    owners = [str(uuid.uuid4()) for _ in range(accounts)]
    roots, stores = [], []
    for number, owner in enumerate(owners):
        root = tmp_path / f"vault-{number}"
        root.mkdir()
        (root / "note.md").write_text(f"# Account {number}\nowned evidence\n")
        registry.configure(owner, root, "curator")
        roots.append(root)
        stores.append(registry.store(owner))

    def cycle():
        return [registry.store(owner).read("note.md", length=30)["content"] for owner in owners]

    cycle()
    cost_probe.measure("account_round_robin", cycle,
                       lambda rows: require(all(row.startswith(f"# Account {number}\n") for number, row in enumerate(rows))),
                       roots=roots, dimensions={"active_vaults": accounts, "retention": "one_store_per_registered_vault"}, repeat=3)
    assert len(registry._stores) == accounts
    assert all(registry.store(owner) is store for owner, store in zip(owners, stores, strict=True))
    assert all(not store._retired and not store._events_released for store in stores)


async def test_http_views_worker_reads_and_concurrency(client, tmp_path, cost_probe):
    root = tmp_path / "vault"
    target = corpus(root, 250)
    text = "# Long\n\n" + "".join(f"## Section {index}\n\nCurrent evidence and conditions.\n\n" for index in range(1000))
    (root / "Library/long.md").write_text(text)
    config = await wiki_api.configure(client, root)
    options = {"roots": [root], "dimensions": {"documents": 251}}

    def ok(response):
        assert response.status_code == 200, response.text
        assert response.json()

    for endpoint, params in (
        ("search", {"query": "sharedmarker", "limit": 10}),
        ("read", {"doc": "Library/long.md", "length": 100}),
        ("blocks", {"doc": "Library/long.md", "limit": 25}),
    ):
        for view in (("full", "locators") if endpoint == "blocks" else ("full", "compact")):
            await cost_probe.measure_async(f"http_{endpoint}_{view}",
                lambda endpoint=endpoint, params=params, view=view: client.get(f"/api/wiki/{endpoint}", params=params | {"view": view}),
                ok, **options)
    for count in (1, 10, 20):
        paths = sorted(root.rglob("note-*.md"))[:count]
        payload = {"view": "compact", "items": [{"doc": str(path.relative_to(root)), "length": 100} for path in paths]}
        await cost_probe.measure_async("http_read_many", lambda payload=payload: client.post("/api/wiki/read-many", json=payload),
                                      ok, **(options | {"dimensions": {"batch_documents": count}}))
    async with active_worker(config, ["Library/long.md"], client=client) as worker:
        headers = worker.headers
        await cost_probe.measure_async("http_worker_read", lambda: client.get("/api/wiki/read", headers=headers,
                                      params={"doc": "Library/long.md", "length": 100, "view": "compact"}), ok, **options)

    for parallel in (1, 4, 8):
        async def wave(parallel=parallel):
            async def request():
                began = perf_counter()
                response = await client.get("/api/wiki/read", params={"doc": target, "length": 100, "view": "compact"})
                return {"wall_ms": (perf_counter() - began) * 1000, "status": response.status_code,
                        "content": response.json().get("content")}
            return await asyncio.gather(*(request() for _ in range(parallel)))

        samples = []

        def checked(rows, parallel=parallel, samples=samples):
            assert len(rows) == parallel and all(row["status"] == 200 and row["content"] for row in rows)
            samples.append(rows)

        row = await cost_probe.measure_async("http_same_vault_concurrency", wave, checked, repeat=3, diagnose=False,
                                            dimensions={"concurrency": parallel, "requests_per_wave": parallel})
        row["request_samples"] = samples
        cost_probe.save()


async def test_same_and_separate_vault_threads(tmp_path, cost_probe):
    with ExitStack() as resources:
        stores = []
        for number in range(4):
            root = tmp_path / f"vault-{number}"
            target = corpus(root, 25)
            stores.append(resources.enter_context(opened(root, tmp_path / f"state-{number}")))
        for shared in (True, False):
            async def wave(shared=shared):
                return await asyncio.gather(*(asyncio.to_thread(stores[0 if shared else index].read, target, length=100)
                                              for index in range(4)))

            await cost_probe.measure_async("parallel_store_reads", wave,
                lambda rows: require(len(rows) == 4 and all(row["content"] for row in rows)),
                dimensions={"concurrency": 4, "shared_vault": shared}, repeat=3, diagnose=False)
