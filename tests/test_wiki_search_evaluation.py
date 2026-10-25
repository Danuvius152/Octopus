"""The standalone evaluator measures a fresh index and releases private state."""

import importlib.util
import json
import stat
import sys
import uuid
from pathlib import Path

import pytest

from server.wiki.store import WikiStore

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/evaluate-wiki-search.py"


@pytest.fixture
def evaluation():
    spec = importlib.util.spec_from_file_location("wiki_search_evaluation", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_evaluation_build_covers_initialization_and_initial_index(evaluation, tmp_path, monkeypatch):
    clock, stores = [0.0], []

    class TimedStore(WikiStore):
        def __init__(self, *args, **kwargs):
            clock[0] += 1
            super().__init__(*args, **kwargs)
            stores.append(self)

        def refresh(self):
            clock[0] += 1
            return super().refresh()

    report_path = tmp_path / "report.json"
    monkeypatch.setattr(evaluation, "WikiStore", TimedStore)
    monkeypatch.setattr(evaluation.time, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--private-output", str(report_path)])
    evaluation.main()
    report = json.loads(report_path.read_text())
    assert report["build_ms"] == 2000
    assert len(report["corpus_revisions"]) == 8
    assert len(report["queries"]) == 15
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
    assert stores[0].events._closed


def test_failed_evaluation_releases_its_observer(evaluation, tmp_path, monkeypatch):
    stores = []

    def track(*args, **kwargs):
        store = WikiStore(*args, **kwargs)
        stores.append(store)
        return store

    def unavailable(*args, **kwargs):
        raise RuntimeError("diagnostic ranking failed")

    monkeypatch.setattr(evaluation, "WikiStore", track)
    monkeypatch.setattr(evaluation, "baseline_search", unavailable)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT)])
    with pytest.raises(RuntimeError, match="diagnostic ranking failed"):
        evaluation.main()
    assert stores[0].events._closed


def test_private_evaluation_imports_metadata_before_indexing_and_counts_all_evidence(
    evaluation, tmp_path, monkeypatch,
):
    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    text = "# Note\n\n## First\nNeedle evidence one.\n\n## Second\nCompass evidence two.\n"
    (root / "Library/note.md").write_text(text)
    state = tmp_path / "source-state"
    source = WikiStore(root, state, str(uuid.uuid4()), create=True)
    try:
        document = source.read("Library/note.md")
        with source.connect() as db:
            db.execute("INSERT INTO managed_notes VALUES(?,?,?,?,?)", (
                document["path"], document["revision"], document["revision"], "[]",
                json.dumps({"aliases": ["uniquemetadataalias"]}),
            ))
    finally:
        source.close()
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    source_bytes = (state / "index.sqlite3").read_bytes()
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps([[query, document["path"]] for query in ("uniquemetadataalias", "needle compass")]))
    report_path = tmp_path / "private.json"
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--vault", str(root), "--metadata-state", str(state),
                                      "--cases", str(cases), "--private-output", str(report_path)])
    evaluation.main()
    report = json.loads(report_path.read_text())
    alias, evidence = report["queries"]
    assert alias["chapter_top3"] == [document["path"]]
    assert len(evidence["evidence"]) >= 2
    assert evidence["read_characters"] == sum(part["characters"] for part in evidence["evidence"])
    for query in report["queries"]:
        assert len(query["samples"]) == 5
        assert [sample["first"] for sample in query["samples"]] == ["baseline", "chapter", "baseline", "chapter", "baseline"]
        assert query["baseline_file_reads"] == query["samples"][-1]["baseline_file_reads"]
        assert query["chapter_file_reads"] == query["samples"][-1]["chapter_file_reads"]
    assert {path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()} == before
    assert (state / "index.sqlite3").read_bytes() == source_bytes
