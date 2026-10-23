"""Identity lookup stays exact; natural title questions retain source selection."""

import os
import uuid
from itertools import product
from pathlib import Path

import pytest

from server.wiki.store import WikiStore

QUESTION_INTENTS = [None, "question", "overview", "compare"]
INTENTS = [*QUESTION_INTENTS, "lookup"]
VIEWS = ["compact", "full"]
SOURCE_UID = "urn:uuid:abcdef12-3456-4789-8abc-def012345678"
SUMMARY_UID = "urn:uuid:fedcba98-7654-4321-8fed-cba987654321"
SOURCE = (
    "---\ntype: Reference\ntitle: Alpha report\n"
    f"uid: {SOURCE_UID}\n---\n# Alpha report\n"
    "Realtime uses video distillation under fixed frame length.\n"
    "Live rendering requires sufficient calibration accuracy.\n"
)


def summary(source_path="alpha.md", title="Realtime", relation=True):
    return (
        "---\ntype: Synthesis\n"
        f"title: {title}\naliases: [Live rendering]\nuid: {SUMMARY_UID}\n"
        + (f"x_wiki: {{representation_of: {source_path}}}\n" if relation else "")
        + "---\n# Realtime\nRealtime uses video distillation. Live rendering is fast.\n"
    )


def write(store, path, text):
    file = store.root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text)


def rows(store):
    with store.connect() as db:
        return {row["path"]: dict(row) for row in db.execute("SELECT * FROM documents")}


@pytest.fixture
def make_store(tmp_path):
    stores = []

    def create(source=SOURCE, source_path="alpha.md", represented=True, title="Realtime"):
        base = tmp_path / str(len(stores))
        root = base / "vault"
        root.mkdir(parents=True)
        file = root / source_path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(source)
        (root / "Summaries").mkdir(exist_ok=True)
        (root / "Summaries/summary.md").write_text(summary(source_path, title, represented))
        store = WikiStore(root, base / "state", str(uuid.uuid4()), create=True)
        stores.append(store)
        return store

    yield create
    for store in stores:
        store.close()


def assert_locations(row, item, view):
    assert (item["id"], item["path"], item["revision"], item["title"]) == (
        row["id"], row["path"], row["revision"], row["title"],
    )
    snippet_start = item["snippet_start"]
    assert item["snippet"] == row["text"][snippet_start:snippet_start + len(item["snippet"])]
    assert item["sections"]
    for section in item["sections"]:
        start, end = section["start"], section["end"]
        assert 0 <= start < end <= len(row["text"])
        if view == "compact":
            assert section["id"] == f"{row['id']}:{row['revision']}:{start}:{end}"
        else:
            assert section["line_start"] == row["text"].count("\n", 0, start) + 1
            assert section["line_end"] == row["text"].count("\n", 0, end) + 1


def test_path_id_uid_keep_exact_object_in_all_sixty_cases(make_store):
    store = make_store()
    expected = rows(store)
    # All 60 read-only combinations share the same two indexed documents.
    for intent, view in product(INTENTS, VIEWS):
        for path, uid in [("alpha.md", SOURCE_UID), ("Summaries/summary.md", SUMMARY_UID)]:
            row = expected[path]
            for query in [path, row["id"], uid]:
                found = store.search(query, intent=intent, view=view)
                assert found["match_mode"] == "exact" and found["total"] == 1
                assert [(hit["id"], hit["path"], hit["revision"]) for hit in found["items"]] == [
                    (row["id"], path, row["revision"]),
                ]


def test_registered_mixed_case_uid_wins_over_same_spelling_titles(make_store):
    store = make_store()
    target = rows(store)["Summaries/summary.md"]
    queries = [target["path"], target["id"], SUMMARY_UID, SUMMARY_UID.upper(), "UrN:uUiD:" + SUMMARY_UID[9:].upper()]
    for index, query in enumerate(queries):
        write(store, f"collision-{index}.md", f"---\ntitle: {query}\n---\n# Collision\n{query} is another document.\n")
    store.refresh()
    for intent, view, query in product(INTENTS, VIEWS, queries):
        found = store.search(query, intent=intent, view=view)
        assert found["match_mode"] == "exact" and found["total"] == 1
        assert [item["id"] for item in found["items"]] == [target["id"]]


def test_natural_title_and_alias_use_source_selector(make_store):
    store = make_store()
    source = rows(store)["alpha.md"]
    for intent, view, query in product(QUESTION_INTENTS, VIEWS, ["Realtime", "Live rendering", "What is Realtime"]):
        found = store.search(query, intent=intent, view=view)
        assert found["total"] == 1 and found["match_mode"] in {"lexical", "comparison"}
        assert [item["id"] for item in found["items"]] == [source["id"]]
        item = found["items"][0]
        assert_locations(source, item, view)
        assert "fixed frame length" in item["snippet"] or "calibration accuracy" in item["snippet"]


def test_lookup_title_alias_keep_the_named_summary(make_store):
    store = make_store()
    for view, query in product(VIEWS, ["Realtime", "Live rendering"]):
        found = store.search(query, intent="lookup", view=view)
        assert found["total"] == 1 and found["match_mode"] == "exact"
        assert [item["path"] for item in found["items"]] == ["Summaries/summary.md"]


def test_unregistered_urn_title_is_semantic_not_identity(make_store):
    fake = "urn:uuid:00000000-0000-4000-8000-000000000099"
    store = make_store(source=SOURCE + f"The topic {fake} describes this evidence.\n", title=fake)
    source = rows(store)["alpha.md"]
    for view, intent, query in product(VIEWS, QUESTION_INTENTS, [fake, fake.upper()]):
        found = store.search(query, intent=intent, view=view)
        assert found["total"] == 1 and found["match_mode"] in {"lexical", "comparison"}
        assert [item["path"] for item in found["items"]] == ["alpha.md"]
        assert_locations(source, found["items"][0], view)
    for view in VIEWS:
        named = store.search(fake, intent="lookup", view=view)
        assert named["match_mode"] == "exact"
        assert [item["path"] for item in named["items"]] == ["Summaries/summary.md"]


def test_path_and_id_remain_case_sensitive(make_store):
    store = make_store(source_path="Alpha.md")
    source = rows(store)["Alpha.md"]
    for index, query in enumerate(["ALPHA.MD", source["id"].upper()]):
        assert query not in {"Alpha.md", source["id"]}
        write(store, f"case-{index}.md", f"---\ntitle: {query}\n---\n# Case\nA separate case-sensitive document.\n")
        store.refresh()
        exact = store.search(query, intent="lookup")
        assert [item["path"] for item in exact["items"]] == [f"case-{index}.md"]
        assert store.search(query, intent="question")["match_mode"] != "exact"


@pytest.mark.parametrize("case", ["nonmatching", "unreadable", "directory", "kind", "asset", "error", "excluded"])
def test_source_qualification_and_scope_precede_representation_selection(make_store, case):
    source = SOURCE
    source_path = "Library/assets/alpha.md" if case == "asset" else "Library/alpha.md"
    if case == "nonmatching":
        source = "---\ntype: Reference\n---\n# Alpha report\nUnrelated evidence.\n"
    elif case == "unreadable":
        source = "---\ntype: Reference\ntitle: Realtime\n---\n \t\n"
    store = make_store(source=source, source_path=source_path)
    source_id = rows(store)[source_path]["id"]
    if case in {"error", "excluded"}:
        with store.connect() as db:
            db.execute("UPDATE documents SET quality=? WHERE id=?", (case, source_id))
    expected = rows(store)
    filters = {"directory": "Summaries"} if case == "directory" else {"kind": "topic"} if case == "kind" else {}
    for intent, view in product(QUESTION_INTENTS, VIEWS):
        found = store.search("Realtime", intent=intent, view=view, **filters)
        assert found["total"] == 1
        assert [item["path"] for item in found["items"]] == ["Summaries/summary.md"]
        assert_locations(expected[found["items"][0]["path"]], found["items"][0], view)
        if case == "asset":
            included = store.search("Realtime", intent=intent, view=view, include_assets=True)
            assert included["total"] == 1 and included["items"][0]["id"] == source_id
            assert_locations(expected[included["items"][0]["path"]], included["items"][0], view)


@pytest.mark.parametrize("scope", ["directory", "kind", "asset"])
def test_filtered_identity_does_not_fall_back_to_another_title(make_store, scope):
    path = "Library/assets/alpha.md" if scope == "asset" else "Library/alpha.md"
    store = make_store(source_path=path)
    source = rows(store)[path]
    queries = [path, source["id"], SOURCE_UID.upper()]
    for index, query in enumerate(queries):
        write(store, f"Summaries/title-{index}.md", f"---\ntitle: {query}\n---\n# Match\n{query} refers to something else.\n")
    store.refresh()
    filters = {"directory": "Summaries"} if scope == "directory" else {"kind": "topic"} if scope == "kind" else {}
    for intent, view, query in product(INTENTS, VIEWS, queries):
        found = store.search(query, intent=intent, view=view, **filters)
        assert found["match_mode"] == "exact"
        assert found["items"] == [] and found["total"] == 0
        assert found["next_cursor"] is None and found["next_offset"] is None
        allowed = {"directory": "Library", "include_assets": scope == "asset"}
        if scope == "kind":
            allowed["kind"] = source["kind"]
        included = store.search(query, intent=intent, view=view, **allowed)
        assert included["match_mode"] == "exact" and included["total"] == 1
        assert [item["id"] for item in included["items"]] == [source["id"]]


def test_retired_registered_id_and_uid_do_not_select_a_same_spelling_title(make_store):
    store = make_store()
    source = rows(store)["alpha.md"]
    (store.root / "alpha.md").unlink()
    store.refresh()
    queries = [source["id"], SOURCE_UID.upper()]
    for index, query in enumerate(queries):
        write(store, f"retired-title-{index}.md", f"---\ntitle: {query}\n---\n# Other\nAnother document.\n")
    store.refresh()
    for intent, view, query in product(INTENTS, VIEWS, queries):
        found = store.search(query, intent=intent, view=view)
        assert found["match_mode"] == "exact" and found["total"] == 0
        assert found["items"] == []


@pytest.mark.parametrize("represented", [True, False])
def test_duplicate_titles_are_ambiguous_only_for_lookup(make_store, represented):
    source = SOURCE.replace("title: Alpha report", "title: Realtime")
    store = make_store(source=source, represented=represented)
    expected = rows(store)
    for view in VIEWS:
        lookup = store.search("Realtime", intent="lookup", view=view)
        assert lookup["match_mode"] == "ambiguous" and lookup["total"] == 2
        for intent in [None, "question"]:
            found = store.search("Realtime", intent=intent, view=view)
            assert found["match_mode"] == "lexical"
            assert found["total"] == (1 if represented else 2)
            assert {item["path"] for item in found["items"]} == ({"alpha.md"} if represented else {"alpha.md", "Summaries/summary.md"})
            for item in found["items"]:
                assert_locations(expected[item["path"]], item, view)


def test_exact_title_pagination_counts_selected_sources_once(make_store):
    store = make_store()
    for index in range(20):
        write(store, f"Summaries/extra-{index}.md", summary())
    for name in ["beta", "gamma"]:
        write(store, name + ".md", f"---\ntitle: Realtime\n---\n# {name}\nRealtime independent {name} evidence.\n")
    store.refresh()
    expected = rows(store)
    for intent, view in product(QUESTION_INTENTS, VIEWS):
        complete = store.search("Realtime", intent=intent, view=view)
        assert complete["total"] == 3
        pages = []
        page = store.search("Realtime", intent=intent, view=view, limit=1)
        while True:
            assert page["total"] == 3
            assert len(page["items"]) == 1
            pages.extend(page["items"])
            assert_locations(expected[page["items"][0]["path"]], page["items"][0], view)
            if page["next_cursor"] is None:
                assert page["next_offset"] is None
                break
            assert page["next_offset"] == len(pages)
            page = store.search("Realtime", intent=intent, view=view, limit=1, cursor=page["next_cursor"])
        assert [item["id"] for item in pages] == [item["id"] for item in complete["items"]]
        assert len({item["id"] for item in pages}) == 3
        assert {item["path"] for item in pages} == {"alpha.md", "beta.md", "gamma.md"}
        offset = store.search("Realtime", intent=intent, view=view, limit=1, offset=1)
        assert [item["id"] for item in offset["items"]] == [pages[1]["id"]]


def test_title_alias_and_registered_identity_queries_never_open_source_files(make_store, monkeypatch):
    store = make_store()
    source = rows(store)["alpha.md"]
    original_walk, original_stat, original_open = os.walk, Path.stat, Path.open
    accesses = []

    def walk(path, *args, **kwargs):
        if Path(path).is_relative_to(store.root):
            accesses.append(("walk", str(path)))
        return original_walk(path, *args, **kwargs)

    def stat(path, *args, **kwargs):
        if path != store.root and path.is_relative_to(store.root):
            accesses.append(("stat", str(path)))
        return original_stat(path, *args, **kwargs)

    def opened(path, mode="r", *args, **kwargs):
        if path.is_relative_to(store.root):
            accesses.append(("open", str(path)))
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "walk", walk)
    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(Path, "open", opened)
    for intent in INTENTS:
        for view in VIEWS:
            for query in ["Realtime", "Live rendering", "alpha.md", source["id"], SOURCE_UID.upper()]:
                assert store.search(query, intent=intent, view=view)["items"]
    assert accesses == []
