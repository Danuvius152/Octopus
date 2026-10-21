"""Wiki-only review regressions; assertions describe the required behavior.

Run from the repository root (no model or production vault is used):
    .venv/bin/python -m pytest -p tests.conftest -q -s \
        docs/review/wiki_additional_repros_20261020.py

These regressions intentionally fail on review baseline 5d99de373921.
They are review evidence, not a claim that the implementation is fixed.
"""

import json
import uuid

import pytest

from server.wiki import format
from server.wiki.curation import WikiCuration
from server.wiki.links import anchor_exists
from server.wiki.store import WikiError, WikiStore
from tests import test_wiki_api as api
from tests.test_wiki_grant_v2 import assigned
from tests.test_wiki_maintenance_state import prepare

HEADERS = api.HEADERS
client = api.client


async def read(client, headers, selector):
    response = await client.get(
        "/api/wiki/read", headers=headers, params={"doc": selector}
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("normalized", [False, True])
async def test_organize_with_relation_preserves_omitted_native_sources(
    client, tmp_path, normalized
):
    root, config, jobs = await prepare(
        client,
        tmp_path,
        {
            "Library/a.md": "# A\nOriginal source A.\n",
            "Library/b.md": "# B\nAdditional related source B.\n",
        },
    )
    a = await read(client, HEADERS, "Library/a.md")
    b = await read(client, HEADERS, "Library/b.md")
    native = (
        "---\nsources:\n"
        f"  - doc: {a['id']}\n    revision: {a['revision']}\n"
        "    location: L1-L2\n---\n# Note\nOriginal claim and conditions.\n"
    )
    (root / "Library/note.md").write_text(native)
    jobs.store.refresh()
    job = jobs.maintain(
        "native-sources", "", "Add a related note", ["Library/note.md"], None, [], "human"
    )
    with assigned(job, config) as (headers, _):
        note = await read(client, headers, "Library/note.md")
        await read(client, headers, b["id"])
        response = await client.post(
            "/api/wiki/organize",
            headers=headers,
            json={
                "path": note["path"],
                "expected_revision": note["revision"],
                "key_note": "Original claim and conditions.",
                "relations": [
                    {
                        "doc": b["id"],
                        "revision": b["revision"],
                        "location": "L1-L2",
                        "explanation": "Additional relevant evidence.",
                    }
                ],
                "reason": "Add B while retaining the existing source A",
                **({"frontmatter_patch": {"type": "Reference"}} if normalized else {}),
            },
        )
        assert response.status_code == 200, response.text
    after = await read(client, HEADERS, note["id"])
    (root / a["path"]).write_text("# A\nLater changed source A.\n")
    jobs.store.refresh()
    changed = await read(client, HEADERS, note["id"])
    facts = {
        "normalized": normalized,
        "before_sources": [ref["doc"] for ref in note["metadata"]["sources"]],
        "after_sources": [ref["doc"] for ref in after["metadata"]["sources"]],
        "after_source_refs": [ref["doc"] for ref in after["metadata"]["source_refs"]],
        "native_source_a_still_in_yaml": any(
            ref.get("doc") == a["id"]
            for ref in format.parse((root / note["path"]).read_text()).fields["sources"]
        ),
        "stale_after_a_changes": changed["stale"],
    }
    print(json.dumps(facts, sort_keys=True))
    assert {a["id"], b["id"]} <= set(facts["after_source_refs"]), facts
    assert facts["stale_after_a_changes"], facts


@pytest.mark.parametrize("move_when", ["none", "before_write", "after_write"])
async def test_publication_recovery_follows_the_same_moved_identity(
    client, tmp_path, move_when
):
    old, new = "Library/note.md", "Library/moved.md"
    root, config, jobs = await prepare(
        client, tmp_path, {old: "# Note\n\nOriginal claim and conditions.\n"}
    )
    job = jobs.maintain("move-recovery", "", "Add a summary", [old], None, [], "human")
    item = dict(job["items"][0])
    with assigned(job, config) as (headers, _):
        if move_when == "before_write":
            (root / old).rename(root / new)
            jobs.store.refresh()
        note = await read(client, headers, item["doc"])
        response = await client.post(
            "/api/wiki/organize",
            headers=headers,
            json={
                "path": note["path"],
                "expected_revision": note["revision"],
                "key_note": "Original claim and conditions.",
                "reason": "Preserve the selected identity across its move",
            },
        )
        assert response.status_code == 200, response.text
        if move_when == "after_write":
            (root / old).rename(root / new)
            jobs.store.refresh()
    output = await read(client, HEADERS, item["doc"])
    expected_path = old if move_when == "none" else new
    assert output["path"] == expected_path
    assert output["revision"] == response.json()["revision"]
    assert jobs.recover_publication(job["id"], item)
    print(json.dumps({"move_when": move_when, "item": item}, sort_keys=True))
    assert item["status"] == "done" and item["index_complete"], item
    assert item["output"] == expected_path, item


@pytest.mark.parametrize("heading_style", ["atx", "setext"])
def test_commonmark_heading_can_be_linked_by_an_actual_publication(
    tmp_path, heading_style
):
    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    heading = "Topic\n=====\n" if heading_style == "setext" else "# Topic\n"
    text = heading + "\nOriginal section content.\n"
    (root / "Library/target.md").write_text(text)
    store = WikiStore(root, tmp_path / "state", uuid.uuid4().hex)
    try:
        assert format.parse(text).headings[0]["title"] == "Topic"
        WikiCuration(store).publish(
            "Library/new.md",
            "# New\n\n[Topic](target.md#topic)\n",
            "concept",
            None,
            None,
            "human",
            "Link an existing CommonMark heading",
        )
        assert (root / "Library/new.md").is_file()
    finally:
        store.close()


def test_publication_rejects_a_heading_that_exists_only_inside_a_code_example(tmp_path):
    text = "# Top\n\n```md\nexample\n```still-code\n# Fake\n```\n"
    assert [heading["title"] for heading in format.parse(text).headings] == ["Top"]
    root = tmp_path / "vault"
    (root / "Library").mkdir(parents=True)
    (root / "Library/target.md").write_text(text)
    store = WikiStore(root, tmp_path / "state", uuid.uuid4().hex)
    try:
        print(json.dumps({"code_example_anchor_accepted": anchor_exists(text, "fake")}))
        with pytest.raises(WikiError, match="New local anchor does not exist"):
            WikiCuration(store).publish(
                "Library/new.md",
                "# New\n\n[Fake](target.md#fake)\n",
                "concept",
                None,
                None,
                "human",
                "Reject a nonexistent heading inside a code example",
            )
    finally:
        store.close()


async def test_reread_journals_the_current_selected_identity_after_path_reuse(
    client, tmp_path
):
    from server.wiki.history import WikiHistory

    old, moved, other = "Library/a.md", "Library/moved-a.md", "Library/b.md"
    root, _, jobs = await prepare(
        client,
        tmp_path,
        {old: "# A\nOriginal A.\n", other: "# B\nUnrelated B.\n"},
    )
    job = jobs.maintain("move-reread", "", "Review A", [old], None, [], "human")
    jobs.update(job["id"], status="running")
    item = dict(job["items"][0])
    b = jobs.store.read(other)
    (root / old).rename(root / moved)
    (root / other).rename(root / old)
    (root / moved).write_text("# A\nA new human condition for A.\n")
    jobs.store.refresh()
    assert jobs.store.read(moved)["id"] == item["doc"]
    assert jobs.store.read(old)["id"] == b["id"]
    before_b = (root / old).read_bytes()
    jobs.reread(job["id"], item, "A changed after submission")
    change_id = item["candidate_changes"][-1]
    with jobs.store.connect() as db:
        change = dict(
            db.execute(
                "SELECT c.path,r.document_id,c.before_hash FROM changes c "
                "JOIN change_retention r ON r.change_id=c.id WHERE c.id=?",
                (change_id,),
            ).fetchone()
        )
    change["before_bytes_are_from_a"] = (
        WikiHistory(jobs.store).blobs / change["before_hash"]
    ).read_bytes() == (root / moved).read_bytes()
    assert (root / old).read_bytes() == before_b
    try:
        WikiHistory(jobs.store).restore(change_id, b["revision"], "before", "human")
    except WikiError as error:
        restore_status = error.status
    else:
        restore_status = 200
    facts = {
        "reread_change": change,
        "selected_doc": item["doc"],
        "restore_status": restore_status,
        "restore_changed_unrelated_b": (root / old).read_bytes() != before_b,
        "restored_b_contains_a": (root / old).read_bytes() == (root / moved).read_bytes(),
    }
    print(json.dumps(facts, sort_keys=True))
    assert change["path"] == moved and change["document_id"] == item["doc"], change
    assert not facts["restore_changed_unrelated_b"], facts
