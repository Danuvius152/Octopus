"""Malformed cursors and stale cursors have distinct public HTTP results."""

import base64
import json

import pytest

from tests.test_wiki_api import client as client
from tests.test_wiki_api import configure


def _encode(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode()


@pytest.mark.parametrize("endpoint,params", [
    ("documents", {}), ("search", {"query": "pagination"}), ("graph", {"doc": "center.md"}),
])
async def test_pagination_rejects_malformed_cursors_and_preserves_valid_continuation(client, tmp_path, endpoint, params):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "center.md").write_text("# pagination\n[[first]]\n[[second]]\n")
    (vault / "first.md").write_text("# pagination first\nFirst note.")
    (vault / "second.md").write_text("# pagination second\nSecond note.")
    await configure(client, vault)
    url = f"/api/wiki/{endpoint}"
    request = {**params, "limit": 1}
    first = await client.get(url, params=request)
    assert first.status_code == 200, first.text
    cursor = first.json()["next_cursor"]
    assert cursor
    original = json.loads(base64.urlsafe_b64decode(cursor))
    missing_epoch = {key: value for key, value in original.items() if key != "epoch"}
    malformed = [[], None, "text", missing_epoch]
    for field in ("offset", "epoch"):
        malformed.extend({**original, field: value} for value in (True, False, -1, 2**63, "1", 1.5, None))
    for value in malformed:
        response = await client.get(url, params={**request, "cursor": _encode(value)})
        assert response.status_code == 422, (endpoint, value, response.status_code, response.text[:200])
    for invalid in ("", "!" + cursor, cursor + "!", "中", "x" * 2049):
        response = await client.get(url, params={**request, "cursor": invalid})
        assert response.status_code == 422
    assert (await client.get(url, params={**request, "cursor": cursor, "offset": 1})).status_code == 422
    assert (await client.get(url, params={**request, "offset": 2**63})).status_code == 422
    second = await client.get(url, params={**request, "cursor": cursor})
    expected = await client.get(url, params={**request, "offset": 1})
    assert second.status_code == expected.status_code == 200
    key = "edges" if endpoint == "graph" else "items"
    assert second.json()[key] == expected.json()[key]
    assert second.json()[key] != first.json()[key]
    changed_filters = await client.get(url, params={**request, "cursor": _encode({**original, "filters": "different"})})
    assert changed_filters.status_code == 422
    # A real indexed content change invalidates an otherwise well-formed cursor.
    (vault / "first.md").write_text("# pagination first\nChanged content.")
    read = await client.get("/api/wiki/read", params={"doc": "first.md"})
    assert read.status_code == 200
    stale = await client.get(url, params={**request, "cursor": cursor})
    assert stale.status_code == 409, stale.text
