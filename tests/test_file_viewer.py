"""Tests for the in-app file viewer.

Covers:
  * `server.file_viewer.resolve_safe_path` — the single security gate
    every viewer entry point funnels through. The most important tests
    are the negative ones (escape, symlink escape, missing, oversize).
  * `GET /api/sessions/{id}/files{,/meta}` — wired endpoint, including
    auth (header + query-token both accepted) and proper status codes
    for each FileViewerError subclass.
"""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from server import deps
from server.database import Database
from server.file_viewer import (
    MAX_FILE_BYTES,
    FileNotFound,
    FileTooLarge,
    PathRejected,
    UnsupportedType,
    resolve_safe_path,
)
from server.main import app
from server.session_manager import session_manager
from tests.accounts import create_account

# ---------------------------------------------------------------------------
# resolve_safe_path — security unit tests
# ---------------------------------------------------------------------------


def test_resolves_simple_relative_path(tmp_path):
    (tmp_path / "doc.md").write_text("# hi")
    r = resolve_safe_path(tmp_path, "doc.md")
    assert r.relative_path == "doc.md"
    assert r.kind == "markdown"
    assert r.mime_type.startswith("text/markdown")
    assert r.size == 4


def test_resolves_nested_relative_path(tmp_path):
    sub = tmp_path / "sub" / "deep"
    sub.mkdir(parents=True)
    (sub / "file.py").write_text("x = 1\n")
    r = resolve_safe_path(tmp_path, "sub/deep/file.py")
    assert r.kind == "code"
    assert r.relative_path == str(Path("sub/deep/file.py"))


def test_accepts_absolute_path_inside_root(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    r = resolve_safe_path(tmp_path, str(tmp_path / "a.txt"))
    assert r.relative_path == "a.txt"


def test_rejects_empty_path(tmp_path):
    with pytest.raises(PathRejected):
        resolve_safe_path(tmp_path, "")
    with pytest.raises(PathRejected):
        resolve_safe_path(tmp_path, "   ")


def test_rejects_tilde_path(tmp_path):
    with pytest.raises(PathRejected):
        resolve_safe_path(tmp_path, "~/secret")


def test_rejects_dotdot_escape(tmp_path):
    sub = tmp_path / "inside"
    sub.mkdir()
    (tmp_path.parent / "outside.txt").write_text("nope")
    with pytest.raises(PathRejected):
        resolve_safe_path(sub, "../outside.txt")


def test_rejects_absolute_path_outside_root(tmp_path):
    (tmp_path.parent / "elsewhere.txt").write_text("nope")
    with pytest.raises(PathRejected):
        resolve_safe_path(tmp_path, str(tmp_path.parent / "elsewhere.txt"))


def test_rejects_symlink_escaping_root(tmp_path):
    outside_root = tmp_path / "outside"
    outside_root.mkdir()
    (outside_root / "secret.md").write_text("# secret")
    inside_root = tmp_path / "inside"
    inside_root.mkdir()
    # A symlink in /inside that points to /outside/secret.md.
    # realpath resolves it; relative_to(/inside) must fail.
    (inside_root / "link.md").symlink_to(outside_root / "secret.md")
    with pytest.raises(PathRejected):
        resolve_safe_path(inside_root, "link.md")


def test_missing_file_raises_filenotfound(tmp_path):
    with pytest.raises(FileNotFound):
        resolve_safe_path(tmp_path, "nope.md")


def test_directory_path_raises_filenotfound(tmp_path):
    (tmp_path / "subdir").mkdir()
    with pytest.raises(FileNotFound):
        resolve_safe_path(tmp_path, "subdir")


def test_oversized_file_raises_filetoolarge(tmp_path):
    big = tmp_path / "huge.md"
    big.write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    with pytest.raises(FileTooLarge):
        resolve_safe_path(tmp_path, "huge.md")


def test_unsupported_extension_raises(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02")
    with pytest.raises(UnsupportedType):
        resolve_safe_path(tmp_path, "blob.bin")


def test_extensionless_dockerfile_allowed(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM python\n")
    r = resolve_safe_path(tmp_path, "Dockerfile")
    assert r.kind == "code"


def test_extensionless_readme_allowed(tmp_path):
    (tmp_path / "README").write_text("hi\n")
    r = resolve_safe_path(tmp_path, "README")
    assert r.kind == "code"


def test_classify_image(tmp_path):
    (tmp_path / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    r = resolve_safe_path(tmp_path, "shot.png")
    assert r.kind == "image"
    assert r.mime_type == "image/png"


def test_classify_pdf(tmp_path):
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4\n%fake")
    r = resolve_safe_path(tmp_path, "doc.pdf")
    assert r.kind == "pdf"
    assert r.mime_type == "application/pdf"


def test_classify_text_log(tmp_path):
    (tmp_path / "out.log").write_text("hello\n")
    r = resolve_safe_path(tmp_path, "out.log")
    assert r.kind == "text"
    assert r.mime_type == "text/plain; charset=utf-8"


def test_classify_yaml_is_code(tmp_path):
    (tmp_path / "ci.yaml").write_text("on: push\n")
    r = resolve_safe_path(tmp_path, "ci.yaml")
    assert r.kind == "code"


# ---------------------------------------------------------------------------
# /api/sessions/{id}/files endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
async def client(tmp_path):
    """In-memory DB + ASGI client, mirrors test_attachments.client."""
    db = Database(":memory:")
    await db.initialize()
    _, bearer = await create_account(db, extra_roots=[tmp_path])
    session_manager.sessions.clear()
    session_manager._admission_closed = False
    await session_manager.initialize(db)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {bearer}"}) as c:
        yield c
    await db.close()


@pytest.fixture
async def session_with_files(client, tmp_path):
    """Create a live session whose working_dir is a fresh tmpdir, and
    populate it with files of various kinds. Returns (session_id, root)."""
    root = tmp_path / "wd"
    root.mkdir()
    (root / "plan.md").write_text("# plan\nstep one")
    (root / "main.py").write_text("def main():\n    pass\n")
    (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    (root / "doc.pdf").write_bytes((Path(__file__).parent / "fixtures" / "viewer.pdf").read_bytes())
    (root / "blob.bin").write_bytes(b"\x00\x01")
    (root / "huge.md").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    sub = root / "sub"
    sub.mkdir()
    (sub / "note.txt").write_text("nested")
    (root.parent / "outside.md").write_text("escape me")

    agent = await session_manager.db.get_system_agent()
    sess = await session_manager.create_session(
        agent["id"], name="viewer-test", working_dir=str(root)
    )
    return sess.id, root


async def test_get_file_markdown(client, session_with_files):
    sid, _ = session_with_files
    r = await client.get(
        f"/api/sessions/{sid}/files",
        params={"path": "plan.md"},
        headers=client.headers,
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert b"# plan" in r.content


async def test_markdown_bytes_follow_the_authorized_canonical_path(client, session_with_files):
    sid, root = session_with_files
    url = f"/api/sessions/{sid}/files"
    (root / "sub" / "second.md").write_text("# replacement")
    alias = root / "alias.md"
    alias.symlink_to("plan.md")

    meta = await client.get(f"{url}/meta", params={"path": "alias.md"})
    assert meta.status_code == 200 and meta.json()["path"] == "plan.md"
    params = {"path": meta.json()["path"], "canonical_path_id": meta.json()["canonical_path_id"]}
    alias.unlink()
    alias.symlink_to("sub/second.md")

    original = await client.get(url, params=params)
    assert original.status_code == 200 and original.text == "# plan\nstep one"

    (root / "plan.md").unlink()
    (root / "plan.md").symlink_to("sub/second.md")
    changed = await client.get(url, params=params)
    assert changed.status_code == 409

    reopened = await client.get(f"{url}/meta", params={"path": "alias.md"})
    assert reopened.status_code == 200 and reopened.json()["path"] == "sub/second.md"
    replacement = await client.get(url, params={
        "path": reopened.json()["path"], "canonical_path_id": reopened.json()["canonical_path_id"],
    })
    assert replacement.status_code == 200 and replacement.text == "# replacement"

    (root / "~notes.md").write_text("# literal tilde")
    tilde = await client.get(f"{url}/meta", params={"path": "./~notes.md"})
    assert tilde.status_code == 200 and tilde.json()["path"] == "~notes.md"
    literal = await client.get(url, params={
        "path": tilde.json()["path"], "canonical_path_id": tilde.json()["canonical_path_id"],
    })
    assert literal.status_code == 200 and literal.text == "# literal tilde"


async def test_viewer_paths_are_exact_http_query_values(client, session_with_files):
    sid, root = session_with_files
    url = f"/api/sessions/{sid}/files"
    (root / "appendix.md").write_text("ordinary")
    assert (await client.get(f"{url}?path=appendix.md%20")).status_code == 404
    (root / "appendix.md ").write_text("trailing space")
    assert (await client.get(f"{url}?path=appendix.md%20")).status_code == 415
    ordinary = await client.get(f"{url}?path=appendix.md")
    assert ordinary.status_code == 200 and ordinary.text == "ordinary"

    name = " 中文%20#图.md"
    (root / name).write_text("literal")
    query = "?path=%20%E4%B8%AD%E6%96%87%2520%23%E5%9B%BE.md"
    meta = await client.get(f"{url}/meta{query}")
    assert meta.status_code == 200 and meta.json()["path"] == name
    body = await client.get(f"{url}{query}&canonical_path_id={meta.json()['canonical_path_id']}")
    assert body.status_code == 200 and body.text == "literal"


async def test_canonical_markdown_detects_a_session_root_redirect_with_the_same_relative_name(
    client, session_with_files,
):
    sid, root = session_with_files
    url = f"/api/sessions/{sid}/files"
    meta = await client.get(f"{url}/meta", params={"path": "plan.md"})
    assert meta.status_code == 200 and meta.json()["path"] == "plan.md"

    root.rename(root.with_name("original-wd"))
    replacement_root = root.with_name("replacement-wd")
    replacement_root.mkdir()
    (replacement_root / "plan.md").write_text("# Other working directory")
    root.symlink_to(replacement_root, target_is_directory=True)

    stale = await client.get(url, params={
        "path": meta.json()["path"], "canonical_path_id": meta.json()["canonical_path_id"],
    })
    assert stale.status_code == 409
    reopened = await client.get(f"{url}/meta", params={"path": "plan.md"})
    assert reopened.status_code == 200 and reopened.json()["path"] == "plan.md"
    replacement = await client.get(url, params={
        "path": reopened.json()["path"], "canonical_path_id": reopened.json()["canonical_path_id"],
    })
    assert replacement.status_code == 200 and replacement.text == "# Other working directory"


async def test_canonical_and_relative_resource_reads_keep_the_session_root(client, session_with_files):
    sid, root = session_with_files
    url = f"/api/sessions/{sid}/files"
    agent = await session_manager.db.get_system_agent()
    sibling = await session_manager.create_session(
        agent["id"], name="other document", working_dir=str(root.parent),
    )
    meta = await client.get(f"{url}/meta", params={"path": "plan.md"})
    assert meta.status_code == 200
    canonical_path_id = meta.json()["canonical_path_id"]
    other_url = f"/api/sessions/{sibling.id}/files"
    assert (await client.get(other_url, params={"path": "outside.md"})).status_code == 200
    (root / "escape.md").symlink_to("../outside.md")
    for path in ("sub/../../outside.md", root.parent / "outside.md", "escape.md"):
        assert (await client.get(url, params={"path": str(path)})).status_code == 403
        assert (await client.get(url, params={"path": str(path), "canonical_path_id": canonical_path_id})).status_code == 403
    unauthenticated = await client.get(
        url, params={"path": "plan.md", "canonical_path_id": canonical_path_id}, headers={"Authorization": ""},
    )
    assert unauthenticated.status_code == 401


async def test_relative_resources_recheck_the_document_directory(client, session_with_files):
    sid, root = session_with_files
    url = f"/api/sessions/{sid}/files"
    docs = root / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text("# Original")
    (docs / "appendix.md").write_text("# Original appendix")
    meta = await client.get(f"{url}/meta", params={"path": "docs/guide.md"})
    assert meta.status_code == 200
    directory = meta.json()["canonical_directory"]
    source = {"source_directory": directory["path"], "source_directory_id": directory["path_id"]}

    params = {"path": "appendix.md", **source}
    resource = await client.get(url, params=params)
    assert resource.status_code == 200 and resource.text == "# Original appendix"
    assert resource.headers["cache-control"] == "no-store"
    for reference, status in (("../plan.md", 200), ("../../outside.md", 403), ("../huge.md", 413), ("../blob.bin", 415)):
        assert (await client.get(url, params={"path": reference, **source})).status_code == status

    docs.rename(root / "retired")
    alternate = root / "alternate"
    alternate.mkdir()
    (alternate / "guide.md").write_text("# Replacement")
    (alternate / "appendix.md").write_text("# Replacement appendix")
    docs.symlink_to(alternate, target_is_directory=True)

    for endpoint in (url, f"{url}/meta"):
        stale = await client.get(endpoint, params=params)
        assert stale.status_code == 409
        assert "document directory changed" in stale.json()["detail"]

    reopened = await client.get(f"{url}/meta", params={"path": "docs/guide.md"})
    assert reopened.status_code == 200
    current = reopened.json()["canonical_directory"]
    assert current["path"] == "alternate" and current["path_id"] != directory["path_id"]
    replacement = await client.get(url, params={
        "path": "appendix.md", "source_directory": current["path"], "source_directory_id": current["path_id"],
    })
    assert replacement.status_code == 200 and replacement.text == "# Replacement appendix"


async def test_get_file_code(client, session_with_files):
    sid, _ = session_with_files
    r = await client.get(
        f"/api/sessions/{sid}/files",
        params={"path": "main.py"},
        headers=client.headers,
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert b"def main" in r.content


async def test_get_file_image(client, session_with_files):
    sid, _ = session_with_files
    r = await client.get(
        f"/api/sessions/{sid}/files",
        params={"path": "logo.png"},
        headers=client.headers,
    )
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"


async def test_pdf_preview_is_inline_while_default_and_download_remain_attachments(client, session_with_files):
    sid, root = session_with_files
    name = "指南 %.pdf"
    (root / name).write_bytes((root / "doc.pdf").read_bytes())
    url = f"/api/sessions/{sid}/files"
    preview = await client.get(url, params={"path": name, "preview": "true"})
    assert preview.status_code == 200
    assert preview.headers["content-type"] == "application/pdf"
    assert preview.headers["content-disposition"].startswith("inline;")
    assert "filename*=utf-8''%E6%8C%87%E5%8D%97%20%25.pdf" in preview.headers["content-disposition"]
    for params in ({"path": name}, {"path": name, "preview": "false"}):
        download = await client.get(url, params=params)
        assert download.content == preview.content
        assert download.headers["content-disposition"].startswith("attachment;")
        assert download.headers["etag"] == preview.headers["etag"]
    partial = await client.get(url, params={"path": name, "preview": "true"}, headers={"Range": "bytes=0-7"})
    assert partial.status_code == 206 and partial.content == b"%PDF-1.4"
    assert partial.headers["etag"] == preview.headers["etag"]
    assert partial.headers["content-disposition"].startswith("inline;")


@pytest.mark.parametrize("path", ["plan.md", "main.py", "logo.png", "../outside.md", "no-such.pdf", "huge.md", "blob.bin"])
async def test_preview_preserves_non_pdf_behavior_and_path_gates(client, session_with_files, path):
    sid, _ = session_with_files
    url = f"/api/sessions/{sid}/files"
    default = await client.get(url, params={"path": path})
    preview = await client.get(url, params={"path": path, "preview": "true"})
    assert preview.status_code == default.status_code
    assert preview.content == default.content
    assert preview.headers.get("content-disposition") == default.headers.get("content-disposition")


async def test_get_file_nested_path(client, session_with_files):
    sid, _ = session_with_files
    r = await client.get(
        f"/api/sessions/{sid}/files",
        params={"path": "sub/note.txt"},
        headers=client.headers,
    )
    assert r.status_code == 200
    assert r.content == b"nested"


async def test_get_file_meta_returns_kind(client, session_with_files):
    sid, _ = session_with_files
    r = await client.get(
        f"/api/sessions/{sid}/files/meta",
        params={"path": "plan.md"},
        headers=client.headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "markdown"
    assert body["mime_type"].startswith("text/markdown")
    assert body["path"] == "plan.md"
    assert body["size"] == len("# plan\nstep one")


async def test_get_file_rejects_path_escape(client, session_with_files):
    sid, _ = session_with_files
    r = await client.get(
        f"/api/sessions/{sid}/files",
        params={"path": "../outside.md"},
        headers=client.headers,
    )
    assert r.status_code == 403


async def test_get_file_missing_returns_404(client, session_with_files):
    sid, _ = session_with_files
    r = await client.get(
        f"/api/sessions/{sid}/files",
        params={"path": "no-such.md"},
        headers=client.headers,
    )
    assert r.status_code == 404


async def test_get_file_oversize_returns_413(client, session_with_files):
    sid, _ = session_with_files
    r = await client.get(
        f"/api/sessions/{sid}/files",
        params={"path": "huge.md"},
        headers=client.headers,
    )
    assert r.status_code == 413


async def test_get_file_unsupported_type_returns_415(client, session_with_files):
    sid, _ = session_with_files
    r = await client.get(
        f"/api/sessions/{sid}/files",
        params={"path": "blob.bin"},
        headers=client.headers,
    )
    assert r.status_code == 415


async def test_get_file_token_query_param_works(client, session_with_files):
    """<img src> / <iframe src> path: no Authorization header, token in URL."""
    sid, _ = session_with_files
    r = await client.get(
        f"/api/sessions/{sid}/files",
        params={"path": "plan.md", "token": client.headers["Authorization"].removeprefix("Bearer ")},
        headers={"Authorization": ""},
    )
    assert r.status_code == 200


async def test_get_file_requires_auth(client, session_with_files):
    sid, _ = session_with_files
    r = await client.get(
        f"/api/sessions/{sid}/files",
        params={"path": "plan.md"},
        headers={"Authorization": ""},
    )
    assert r.status_code == 401


async def test_get_file_session_not_found(client):
    r = await client.get(
        "/api/sessions/no-such-session/files",
        params={"path": "x.md"},
        headers=client.headers,
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/sessions/{id}/showme/resolve — credential normalization
# ---------------------------------------------------------------------------
#
# Pins the fix for the credential-shape bug: an attached credential must
# reach the harness as a resolved `HarnessCredential`, never a raw DB row
# (which is encrypted, missing home_dir for Codex, and crashes attribute
# access in `_apply_env_credential` / `_apply_home_dir`). The route goes
# through `session_manager.resolve_credential_by_id`, which normalizes for
# us.


async def _patch_showme(monkeypatch):
    """Capture the resolved one-shot context at the harness boundary."""
    captured: dict = {}

    async def _fake_oneshot(ctx, **kwargs):
        await ctx.authorize_spawn()
        captured["credential"] = ctx.credential
        captured["model"] = ctx.model
        captured["reasoning_effort"] = ctx.reasoning_effort
        captured["service_tier"] = ctx.service_tier
        captured["working_dir"] = ctx.working_dir
        captured["prompt"] = ctx.prompt
        captured["calls"] = captured.get("calls", 0) + 1
        return '{"path":"answer.md"}'

    from server.harness import get_harness

    for backend in ("claude-code", "codex"):
        monkeypatch.setattr(get_harness(backend), "run_oneshot", _fake_oneshot)
    return captured


async def test_showme_resolve_no_credential_passes_none(
    client, session_with_files, monkeypatch
):
    captured = await _patch_showme(monkeypatch)
    sid, _ = session_with_files

    r = await client.post(
        f"/api/sessions/{sid}/showme/resolve",
        json={"text": "the plan"},
        headers=client.headers,
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"path": "answer.md", "message": None}
    # No credential on the agent → resolver should receive None (the CLI
    # falls back to whatever host auth it finds).
    assert captured["credential"] is None


async def test_showme_exact_path_does_not_require_model_authentication(client, session_with_files, monkeypatch):
    sid, _ = session_with_files
    session = session_manager.get_session(sid)
    await session_manager.db.save_credential(
        "unavailable", session.backend, "owned", "api_key", "", "now", user_id=session.user_id,
    )
    await session_manager.db.update_credential("unavailable", needs_reconnect=True)
    session.credential_id = "unavailable"

    async def forbidden_oneshot(*args, **kwargs):
        pytest.fail("An exact local path must not invoke a model")

    history_reads = []
    load_messages = session_manager.db.load_messages

    async def observe_history(*args, **kwargs):
        rows = await load_messages(*args, **kwargs)
        history_reads.append(rows)
        return rows

    monkeypatch.setattr(session_manager.db, "load_messages", observe_history)
    monkeypatch.setattr("server.harness.Harness.run_oneshot", forbidden_oneshot)
    direct = await client.post(f"/api/sessions/{sid}/showme/resolve", json={"text": "plan.md"})
    assert direct.status_code == 200, direct.text
    assert direct.json()["path"] == "plan.md"
    assert history_reads == []
    inferred = await client.post(f"/api/sessions/{sid}/showme/resolve", json={"text": "the proposed plan"})
    assert inferred.status_code == 422
    assert "unavailable" in inferred.json()["detail"]
    assert len(history_reads) == 1


async def test_showme_inference_reads_last_twelve_total_rows_before_filtering(
    client, session_with_files, monkeypatch,
):
    sid, _ = session_with_files
    db = session_manager.db
    for seq in range(1, 53):
        await db.append_message(sid, seq, "user", "text", f"OLD CONTEXT {seq}")
    recent = [
        {"role": "user", "type": "text", "content": "recent user first"},
        {"role": "system", "type": "notice", "content": "ignored notice"},
        {"role": "assistant", "type": "text", "content": "recent assistant"},
        {"role": "assistant", "type": "tool_use", "tool_name": "Read",
         "tool_input": {"path": "sub/note.txt"}},
        {"role": "user", "type": "tool_result", "content": "ignored tool result"},
        {"role": "assistant", "type": "result", "content": "ignored terminal"},
        {"role": "user", "type": "text", "content": {"value": "ignored object"}},
        {"role": "system", "type": "text", "content": "ignored system text"},
        {"role": "assistant", "type": "thinking", "content": "ignored thinking"},
        {"role": "assistant", "type": "tool_use", "tool_name": "Bash",
         "tool_input": {"command": "ignored command"}},
        {"role": "user", "type": "text", "content": "recent user last"},
        {"role": "assistant", "type": "text", "content": "recent assistant last"},
    ]
    for seq, row in enumerate(recent, 53):
        await db.append_message(sid, seq, **row)
    loaded = []
    load_messages = db.load_messages

    async def observe_history(*args, **kwargs):
        rows = await load_messages(*args, **kwargs)
        loaded.append([row["seq"] for row in rows])
        return rows

    monkeypatch.setattr(db, "load_messages", observe_history)
    captured = await _patch_showme(monkeypatch)
    response = await client.post(f"/api/sessions/{sid}/showme/resolve", json={"text": "the proposed plan"})
    assert response.status_code == 200, response.text
    assert response.json() == {"path": "answer.md", "message": None}
    assert loaded == [list(range(53, 65))]
    assert captured["calls"] == 1
    prompt = captured["prompt"]
    assert "OLD CONTEXT" not in prompt
    assert "ignored" not in prompt
    assert prompt.index("User: recent user first") < prompt.index("Assistant: recent assistant")
    assert prompt.index("Assistant used Read with path='sub/note.txt'") < prompt.index("User: recent user last")
    assert prompt.index("User: recent user last") < prompt.index("Assistant: recent assistant last")


@pytest.mark.parametrize("reference", ["../outside.md", "escape-link.md", "missing.md"])
async def test_showme_unsafe_or_missing_path_still_uses_inference(
    client, session_with_files, monkeypatch, reference,
):
    sid, root = session_with_files
    (root / "escape-link.md").symlink_to(root.parent / "outside.md")
    captured = await _patch_showme(monkeypatch)
    response = await client.post(f"/api/sessions/{sid}/showme/resolve", json={"text": reference})
    assert response.status_code == 200, response.text
    assert response.json() == {"path": "answer.md", "message": None}
    assert captured["calls"] == 1
    assert "(none — this is the first interaction)" in captured["prompt"]
    viewed = await client.get(f"/api/sessions/{sid}/files", params={"path": reference})
    assert viewed.status_code == (404 if reference == "missing.md" else 403)


async def test_showme_exact_path_keeps_original_account_gate(client, session_with_files, monkeypatch):
    sid, _ = session_with_files
    _, other_bearer = await create_account(session_manager.db, username="viewer-other")

    async def forbidden_read(*args, **kwargs):
        pytest.fail("A foreign session must not read history or invoke a model")

    monkeypatch.setattr(session_manager.db, "load_messages", forbidden_read)
    monkeypatch.setattr("server.harness.Harness.run_oneshot", forbidden_read)
    response = await client.post(
        f"/api/sessions/{sid}/showme/resolve", json={"text": "plan.md"},
        headers={"Authorization": f"Bearer {other_bearer}"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Session not found"


@pytest.mark.parametrize("override, expected", [(None, "fast"), ("default", "default"), ("fast", "fast")])
async def test_showme_resolve_inherits_speed_independently(
    client, session_with_files, monkeypatch, override, expected,
):
    captured = await _patch_showme(monkeypatch)
    sid, _ = session_with_files
    session = session_manager.get_session(sid)
    session.backend = "codex"
    session.service_tier = override
    await session_manager.db.update_agent(session.agent_id, service_tier="fast")
    response = await client.post(
        f"/api/sessions/{sid}/showme/resolve", json={"text": "the plan"}, headers=client.headers,
    )
    assert response.status_code == 200, response.text
    assert captured["service_tier"] == expected
    assert captured["reasoning_effort"] is None
    assert captured["reasoning_effort"] is None


async def test_showme_resolve_attached_credential_is_normalized(
    client, session_with_files, monkeypatch
):
    """Regression guard: a session whose effective agent has an attached
    credential must trigger the credential resolver, so the harness receives
    a `HarnessCredential` (decrypted, right shape) rather than the raw DB row.
    """
    from datetime import datetime

    from server.crypto import encrypt
    from server.harness.events import HarnessCredential

    sid, _ = session_with_files
    db = session_manager.db
    now = datetime.now(UTC).isoformat()
    agent = await db.get_system_agent()
    secret = encrypt("sk-ant-test", await deps.get_user_manager().data_key_for(agent["user_id"]))
    await db.save_credential(
        credential_id="cred-1",
        backend="claude-code",
        label="test key",
        auth_type="api_key",
        secret_encrypted=secret,
        created_at=now,
        user_id=agent["user_id"],
    )
    # Attach the credential at the agent level — matches the production path
    # the bug was filed against.
    await db.update_agent(agent["id"], credential_id="cred-1")

    captured = await _patch_showme(monkeypatch)
    r = await client.post(
        f"/api/sessions/{sid}/showme/resolve",
        json={"text": "the plan"},
        headers=client.headers,
    )
    assert r.status_code == 200, r.text

    cred = captured["credential"]
    assert isinstance(cred, HarnessCredential), (
        f"resolver got {type(cred).__name__} — bug: raw DB row reaches the harness"
    )
    assert cred.backend == "claude-code"
    assert cred.auth_type == "api_key"
    # Secret must be the decrypted plaintext, not the on-disk ciphertext.
    assert cred.secret == "sk-ant-test"


