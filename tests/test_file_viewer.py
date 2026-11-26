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

import asyncio
import fcntl
import json
import os
import sys
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC
from io import FileIO
from pathlib import Path
from threading import Event, get_ident

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from server import deps, file_viewer
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
from server.mcp_identity import mint, register_run, revoke_run
from server.routers import files
from server.routers.files import router as files_router
from server.session_manager import session_manager
from server.sessions import Session, SessionManager
from server.workspace import paths_for
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
    (root / "answer.md").write_text("# Resolved answer")
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


async def test_viewer_disables_redirected_cwd_and_reopens_from_an_authorized_root(
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
        "path": meta.json()["path"], "source": meta.json()["source"],
        "canonical_path_id": meta.json()["canonical_path_id"],
    })
    assert stale.status_code == 403
    relative = await client.get(f"{url}/meta", params={"path": "plan.md"})
    assert relative.status_code == 403
    assert relative.json() == {"detail": "The session working directory is no longer available."}
    assert (await client.get(url, params={"path": str(root / "plan.md")})).status_code == 403
    reopened = await client.get(f"{url}/meta", params={"path": str(replacement_root / "plan.md")})
    assert reopened.status_code == 200 and reopened.json()["path"] == "replacement-wd/plan.md"
    assert reopened.json()["source"] == str(root.parent)
    replacement = await client.get(url, params={
        "path": reopened.json()["path"], "source": reopened.json()["source"],
        "canonical_path_id": reopened.json()["canonical_path_id"],
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
    for path in ("sub/../../outside.md", "escape.md"):
        assert (await client.get(url, params={"path": str(path)})).status_code == 403
    outside = str(root.parent / "outside.md")
    assert (await client.get(url, params={"path": outside})).status_code == 200
    for path in ("sub/../../outside.md", outside, "escape.md"):
        assert (await client.get(url, params={"path": path, "source": meta.json()["source"]})).status_code == 403
        assert (await client.get(url, params={"path": path, "canonical_path_id": canonical_path_id})).status_code == 403
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
    assert partial.headers["cache-control"] == "no-store"

    matching = await client.get(url, params={"path": name}, headers={"Range": "bytes=-8", "If-Range": preview.headers["etag"]})
    assert matching.status_code == 206 and matching.content == preview.content[-8:]
    stale = await client.get(url, params={"path": name}, headers={"Range": "bytes=0-7", "If-Range": '"stale"'})
    assert stale.status_code == 200 and stale.content == preview.content
    multiple = await client.get(url, params={"path": name}, headers={"Range": "bytes=0-7,14-18"})
    assert multiple.status_code == 206
    assert multiple.headers["content-type"].startswith("multipart/byteranges;")
    assert len(multiple.content) == int(multiple.headers["content-length"])
    assert b"\r\n\r\n%PDF-1.4\r\n" in multiple.content
    assert b"\r\n\r\n" + preview.content[14:19] + b"\r\n" in multiple.content
    malformed = await client.get(url, params={"path": name}, headers={"Range": "items=0-1"})
    assert malformed.status_code == 400 and malformed.headers["cache-control"] == "no-store"
    unsatisfiable = await client.get(url, params={"path": name}, headers={"Range": "bytes=10000-"})
    assert unsatisfiable.status_code == 416 and unsatisfiable.headers["cache-control"] == "no-store"
    assert unsatisfiable.headers["content-range"] == f"bytes */{len(preview.content)}"


@pytest.mark.parametrize("path", ["plan.md", "main.py", "logo.png", "../outside.md", "no-such.pdf", "huge.md", "blob.bin"])
async def test_preview_preserves_non_pdf_behavior_and_path_gates(client, session_with_files, path):
    sid, _ = session_with_files
    url = f"/api/sessions/{sid}/files"
    default = await client.get(url, params={"path": path})
    preview = await client.get(url, params={"path": path, "preview": "true"})
    assert preview.status_code == default.status_code
    assert preview.content == default.content
    assert preview.headers.get("content-disposition") == default.headers.get("content-disposition")


@pytest.mark.parametrize(("path", "status"), [("../outside.md", 403), ("no-such.pdf", 404)])
async def test_pdf_frame_exposes_the_read_error_status(client, session_with_files, path, status):
    sid, _ = session_with_files
    url = f"/api/sessions/{sid}/files"
    params = {"path": path, "preview": "true"}
    ordinary = await client.get(url, params=params)
    framed = await client.get(url, params={**params, "frame": "true"})

    assert ordinary.status_code == framed.status_code == status
    assert framed.headers["cache-control"] == "no-store"
    assert framed.json() == {**ordinary.json(), "status": status}


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


async def test_viewer_uses_selected_source_for_document_and_reopens_after_revocation(client, session_with_files):
    sid, cwd = session_with_files
    user_id = session_manager.get_session(sid).user_id
    repo = cwd.with_name("repo")
    team = repo / "team"
    team.mkdir(parents=True)
    report = team / "plan.md"
    report.write_text("# Repository report")
    diagram = b"\x89PNG\r\n\x1a\n" + b"repository diagram"
    (repo / "diagram.png").write_bytes(diagram)
    await session_manager.db.update_user_field(
        user_id, extra_roots=json.dumps(list(map(str, [cwd, cwd / "sub", repo, team]))),
    )
    url = f"/api/sessions/{sid}/files"

    sources = await client.get(f"{url}/sources")
    assert sources.status_code == 200
    assert sources.headers["cache-control"] == "no-store"
    assert sources.json()["cwd"] == str(cwd)
    assert {str(cwd), str(repo), str(team)} <= set(sources.json()["roots"])

    browse = await client.get(f"{url}/browse", params={"source": str(repo)})
    assert browse.headers["cache-control"] == "no-store"
    assert {(e["path"], e["kind"]) for e in browse.json()["entries"]} == {
        ("team", "directory"), ("diagram.png", "file"),
    }
    query = {"source": str(repo), "query": "PLAN"}
    matches = await client.get(f"{url}/find", params=query)
    assert matches.headers["cache-control"] == "no-store"
    assert [e["path"] for e in matches.json()["entries"]] == ["team/plan.md"]

    original = await client.get(f"{url}/meta", params={"path": "plan.md"})
    assert original.status_code == 200
    assert original.json()["source"] == str(cwd)
    assert original.json()["absolute_path"] == str(cwd / "plan.md")
    nested = await client.get(f"{url}/meta", params={"path": str(cwd / "sub" / "note.txt")})
    assert nested.status_code == 200 and nested.json()["source"] == str(cwd)

    inferred = await client.get(f"{url}/meta", params={"path": str(report)})
    assert inferred.status_code == 200
    assert inferred.json()["source"] == str(team)
    assert inferred.json()["path"] == "plan.md"

    selected = await client.get(f"{url}/meta", params={"path": matches.json()["entries"][0]["path"], "source": str(repo)})
    assert selected.status_code == 200
    meta = selected.json()
    assert meta["source"] == str(repo)
    assert meta["path"] == "team/plan.md"
    assert meta["absolute_path"] == str(report)
    assert selected.headers["cache-control"] == "no-store"
    body = {"source": meta["source"], "path": meta["path"], "canonical_path_id": meta["canonical_path_id"]}
    directory = meta["canonical_directory"]
    asset = {"path": "../diagram.png", "source": meta["source"],
             "source_directory": directory["path"], "source_directory_id": directory["path_id"]}
    response = await client.get(url, params=body)
    assert response.status_code == 200 and response.text == "# Repository report"
    response = await client.get(url, params=asset)
    assert response.status_code == 200 and response.content == diagram
    assert (await client.get(url, params=body | {"source": str(team), "path": "plan.md"})).status_code == 409
    alternate_asset = asset | {"source": str(team), "source_directory": ".", "path": "plan.md"}
    assert (await client.get(url, params=alternate_asset)).status_code == 409

    await session_manager.db.update_user_field(user_id, extra_roots=json.dumps(list(map(str, [cwd, team]))))
    assert str(repo) not in (await client.get(f"{url}/sources")).json()["roots"]
    for endpoint, params in (("browse", {"source": str(repo)}), ("find", query)):
        denied = await client.get(f"{url}/{endpoint}", params=params)
        assert denied.status_code == 403
        assert denied.headers["cache-control"] == "no-store"
    for params in (body, asset):
        denied = await client.get(url, params=params)
        assert denied.status_code == 403
        assert denied.headers["cache-control"] == "no-store"
        assert denied.json()["detail"] == "The file source is no longer available."

    reopened = await client.get(f"{url}/meta", params={"path": str(report)})
    assert reopened.status_code == 200
    assert reopened.json()["source"] == str(team)


def _observe_picker_scans(monkeypatch, source, on_entry):
    scandir = os.scandir

    class ObservedScan:
        def __init__(self, scan):
            self.scan = scan

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.scan.close()

        def __iter__(self):
            return self

        def __next__(self):
            entry = next(self.scan)
            on_entry()
            return entry

    def observe(path):
        scan = scandir(path)
        if isinstance(path, str) and path.startswith("/proc/self/fd/") and Path(path).readlink().is_relative_to(source):
            return ObservedScan(scan)
        return scan

    monkeypatch.setattr(os, "scandir", observe)


@pytest.mark.parametrize(("limit", "expected_entries", "expected_hits"), [
    ("entries", 5_000, 0), ("results", 100, 100), ("time", 8, 7),
])
async def test_file_picker_reports_and_stops_at_each_search_limit(
    client, session_with_files, monkeypatch, limit, expected_entries, expected_hits,
):
    sid, cwd = session_with_files
    large = cwd / "large"
    large.mkdir()
    for i in range(5_100 if limit == "entries" else 120):
        (large / f"report-{i:04}.md").touch()
    await session_manager.db.update_user_field(
        session_manager.get_session(sid).user_id, extra_roots=json.dumps([str(cwd), str(large)]),
    )
    examined = 0

    def count():
        nonlocal examined
        examined += 1

    _observe_picker_scans(monkeypatch, large, count)
    # Advance by the cost of a directory entry so the timeout assertion is
    # independent of worker load while the scanner still visits real files.
    monkeypatch.setattr(file_viewer, "monotonic", lambda: examined * 0.026 if limit == "time" else 0)
    response = await client.get(f"/api/sessions/{sid}/files/find", params={
        "source": str(large), "query": "missing" if limit == "entries" else "REPORT",
    })
    assert response.status_code == 200
    assert response.json()["truncated"] is True
    assert examined == expected_entries
    assert len(response.json()["entries"]) == expected_hits


@pytest.mark.parametrize("endpoint", ["find", "browse"])
async def test_file_picker_disconnect_stops_scan(client, session_with_files, monkeypatch, endpoint):
    sid, cwd = session_with_files
    for i in range(120):
        (cwd / f"report-{i:04}.md").touch()
    first_entry = asyncio.Event()
    resume_scan = Event()
    loop = asyncio.get_running_loop()
    examined = 0

    def pause_after_first():
        nonlocal examined
        examined += 1
        if examined == 1:
            loop.call_soon_threadsafe(first_entry.set)
            assert resume_scan.wait(timeout=3)

    _observe_picker_scans(monkeypatch, cwd, pause_after_first)
    monkeypatch.setattr(file_viewer, "monotonic", lambda: 0)

    async def disconnect_during_scan(scope, _receive, send):
        async def receive():
            await first_entry.wait()
            loop.call_soon(resume_scan.set)
            return {"type": "http.disconnect"}

        await app(scope, receive, send)

    async with AsyncClient(transport=ASGITransport(app=disconnect_during_scan), base_url="http://test",
                           headers=client.headers) as disconnected:
        async with asyncio.timeout(5):
            await disconnected.get(f"/api/sessions/{sid}/files/{endpoint}", params={"source": str(cwd), "query": "missing"})
    assert examined == 1


async def test_file_picker_disconnect_during_authorization_retires_the_request(client, session_with_files, monkeypatch):
    sid, cwd = session_with_files
    authorizing = asyncio.Event()
    exited = asyncio.Event()

    async def wait_for_authority(_token):
        authorizing.set()
        try:
            await asyncio.Future()
        finally:
            exited.set()

    monkeypatch.setattr(files, "resolve_viewer_authority", wait_for_authority)

    async def disconnect_during_authorization(scope, _receive, send):
        async def receive():
            await authorizing.wait()
            return {"type": "http.disconnect"}

        await app(scope, receive, send)

    async with AsyncClient(transport=ASGITransport(app=disconnect_during_authorization), base_url="http://test",
                           headers=client.headers) as disconnected:
        async with asyncio.timeout(5):
            response = await disconnected.get(f"/api/sessions/{sid}/files/find", params={
                "source": str(cwd), "query": "plan",
            })
    assert response.status_code == 499
    assert response.headers["cache-control"] == "no-store"
    assert exited.is_set()


async def test_file_picker_reports_skipped_entries_and_cannot_browse_linked_directories(client, session_with_files):
    sid, cwd = session_with_files
    (cwd / "alias").symlink_to(cwd / "sub", target_is_directory=True)
    (cwd / "plan-alias.md").symlink_to(cwd / "plan.md")
    os.mkfifo(cwd / "plan-pipe.md")
    (cwd / "odd-names").mkdir()
    with open(os.fsencode(cwd / "odd-names") + b"/plan-\xff.md", "wb"):
        pass
    url = f"/api/sessions/{sid}/files"
    params = {"source": str(cwd)}
    response = await client.get(f"{url}/find", params=params | {"query": "plan"})
    assert response.json() == {
        "entries": [{"name": "plan.md", "path": "plan.md", "kind": "file"}], "skipped": True, "truncated": False,
    }
    browse = await client.get(f"{url}/browse", params=params)
    assert browse.json()["skipped"] is True
    assert {"alias", "plan-alias.md", "plan-pipe.md"}.isdisjoint(entry["name"] for entry in browse.json()["entries"])
    assert (await client.get(f"{url}/browse", params=params | {"directory": "alias"})).status_code == 404
    assert (await client.get(f"{url}/browse", params=params | {"directory": "../"})).status_code == 403
    odd_names = await client.get(f"{url}/browse", params=params | {"directory": "odd-names"})
    assert odd_names.json() == {"entries": [], "skipped": True, "truncated": False}


async def test_viewer_detects_document_parent_replacement_but_allows_file_edits(client, session_with_files):
    sid, cwd = session_with_files
    url = f"/api/sessions/{sid}/files"
    docs = cwd / "docs"
    docs.mkdir()
    report = docs / "plan.md"
    report.write_text("original report")
    (docs / "note.txt").write_text("original note")
    meta = await client.get(f"{url}/meta", params={"path": "docs/plan.md"})
    assert meta.status_code == 200
    body = {key: meta.json()[key] for key in ("source", "path", "canonical_path_id")}
    directory = meta.json()["canonical_directory"]
    asset = {"path": "note.txt", "source": body["source"],
             "source_directory": directory["path"], "source_directory_id": directory["path_id"]}

    report.write_text("updated report")
    updated = await client.get(url, params=body)
    assert updated.status_code == 200 and updated.text == "updated report"
    docs.rename(cwd / "retired")
    docs.mkdir()
    report.write_text("replacement report")
    (docs / "note.txt").write_text("replacement note")
    for params in (body, asset):
        stale = await client.get(url, params=params)
        assert stale.status_code == 409
        assert stale.headers["cache-control"] == "no-store"


async def test_absolute_file_sources_match_path_components_before_resolving_links(client, session_with_files):
    sid, cwd = session_with_files
    user_id = session_manager.get_session(sid).user_id
    repo = cwd.with_name("repo")
    repo.mkdir()
    (repo / "plan.md").write_text("repository file")
    alias = cwd.with_name("alias")
    alias.symlink_to(repo, target_is_directory=True)
    cwd_alias = cwd.with_name("cwd-alias")
    cwd_alias.symlink_to(cwd, target_is_directory=True)
    (cwd / "other.md").symlink_to(repo / "plan.md")
    neighbor = repo.with_name("repo-other")
    neighbor.mkdir()
    (neighbor / "plan.md").write_text("outside file")
    await session_manager.db.update_user_field(user_id, extra_roots=json.dumps(list(map(str, [cwd, repo]))))
    url = f"/api/sessions/{sid}/files"
    for target in (alias / "plan.md", cwd_alias / "plan.md", cwd / "other.md", neighbor / "plan.md"):
        denied = await client.get(url, params={"path": str(target)})
        assert denied.status_code == 403
        assert denied.json() == {"detail": "The file path is outside the authorized source."}
        assert denied.headers["cache-control"] == "no-store"
    allowed = await client.get(url, params={"path": str(repo / "plan.md")})
    assert allowed.status_code == 200 and allowed.text == "repository file"

    team = repo / "team"
    team.mkdir()
    await session_manager.db.update_user_field(user_id, extra_roots=json.dumps(list(map(str, [cwd, repo, team]))))
    other = repo / "other"
    other.mkdir()
    (other / "plan.md").write_text("other repository file")
    team.rename(repo / "retired-team")
    team.symlink_to(other, target_is_directory=True)
    assert (await client.get(url, params={"path": str(team / "plan.md")})).status_code == 403
    reopened = await client.get(url, params={"path": str(other / "plan.md")})
    assert reopened.status_code == 200 and reopened.text == "other repository file"

    repo.rename(repo.with_name("retired-repo"))
    repo.symlink_to(cwd, target_is_directory=True)
    assert (await client.get(url, params={"source": str(repo), "path": "plan.md"})).status_code == 403
    assert (await client.get(url, params={"path": str(repo / "plan.md")})).status_code == 403


@pytest.mark.parametrize("redirect", ["source", "ancestor"])
async def test_implicit_open_rejects_a_saved_source_through_a_dangling_link(client, session_with_files, redirect):
    sid, cwd = session_with_files
    user_id = session_manager.get_session(sid).user_id
    repo = cwd.with_name("repo")
    branch = repo / "branch"
    team = branch / "team"
    team.mkdir(parents=True)
    (repo / "plan.md").write_text("repository report")
    await session_manager.db.update_user_field(user_id, extra_roots=json.dumps(list(map(str, [cwd, repo, team]))))
    replaced = team if redirect == "source" else branch
    replaced.rename(replaced.with_name(replaced.name + "-retired"))
    replaced.symlink_to(replaced.with_name("missing"), target_is_directory=True)

    url = f"/api/sessions/{sid}/files"
    denied = await client.get(url, params={"path": str(team / ".." / ".." / "plan.md")})
    assert denied.status_code == 403
    assert denied.headers["cache-control"] == "no-store"
    assert denied.json() == {"detail": "The file source is no longer available."}
    reopened = await client.get(url, params={"path": str(repo / "plan.md")})
    assert reopened.status_code == 200 and reopened.text == "repository report"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can traverse the directory with its execute bits cleared")
async def test_viewer_keeps_reading_cwd_when_another_source_is_not_traversable(client, session_with_files, monkeypatch):
    sid, cwd = session_with_files
    owner = session_manager.get_session(sid).user_id
    parent = cwd.with_name("locked")
    extra = parent / "repo"
    extra.mkdir(parents=True)
    await session_manager.db.update_user_field(owner, extra_roots=json.dumps(list(map(str, [cwd, extra]))))
    probe = Path.is_symlink

    def report_permission_error(path):
        # Model pathlib's permission error on older supported interpreters.
        if path == extra:
            raise PermissionError(path)
        return probe(path)

    monkeypatch.setattr(Path, "is_symlink", report_permission_error)
    parent.chmod(0o600)
    try:
        url = f"/api/sessions/{sid}/files"
        response = await client.get(url, params={"path": "plan.md"})
        assert response.status_code == 200 and response.text == "# plan\nstep one"
        denied = await client.get(url, params={"source": str(extra), "path": "plan.md"})
        assert denied.status_code == 403
        assert denied.json() == {"detail": "The file source is no longer available."}
    finally:
        parent.chmod(0o700)


@pytest.mark.parametrize("redirect", ["workspace", "account"])
async def test_viewer_never_implicitly_grants_a_redirected_account_workspace(client, session_with_files, redirect):
    sid, _ = session_with_files
    user_id = session_manager.get_session(sid).user_id
    other, token = await create_account(session_manager.db, username="other-owner")
    workspace = paths_for(user_id).workspace
    other_workspace = paths_for(other["id"]).workspace
    (workspace / "plan.md").write_text("workspace file")
    (other_workspace / "plan.md").write_text("another account's file")
    url = f"/api/sessions/{sid}/files"
    meta = await client.get(f"{url}/meta", params={"path": str(workspace / "plan.md")})
    assert meta.status_code == 200 and meta.json()["source"] == str(workspace)

    for archived in (False, True):
        if archived:
            await session_manager.archive_session(sid, replace=False)
        denied = await client.get(url, params={"path": "plan.md"}, headers={"Authorization": f"Bearer {token}"})
        assert denied.status_code == 404
        assert denied.json() == {"detail": "Session not found"}
        assert denied.headers["cache-control"] == "no-store"

    original, destination = (workspace, other_workspace) if redirect == "workspace" else (workspace.parent, other_workspace.parent)
    original.rename(original.with_name(original.name + "-retired"))
    original.symlink_to(destination, target_is_directory=True)
    for params in ({"path": str(workspace / "plan.md")}, {"path": "plan.md", "source": str(workspace)},
                   {"path": str(other_workspace / "plan.md")}):
        denied = await client.get(url, params=params)
        assert denied.status_code == 403
        assert "another account" not in denied.text
        assert str(workspace) not in denied.text and str(other_workspace) not in denied.text
    assert (await client.get(url, params={"path": "plan.md"})).status_code == 200


async def test_viewer_without_an_account_has_only_the_original_session_source(tmp_path, monkeypatch):
    cwd = tmp_path / "legacy-cwd"
    cwd.mkdir()
    (cwd / "plan.md").write_text("# Legacy report")
    (tmp_path / "outside.md").write_text("outside report")
    manager = SessionManager()
    legacy_session = Session(id="legacy", name="Legacy session", working_dir=str(cwd))
    manager.sessions[legacy_session.id] = legacy_session
    legacy_app = FastAPI()
    legacy_app.include_router(files_router)
    legacy_app.dependency_overrides[deps.get_session_manager] = lambda: manager

    async def legacy_authority(token: str) -> deps.ViewerAuthority | None:
        return deps.ViewerAuthority(None, []) if token == "legacy-token" else None

    monkeypatch.setattr(files, "resolve_viewer_authority", legacy_authority)
    async with AsyncClient(transport=ASGITransport(app=legacy_app), base_url="http://test",
                           headers={"Authorization": "Bearer legacy-token"}) as legacy:
        url = f"/api/sessions/{legacy_session.id}/files"
        relative = await legacy.get(url, params={"path": "plan.md"})
        assert relative.status_code == 200 and relative.text == "# Legacy report"
        absolute = await legacy.get(f"{url}/meta", params={"path": str(cwd / "plan.md")})
        assert absolute.status_code == 200 and absolute.json()["source"] == str(cwd)
        outside = await legacy.get(url, params={"path": str(cwd.parent / "outside.md")})
        assert outside.status_code == 403


@pytest.mark.parametrize("change", ["grant", "token", "account"])
async def test_viewer_checks_authority_after_waiting_for_file_worker(client, session_with_files, monkeypatch, change):
    sid, _ = session_with_files
    user_id = session_manager.get_session(sid).user_id
    waiting = asyncio.Event()
    admitted = asyncio.Event()
    run_in_threadpool = files.run_in_threadpool

    async def wait_for_worker(*args, **kwargs):
        waiting.set()
        await admitted.wait()
        return await run_in_threadpool(*args, **kwargs)

    monkeypatch.setattr(files, "run_in_threadpool", wait_for_worker)
    async with asyncio.timeout(5):
        async with asyncio.TaskGroup() as active:
            pending = active.create_task(client.get(f"/api/sessions/{sid}/files", params={"path": "plan.md"}))
            await waiting.wait()
            if change == "grant":
                await session_manager.db.update_user_field(user_id, extra_roots="[]")
            elif change == "token":
                assert (await client.post("/api/auth/logout")).status_code == 204
            else:
                await deps.get_user_manager().set_disabled(user_id, True)
            admitted.set()
    response = pending.result()
    assert response.headers["cache-control"] == "no-store"
    if change == "grant":
        assert response.status_code == 403
        assert response.json() == {"detail": "The session working directory is no longer available."}
    else:
        assert response.status_code == 401
        assert response.json() == {"detail": "Invalid token"}


async def test_viewer_does_not_combine_a_revoked_bearer_with_a_new_grant(client, session_with_files, monkeypatch):
    sid, cwd = session_with_files
    repo = cwd.with_name("newly-granted")
    repo.mkdir()
    (repo / "plan.md").write_text("newly granted content")
    user_id = session_manager.get_session(sid).user_id
    await session_manager.db.update_user_field(user_id, extra_roots=json.dumps([str(cwd)]))
    users = deps.get_user_manager()
    old_token = client.headers["Authorization"].removeprefix("Bearer ")
    captured = asyncio.Event()
    continue_read = asyncio.Event()
    resolve_token = users.resolve_token

    async def read_then_wait(token, *, touch=True):
        user = await resolve_token(token, touch=touch)
        if token == old_token and user is not None:
            captured.set()
            await continue_read.wait()
        return user

    monkeypatch.setattr(users, "resolve_token", read_then_wait)
    url = f"/api/sessions/{sid}/files"
    params = {"source": str(repo), "path": "plan.md"}
    async with asyncio.timeout(5):
        async with asyncio.TaskGroup() as active:
            pending = active.create_task(client.get(url, params=params))
            await captured.wait()
            await users.revoke_token(old_token)
            await session_manager.db.update_user_field(user_id, extra_roots=json.dumps(list(map(str, [cwd, repo]))))
            continue_read.set()

    denied = pending.result()
    assert denied.status_code == 403
    assert denied.headers["cache-control"] == "no-store"
    assert denied.json() == {"detail": "The file source is no longer available."}
    new_token = await users.issue_token(user_id)
    reopened = await client.get(url, params=params, headers={"Authorization": f"Bearer {new_token}"})
    assert reopened.status_code == 200 and reopened.text == "newly granted content"


async def test_viewer_checks_mcp_lease_after_reading_the_current_owner_grants(client, session_with_files, monkeypatch):
    sid, _ = session_with_files
    user_id = session_manager.get_session(sid).user_id
    lease = f"viewer-{sid}"
    token = mint(sid, user_id=user_id, run_lease_id=lease)
    headers = {"Authorization": f"Bearer {token}"}
    url = f"/api/sessions/{sid}/files"
    register_run(lease, sid, user_id, None)
    try:
        allowed = await client.get(url, params={"path": "plan.md"}, headers=headers)
        assert allowed.status_code == 200 and allowed.text == "# plan\nstep one"

        database = session_manager.db
        get_user = database.get_user

        async def read_then_retire(owner):
            user = await get_user(owner)
            revoke_run(lease)
            return user

        monkeypatch.setattr(database, "get_user", read_then_retire)
        denied = await client.get(url, params={"path": "plan.md"}, headers=headers)
        assert denied.status_code == 401
        assert denied.headers["cache-control"] == "no-store"
        assert denied.json() == {"detail": "Invalid token"}
    finally:
        revoke_run(lease)


@pytest.mark.parametrize("archived", [False, True])
async def test_viewer_checks_current_cwd_grant_for_each_request(client, session_with_files, archived):
    sid, cwd = session_with_files
    session = session_manager.get_session(sid)
    if archived:
        await session_manager.archive_session(sid, replace=False)

    url = f"/api/sessions/{sid}/files"
    meta = await client.get(f"{url}/meta", params={"path": "plan.md"})
    assert meta.status_code == 200
    directory = meta.json()["canonical_directory"]

    await session_manager.db.update_user_field(session.user_id, extra_roots="[]")
    refreshed = await client.get(f"{url}/sources")
    assert refreshed.status_code == 200
    assert refreshed.headers["cache-control"] == "no-store"
    assert refreshed.json()["cwd"] is None
    assert str(cwd) not in refreshed.json()["roots"]
    requests = [
        (f"{url}/meta", {"path": "plan.md"}, {}),
        (url, {"path": "plan.md", "canonical_path_id": meta.json()["canonical_path_id"]}, {}),
        (url, {"path": "logo.png", "source_directory": directory["path"],
               "source_directory_id": directory["path_id"]}, {}),
        (url, {"path": "plan.md"}, {}),
        (url, {"path": "doc.pdf", "preview": "true"}, {"Range": "bytes=0-7"}),
    ]
    for endpoint, params, headers in requests:
        response = await client.get(endpoint, params=params, headers=headers)
        assert response.status_code == 403
        assert response.headers.get("cache-control") == "no-store"
        assert response.json()["detail"] == "The session working directory is no longer available."
    assert meta.headers.get("cache-control") == "no-store"


@pytest.mark.parametrize("mode", ["body", "resource", "download", "range"])
async def test_viewer_response_keeps_checked_bytes_when_file_is_replaced(client, session_with_files, mode):
    sid, root = session_with_files
    url = f"/api/sessions/{sid}/files"
    params = {"path": "doc.pdf" if mode == "range" else "plan.md"}
    headers = {}
    meta = (await client.get(f"{url}/meta", params=params)).json()
    if mode == "body":
        params["canonical_path_id"] = meta["canonical_path_id"]
    if mode == "resource":
        directory = meta["canonical_directory"]
        params.update(source_directory=directory["path"], source_directory_id=directory["path_id"])
    if mode == "range":
        params["preview"] = "true"
        headers["Range"] = "bytes=0-7"

    target = root / params["path"]
    original = target.read_bytes()
    replacement = root / "replacement"
    replacement.write_bytes(b"x" * len(original))
    viewer_app = FastAPI()
    viewer_app.include_router(files_router)

    async def replace_after_response_start(scope, receive, send):
        scope["extensions"] = {"http.response.pathsend": {}}

        async def send_after_replace(message):
            if message["type"] == "http.response.start":
                replacement.replace(target)
            await send(message)

        await viewer_app(scope, receive, send_after_replace)

    transport = ASGITransport(app=replace_after_response_start)
    async with AsyncClient(transport=transport, base_url="http://test", headers=client.headers) as reader:
        response = await reader.get(url, params=params, headers=headers)

    assert response.status_code == (206 if mode == "range" else 200)
    assert response.content == (original[:8] if mode == "range" else original)
    assert response.headers.get("cache-control") == "no-store"


async def test_viewer_does_not_redirect_the_cwd_grant_to_another_source(client, session_with_files):
    sid, root = session_with_files
    session = session_manager.get_session(sid)
    other = root.with_name("other-grant")
    other.mkdir()
    (other / "plan.md").write_text("another source")
    await session_manager.db.update_user_field(session.user_id, extra_roots=json.dumps([str(root), str(other)]))
    url = f"/api/sessions/{sid}/files"
    assert (await client.get(f"{url}/meta", params={"path": "plan.md"})).status_code == 200

    root.rename(root.with_name("previous-directory"))
    root.symlink_to(other, target_is_directory=True)

    for endpoint in (url, f"{url}/meta"):
        response = await client.get(endpoint, params={"path": "plan.md"})
        assert response.status_code == 403


@pytest.mark.parametrize("mode", ["body", "download", "range"])
async def test_viewer_rejects_outside_redirect_at_the_checked_open(client, session_with_files, monkeypatch, mode):
    sid, root = session_with_files
    url = f"/api/sessions/{sid}/files"
    name = "doc.pdf" if mode == "range" else "plan.md"
    params = {"path": name}
    headers = {}
    if mode == "body":
        meta = await client.get(f"{url}/meta", params=params)
        assert meta.status_code == 200
        params["canonical_path_id"] = meta.json()["canonical_path_id"]
    if mode == "range":
        params["preview"] = "true"
        headers["Range"] = "bytes=0-7"

    target = root / name
    # The redirected entry points to a missing fixture outside the session root.
    unrelated = root.parent / "unavailable" / name
    open_fd = os.open
    redirected = False

    def open_changed_entry(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal redirected
        if path == name and dir_fd is not None and flags & os.O_PATH and not redirected:
            target.unlink()
            target.symlink_to(unrelated)
            redirected = True
        return open_fd(path, flags, mode, dir_fd=dir_fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", open_changed_entry)
        response = await client.get(url, params=params, headers=headers)
    assert redirected
    assert response.status_code == 409
    assert "file path changed" in response.json()["detail"]
    assert "content-disposition" not in response.headers


@pytest.mark.parametrize("kind", ["file", "resource"])
async def test_viewer_rechecks_moved_directory_after_open(client, session_with_files, monkeypatch, kind):
    sid, root = session_with_files
    url = f"/api/sessions/{sid}/files"
    document = root / "sub" / "report.md"
    document.write_text("# report")
    meta = await client.get(f"{url}/meta", params={"path": "sub/report.md"})
    directory = meta.json()["canonical_directory"]
    params = {"path": "sub/note.txt"}
    if kind == "resource":
        params = {"path": "../plan.md", "source_directory": directory["path"],
                  "source_directory_id": directory["path_id"]}
    open_fd = os.open

    def move_open_directory(path, flags, mode=0o777, *, dir_fd=None):
        fd = open_fd(path, flags, mode, dir_fd=dir_fd)
        if path == "sub" and dir_fd is not None and flags & os.O_DIRECTORY:
            document.parent.rename(root / "retired")
        return fd

    monkeypatch.setattr(os, "open", move_open_directory)
    response = await client.get(url, params=params)
    assert response.status_code == 409
    assert ("document directory changed" if kind == "resource" else "file path changed") in response.json()["detail"]


@pytest.mark.parametrize("change", ["grow", "truncate"])
async def test_viewer_snapshot_uses_checked_file_size(client, session_with_files, monkeypatch, change):
    sid, root = session_with_files
    path = root / "plan.md"
    original = path.read_bytes()
    fstat = os.fstat
    changed = False

    def change_checked_file_size(fd):
        nonlocal changed
        info = fstat(fd)
        if (
            not changed
            and os.readlink(f"/proc/self/fd/{fd}") == str(path)
            and not fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_PATH
        ):
            changed = True
            if change == "grow":
                with path.open("ab") as stream:
                    stream.write(b"x" * MAX_FILE_BYTES)
            else:
                path.write_bytes(b"")
        return info

    with monkeypatch.context() as patch:
        patch.setattr(os, "fstat", change_checked_file_size)
        response = await client.get(f"/api/sessions/{sid}/files", params={"path": path.name})
    assert changed
    if change == "grow":
        assert response.status_code == 200 and response.content == original
        assert int(response.headers["content-length"]) == len(original)
    else:
        assert response.status_code == 409 and "size changed" in response.json()["detail"]


async def test_viewer_retries_short_reads_from_the_same_file(client, session_with_files, monkeypatch):
    sid, root = session_with_files
    original = (root / "plan.md").read_bytes()

    class ShortReadFile(FileIO):
        def read(self, size=-1):
            return super().read(3 if size < 0 else min(size, 3))

        def readinto(self, buffer):
            return super().readinto(memoryview(buffer)[:3])

    monkeypatch.setattr(file_viewer, "FileIO", ShortReadFile)
    response = await client.get(f"/api/sessions/{sid}/files", params={"path": "plan.md"})
    assert response.status_code == 200
    assert response.content == original


async def test_cancelling_viewer_send_leaves_no_open_source_handles(client, session_with_files):
    sid, root = session_with_files
    body_send_started = False

    def root_handles():
        paths = []
        for fd in Path("/proc/self/fd").iterdir():
            try:
                path = fd.readlink()
            except FileNotFoundError:
                continue
            if path.is_relative_to(root):
                paths.append(path)
        return paths

    async def cancel_send(scope, receive, send):
        async def cancel_at_body(message):
            nonlocal body_send_started
            if message["type"] == "http.response.body":
                body_send_started = True
                assert not root_handles()
                raise asyncio.CancelledError
            await send(message)

        await app(scope, receive, cancel_at_body)

    async with AsyncClient(transport=ASGITransport(app=cancel_send), base_url="http://test", headers=client.headers) as reader:
        with pytest.raises(asyncio.CancelledError):
            await reader.get(f"/api/sessions/{sid}/files", params={"path": "plan.md"})
    assert body_send_started and not root_handles()


async def test_viewer_reads_exact_budget_and_serves_html_as_source(client, session_with_files):
    sid, root = session_with_files
    source = b"<script>document.write('not rendered')</script>"
    body = source.ljust(MAX_FILE_BYTES, b" ")
    (root / "example.html").write_bytes(body)
    response = await client.get(f"/api/sessions/{sid}/files", params={"path": "example.html", "preview": "true"})
    assert response.status_code == 200 and response.content == body
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert response.headers["content-disposition"] == 'attachment; filename="example.html"'


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


async def _patch_showme(monkeypatch, output='{"path":"answer.md"}', *, before_reply=None):
    """Capture the resolved one-shot context at the harness boundary."""
    captured: dict = {}

    async def _fake_oneshot(ctx, **kwargs):
        with await ctx.authorize_spawn():
            captured["calls"] = captured.get("calls", 0) + 1
        captured["credential"] = ctx.credential
        captured["model"] = ctx.model
        captured["reasoning_effort"] = ctx.reasoning_effort
        captured["service_tier"] = ctx.service_tier
        captured["working_dir"] = ctx.working_dir
        captured["prompt"] = ctx.prompt
        if before_reply is not None:
            await before_reply()
        return output

    from server.harness import get_harness

    for backend in ("claude-code", "codex"):
        monkeypatch.setattr(get_harness(backend), "run_oneshot", _fake_oneshot)
    return captured


async def test_showme_initial_directory_check_uses_a_worker_and_closes_on_cancellation(
    client, session_with_files, monkeypatch,
):
    sid, _ = session_with_files
    captured = await _patch_showme(monkeypatch)
    blocked, release, closed = Event(), Event(), Event()
    request_thread = get_ident()
    opening_threads = []
    open_directory = files.open_session_working_directory

    @contextmanager
    def observe_directory(*args, **kwargs):
        if kwargs.get("expected") is not None:
            with open_directory(*args, **kwargs) as fd:
                yield fd
            return
        opening_threads.append(get_ident())
        blocked.set()
        assert release.wait(3)
        try:
            with open_directory(*args, **kwargs) as fd:
                yield fd
        finally:
            closed.set()

    monkeypatch.setattr(files, "open_session_working_directory", observe_directory)
    request = asyncio.create_task(client.post(f"/api/sessions/{sid}/showme/resolve", json={"text": "the report"}))
    try:
        assert await asyncio.to_thread(blocked.wait, 3)
        assert len(opening_threads) == 1 and opening_threads[0] != request_thread
        sources = await asyncio.wait_for(client.get(f"/api/sessions/{sid}/files/sources"), 2)
        assert sources.status_code == 200
        request.cancel()
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert closed.is_set()
        assert captured == {}
    finally:
        release.set()
        await asyncio.gather(request, return_exceptions=True)


@pytest.mark.parametrize("when", ["before_request", "before_spawn", "before_preparation"])
async def test_showme_refuses_to_spawn_with_revoked_cwd(client, session_with_files, monkeypatch, when):
    from server.harness import Harness, get_harness

    sid, cwd = session_with_files
    session = session_manager.get_session(sid)
    db = session_manager.db
    workspace = paths_for(session.user_id).workspace
    (workspace / "recovery.md").write_text("# Available from workspace")
    spawn_calls = []
    real_spawn = asyncio.create_subprocess_exec

    async def observe_spawn(*argv, **kwargs):
        spawn_calls.append(kwargs.get("cwd"))
        return await real_spawn(*argv, **kwargs)

    def render(ctx):
        assert ctx.working_dir == str(cwd)
        if when == "before_preparation":
            cwd.rename(cwd.with_name("retired"))
        return [sys.executable, "-c", "print('harmless fixture')"], {"cwd": ctx.working_dir}

    profile = get_harness(session.backend).profile
    harness = Harness(replace(profile, build_oneshot_argv=render, parse_oneshot_stdout=lambda _: '{"path":"plan.md"}'))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", observe_spawn)
    monkeypatch.setattr(files, "get_harness", lambda _: harness)

    async def revoke():
        await db.update_user_field(session.user_id, extra_roots="[]")

    if when == "before_request":
        await revoke()
    elif when == "before_spawn":
        run_oneshot = harness.run_oneshot

        async def revoke_before_admission(ctx):
            async def authorize():
                await revoke()
                return await ctx.authorize_spawn()

            return await run_oneshot(replace(ctx, authorize_spawn=authorize))

        monkeypatch.setattr(harness, "run_oneshot", revoke_before_admission)

    response = await client.post(f"/api/sessions/{sid}/showme/resolve", json={"text": "the requested report"})
    assert spawn_calls == []
    assert response.status_code == 403
    assert response.json()["code"] == "working_directory_unavailable"
    if when == "before_preparation":
        assert not cwd.exists(), "one-shot preparation must not recreate the directory"
    available = await client.get(f"/api/sessions/{sid}/files/sources")
    assert available.json()["cwd"] is None
    assert str(cwd) not in available.json()["roots"] and str(workspace) in available.json()["roots"]
    recovered = await client.get(f"/api/sessions/{sid}/files", params={"path": "recovery.md", "source": str(workspace)})
    assert recovered.text == "# Available from workspace"


@pytest.mark.parametrize("replacement", ["symlink", "directory"])
async def test_showme_child_uses_the_admitted_directory_after_rename(client, session_with_files, monkeypatch, replacement):
    from server.harness import Harness, get_harness

    sid, cwd = session_with_files
    session = session_manager.get_session(sid)
    moved = cwd.with_name("moved")
    redirected = cwd.with_name("redirected")
    redirected.mkdir()
    (cwd / "marker").write_text("original")
    (redirected / "marker").write_text("redirected")
    (redirected / "plan.md").write_text("# Replacement directory plan")
    spawn = asyncio.create_subprocess_exec

    async def switch_path_at_launch(*argv, **kwargs):
        cwd.rename(moved)
        if replacement == "symlink":
            cwd.symlink_to(redirected, target_is_directory=True)
        else:
            redirected.rename(cwd)
        return await spawn(*argv, **kwargs)

    def render(ctx):
        reply = "{'path': 'plan.md'} if marker == 'original' else {'message': marker}"
        if replacement == "symlink":
            reply = "{'message': marker}"
        script = f"import json; marker = open('marker').read(); print(json.dumps({reply}))"
        return [sys.executable, "-c", script], {"cwd": ctx.working_dir}

    profile = get_harness(session.backend).profile
    harness = Harness(replace(profile, build_oneshot_argv=render, parse_oneshot_stdout=str.strip))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", switch_path_at_launch)
    monkeypatch.setattr(files, "get_harness", lambda _: harness)
    response = await client.post(f"/api/sessions/{sid}/showme/resolve", json={"text": "the requested report"})
    if replacement == "symlink":
        assert response.status_code == 200, response.text
        assert response.json() == {"description": "the requested report", "path": None, "message": "original"}
    else:
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "working_directory_unavailable"
        assert response.json()["description"] == "the requested report"


@pytest.mark.parametrize(("change", "expected", "detail"), [
    ("restore", 404, "Session not found"),
    ("deleting", 409, "Session admission is closed"),
    ("archiving_agent", 409, "Agent admission is closed"),
    ("bearer", 401, "Invalid token"),
    ("credential", 422, "Credential is unavailable"),
    ("rotate", 422, "Credential changed"),
])
async def test_showme_rechecks_the_original_run_at_admission(
    client, session_with_files, monkeypatch, change, expected, detail,
):
    from datetime import datetime

    from server.agent_manager import AgentManager
    from server.crypto import encrypt
    from server.harness import Harness, get_harness

    sid, cwd = session_with_files
    session = session_manager.get_session(sid)
    db = session_manager.db
    if change == "archiving_agent":
        agent = await AgentManager(db).create_agent(name="Context search agent", user_id=session.user_id)
        session = await session_manager.create_session(agent["id"], name="Context search", working_dir=str(cwd))
        sid = session.id
    credential_id = "context-search-credential"
    if change in {"credential", "rotate"}:
        key = await deps.get_user_manager().data_key_for(session.user_id)
        secret = encrypt("offline-test-key", key)
        await db.save_credential(
            credential_id, session.backend, "Context search", "api_key", secret,
            datetime.now(UTC).isoformat(), user_id=session.user_id,
        )
        await db.update_agent(session.agent_id, credential_id=credential_id)
    spawns = []
    real_spawn = asyncio.create_subprocess_exec

    async def observe_spawn(*args, **kwargs):
        spawns.append(kwargs.get("cwd"))
        return await real_spawn(*args, **kwargs)

    profile = get_harness(session.backend).profile
    harness = Harness(replace(profile, build_oneshot_argv=lambda ctx: (
        [sys.executable, "-c", "print('harmless fixture')"], {"cwd": str(cwd)},
    )))
    run_oneshot = harness.run_oneshot
    lifecycle_task = None
    lifecycle_waiting = asyncio.Event()
    release_lifecycle = asyncio.Event()
    if change in {"deleting", "archiving_agent"}:
        method = "_delete_captured_session" if change == "deleting" else "evict_agent_sessions"
        finish_lifecycle = getattr(session_manager, method)

        async def delay_lifecycle(*args):
            lifecycle_waiting.set()
            await release_lifecycle.wait()
            return await finish_lifecycle(*args)

        monkeypatch.setattr(session_manager, method, delay_lifecycle)

    async def change_before_admission(ctx):
        nonlocal lifecycle_task
        if change == "restore":
            await session_manager.archive_session(sid, replace=False)
            restored = await session_manager.unarchive_session(sid, session.user_id)
            assert restored.id == session.id and restored is not session
        elif change == "bearer":
            await deps.get_user_manager().revoke_token(client.headers["Authorization"].removeprefix("Bearer "))
        elif change == "deleting":
            lifecycle_task = asyncio.create_task(session_manager.delete_session(sid))
            await asyncio.wait_for(lifecycle_waiting.wait(), 3)
        elif change == "archiving_agent":
            lifecycle_task = asyncio.create_task(session_manager.archive_agent(session.agent_id))
            await asyncio.wait_for(lifecycle_waiting.wait(), 3)
        elif change == "rotate":
            assert ctx.credential is not None
            await db.update_credential(credential_id, secret_encrypted=encrypt("new-offline-test-key", key))
        else:
            assert ctx.credential is not None
            await db.update_credential(credential_id, needs_reconnect=True)
        return await run_oneshot(ctx)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", observe_spawn)
    monkeypatch.setattr(harness, "run_oneshot", change_before_admission)
    monkeypatch.setattr(files, "get_harness", lambda _: harness)
    try:
        response = await client.post(f"/api/sessions/{sid}/showme/resolve", json={"text": "the requested report"})
    finally:
        release_lifecycle.set()
        if lifecycle_task is not None:
            await lifecycle_task
    assert spawns == []
    assert response.status_code == expected, response.text
    assert response.json()["detail"].startswith(detail)


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
    assert r.json()["path"] == "answer.md"
    assert r.json()["description"] == "the plan"
    assert r.json()["metadata"]["path"] == "answer.md"
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


@pytest.mark.parametrize("body", [
    {"text": "the proposed plan for v1.2"},
    {"text": "请打开 plan.md", "mode": "description"},
])
async def test_showme_inference_reads_last_twelve_total_rows_before_filtering(
    client, session_with_files, monkeypatch, body,
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
        {"role": "assistant", "type": "tool_use", "tool_name": "Write",
         "tool_input": {"file_path": "/reports/Claude plan.md", "content": "ignored file bytes"}},
        {"role": "assistant", "type": "result", "content": "ignored terminal"},
        {"role": "user", "type": "text", "content": {"value": "ignored object"}},
        {"role": "system", "type": "text", "content": "ignored system text"},
        {"role": "assistant", "type": "tool_result", "tool_name": "fileChange",
         "runtime_metadata": {"native": {"thread_id": "thread", "turn_id": "turn", "item_id": "patch"}},
         "content": json.dumps({"threadId": "thread", "turnId": "turn", "item": {
             "id": "patch", "type": "fileChange", "status": "completed", "changes": [
                 {"path": "/reports/old name.md", "kind": {"type": "update", "move_path": "/reports/Codex plan.md"},
                  "diff": "ignored patch bytes"},
             ],
         }})},
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
    response = await client.post(f"/api/sessions/{sid}/showme/resolve", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["path"] == "answer.md"
    assert response.json()["description"] == body["text"]
    assert loaded == [list(range(53, 65))]
    assert captured["calls"] == 1
    prompt = captured["prompt"]
    assert f"User reference: {body['text']}" in prompt
    assert "copy an absolute path from the recent conversation" in prompt
    assert "OLD CONTEXT" not in prompt
    assert "ignored" not in prompt
    assert prompt.index("User: recent user first") < prompt.index("Assistant: recent assistant")
    assert prompt.index("Assistant used Read with path='sub/note.txt'") < prompt.index("User: recent user last")
    assert "Assistant used Write with path='/reports/Claude plan.md'" in prompt
    assert "Assistant used fileChange with path='/reports/Codex plan.md'" in prompt
    assert prompt.index("User: recent user last") < prompt.index("Assistant: recent assistant last")
    assert [row["seq"] for row in await load_messages(sid)] == list(range(1, 65))


async def test_showme_offers_description_only_for_a_missing_ambiguous_filename(client, session_with_files, monkeypatch):
    sid, cwd = session_with_files
    (cwd / "中文 计划.md").write_text("# Literal filename")
    (cwd / "existing directory.md").mkdir()
    (cwd / "missing link.md").symlink_to(cwd.parent.parent / "denied.md")
    (cwd / "wrong format.bin").write_bytes(b"\x00")
    (cwd / "large plan.md").write_bytes(b"x" * (MAX_FILE_BYTES + 1))
    captured = await _patch_showme(monkeypatch)
    endpoint = f"/api/sessions/{sid}/showme/resolve"
    for text, expected, can_search in [
        ("请打开 plan.md", 404, True), ('"请打开 plan.md"', 404, False),
        ("./请打开 plan.md", 404, False), ("请打开 plan.md:12", 404, True),
        ('"请打开 plan.md":12', 404, False),
        ("existing directory.md", 404, False), ("missing link.md", 403, False),
        ("wrong format.bin", 415, False), ("large plan.md", 413, False),
        ("中文 计划.md", 200, False),
    ]:
        response = await client.post(endpoint, json={"text": text})
        assert response.status_code == expected, response.text
        assert response.json().get("can_search_description", False) is can_search
    invalid = await client.post(endpoint, json={"text": "plan.md", "mode": "invalid"})
    assert invalid.status_code == 422 and invalid.json()["detail"][0]["loc"] == ["body", "mode"]
    assert captured == {}


@pytest.mark.parametrize(("candidate", "revoke", "expected"), [
    ("plan.md", None, 200), ("repo/plan.md", None, 200), ("outside/plan.md", None, 403),
    ("repo/plan.md", "before_metadata", 403), ("repo/plan.md", "before_body", 200),
    ("missing.md", None, 404), ("blob.bin", None, 415),
])
async def test_showme_applies_current_file_authority_to_the_candidate(
    client, session_with_files, monkeypatch, candidate, revoke, expected,
):
    sid, cwd = session_with_files
    session = session_manager.get_session(sid)
    db = session_manager.db
    for name in ("repo", "outside"):
        (cwd.parent / name).mkdir()
        (cwd.parent / name / "plan.md").write_text(f"# {name} version")
    repo = cwd.parent / "repo"
    await db.update_user_field(session.user_id, extra_roots=json.dumps([str(cwd), str(repo)]))

    async def revoke_repo():
        await db.update_user_field(session.user_id, extra_roots=json.dumps([str(cwd)]))

    path = str(cwd.parent / candidate) if "/" in candidate else candidate
    captured = await _patch_showme(
        monkeypatch, json.dumps({"path": path}), before_reply=revoke_repo if revoke == "before_metadata" else None,
    )
    description = "the plan mentioned in our discussion"
    resolve = await client.post(f"/api/sessions/{sid}/showme/resolve", json={"text": description})
    assert captured["calls"] == 1
    assert resolve.status_code == expected, resolve.text
    assert resolve.headers["cache-control"] == "no-store"
    assert resolve.json()["description"] == description
    if expected != 200:
        assert "metadata" not in resolve.json() and "can_search_description" not in resolve.json()
        assert ("target" in resolve.json()) == (expected != 403)
        return

    assert resolve.json()["path"] == path
    meta = resolve.json()["metadata"]
    assert meta["source"] == str(repo if "/" in candidate else cwd)
    assert meta["path"] == "plan.md"
    if revoke == "before_body":
        await revoke_repo()
    body = await client.get(f"/api/sessions/{sid}/files", params={
        key: meta[key] for key in ("path", "source", "canonical_path_id")
    })
    if revoke == "before_body":
        assert body.status_code == 403
    else:
        assert body.text == ("# repo version" if "/" in candidate else "# plan\nstep one")


@pytest.mark.parametrize(("reference", "status"), [
    ("../outside.md", 403), ("escape-link.md", 403), ("missing.md", 404),
    ("docs/current", 404), ("docs/README", 404), ("missing 中文.md", 404),
    ("blob.bin", 415), ("huge.md", 413), (".md", 415),
])
async def test_showme_explicit_file_errors_skip_inference(
    client, session_with_files, monkeypatch, reference, status,
):
    sid, root = session_with_files
    (root / "escape-link.md").symlink_to(root.parent / "outside.md")
    (root / ".md").write_text("literal dotfile")
    captured = await _patch_showme(monkeypatch)
    response = await client.post(f"/api/sessions/{sid}/showme/resolve", json={"text": reference})
    assert response.status_code == status, response.text
    assert response.headers["cache-control"] == "no-store"
    assert captured == {}
    viewed = await client.get(f"/api/sessions/{sid}/files", params={"path": reference})
    assert viewed.status_code == status


@pytest.mark.parametrize(("filename", "suffix"), [("README", ":12"), ("dir#archive/README", "#L12")])
async def test_showme_line_on_extensionless_file_keeps_a_standalone_path(
    client, session_with_files, monkeypatch, filename, suffix,
):
    sid, cwd = session_with_files
    file = cwd / filename
    file.parent.mkdir(exist_ok=True)
    file.write_text("line\n" * 12)
    command_path = f"./{filename}" if "/" in filename else filename
    captured = await _patch_showme(monkeypatch)
    base = f"/api/sessions/{sid}"

    response = await client.post(f"{base}/showme/resolve", json={"text": command_path + suffix})

    assert response.status_code == 200, response.text
    assert captured == {}
    result = response.json()
    assert result["target"] == {"path": command_path, "position": {"kind": "lines", "start": 12}}
    assert result["metadata"]["source"] == str(cwd)
    legacy = await client.get(f"{base}/files", params={"path": result["path"]})
    assert legacy.status_code == 200, legacy.text
    assert legacy.text == "line\n" * 12


@pytest.mark.parametrize(("suffix", "position"), [
    ("#L12", {"kind": "lines", "start": 12}),
    ("#L12-L18", {"kind": "lines", "start": 12, "end": 18}),
    ("#附录", {"kind": "heading", "slug": "附录"}),
    (":0", {"kind": "invalid", "suffix": ":0"}),
    ("#L9-L3", {"kind": "invalid", "suffix": "#L9-L3"}),
    ("#L9007199254740992", {"kind": "invalid", "suffix": "#L9007199254740992"}),
])
async def test_showme_quoted_filename_and_position_are_independent(client, session_with_files, suffix, position):
    sid, cwd = session_with_files
    filename = "中文 %20:计划.md#附录.md"
    (cwd / filename).write_text("# 附录")

    response = await client.post(
        f"/api/sessions/{sid}/showme/resolve", json={"text": f'{json.dumps(filename)}{suffix}'},
    )

    assert response.status_code == 200, response.text
    assert response.json()["path"] == filename
    assert response.json()["target"] == {"path": filename, "position": position}


async def test_showme_source_requires_a_quoted_path(client, session_with_files, monkeypatch):
    sid, cwd = session_with_files
    captured = await _patch_showme(monkeypatch)
    invalid_source = "[" * 1500 + '"/elsewhere"' + "]" * 1500
    raw_decode = json.JSONDecoder.raw_decode

    def decode(decoder, candidate, idx=0):
        # CPython 3.14 decodes this iteratively. Exercise a nesting refusal
        # without changing the process-wide recursion limit.
        if candidate == invalid_source:
            raise RecursionError("Controlled JSON decoder nesting refusal")
        return raw_decode(decoder, candidate, idx)

    monkeypatch.setattr(json.JSONDecoder, "raw_decode", decode)
    response = await client.post(
        f"/api/sessions/{sid}/showme/resolve",
        json={"text": f'{json.dumps(str(cwd / "plan.md"))} --source {invalid_source}'},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == "Use double-quoted absolute paths for the file and its source."
    assert captured == {}


@pytest.mark.parametrize(("filename", "status"), [("~/中文 %23#计划.md", 404), ("../中文 %23#计划.md", 403)])
async def test_showme_failed_file_target_can_be_edited(client, session_with_files, monkeypatch, filename, status):
    sid, _ = session_with_files
    captured = await _patch_showme(monkeypatch)
    request = await client.post(
        f"/api/sessions/{sid}/showme/resolve", json={"text": f'{json.dumps(filename)}:12'},
    )

    assert request.status_code == status, request.text
    assert request.headers["cache-control"] == "no-store"
    result = request.json()
    assert isinstance(result["detail"], str)
    assert result["target"] == {"path": filename, "position": {"kind": "lines", "start": 12}}
    assert captured == {}


async def test_showme_legacy_paths_reopen_the_checked_sources(client, session_with_files, monkeypatch):
    sid, cwd = session_with_files
    db = session_manager.db
    user_id = session_manager.get_session(sid).user_id
    workspace = paths_for(user_id).workspace
    repo = cwd.with_name("repo")
    repo.mkdir()
    team = repo / "team"
    team.mkdir()
    (workspace / "plan.md").write_text("# Workspace")
    (repo / "plan.md").write_text("# Repository")
    (team / "plan.md").write_text("# Team plan\n\n![Diagram](../diagram.svg)")
    (repo / "alias.md").symlink_to("team/plan.md")
    (repo / "diagram.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    (cwd / "README").write_text("bare file")
    (cwd / "中文 计划.md").write_text("# Spaces")
    (cwd / "中文 %20#计划.md").write_text("# Literal")
    (cwd / "docs").mkdir()
    (cwd / "docs" / "README").symlink_to("../README")
    (cwd / "docs" / "current").symlink_to("../plan.md")
    (cwd / "$(ignored).md").write_text("# Literal shell characters")
    literal_names = {"字面\\n.md": "Backslash", "字面\r.md": "CR", "字面\n.md": "LF"}
    for name, text in literal_names.items():
        (cwd / name).write_text(text)
    await db.update_user_field(user_id, extra_roots=json.dumps([str(cwd), str(repo), str(team)]))
    await session_manager.archive_session(sid, replace=False)
    captured = await _patch_showme(monkeypatch)
    base = f"/api/sessions/{sid}"
    cases = [
        ("./plan.md", "plan.md", "# plan\nstep one", cwd),
        ("README", "README", "bare file", cwd),
        ("docs/README", "README", "bare file", cwd),
        ("docs/current", "plan.md", "# plan\nstep one", cwd),
        ("中文 计划.md", "中文 计划.md", "# Spaces", cwd),
        ('"中文 %20#计划.md"', "中文 %20#计划.md", "# Literal", cwd),
        (str(cwd / "中文 %20#计划.md"), "中文 %20#计划.md", "# Literal", cwd),
        ("$(ignored).md", "$(ignored).md", "# Literal shell characters", cwd),
        (str(workspace / "plan.md"), str(workspace / "plan.md"), "# Workspace", workspace),
        (str(repo / "plan.md"), str(repo / "plan.md"), "# Repository", repo),
        (str(repo / "alias.md"), str(repo / "alias.md"), "# Team plan\n\n![Diagram](../diagram.svg)", repo),
        *((json.dumps(name, ensure_ascii=False), name, text, cwd) for name, text in literal_names.items()),
    ]
    for reference, expected_path, text, source in cases:
        resolution = await client.post(f"{base}/showme/resolve", json={"text": reference})
        assert resolution.status_code == 200, resolution.text
        assert resolution.headers["cache-control"] == "no-store"
        result = resolution.json()
        assert result["path"] == expected_path
        meta = await client.get(f"{base}/files/meta", params={"path": result["path"]})
        assert meta.json() == result["metadata"]
        assert meta.json()["source"] == str(source)
        body = await client.get(f"{base}/files", params={"path": result["path"]})
        assert body.status_code == 200 and body.text == text
        if reference == str(repo / "alias.md"):
            folder = meta.json()["canonical_directory"]
            resource = await client.get(f"{base}/files", params={
                "path": "../diagram.svg", "source": str(source),
                "source_directory": folder["path"], "source_directory_id": folder["path_id"],
            })
            assert resource.status_code == 200 and resource.text.startswith("<svg")

    canonical = team / "plan.md"
    bound = f"{json.dumps(str(canonical))} --source {json.dumps(str(repo))}#team-plan"
    reopened = await client.post(f"{base}/showme/resolve", json={"text": bound})
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["metadata"]["source"] == str(repo)
    assert reopened.json()["target"]["position"] == {"kind": "heading", "slug": "team-plan"}
    reopened_body = await client.get(f"{base}/files", params={"path": reopened.json()["path"]})
    assert reopened_body.text == "# Team plan\n\n![Diagram](../diagram.svg)"

    await db.update_user_field(user_id, extra_roots=json.dumps([str(cwd), str(team)]))
    denied_source = await client.post(f"{base}/showme/resolve", json={"text": bound})
    assert denied_source.status_code == 403
    assert denied_source.json()["target"]["source"] == str(repo)
    unbound = await client.post(f"{base}/showme/resolve", json={"text": json.dumps(str(canonical))})
    assert unbound.json()["metadata"]["source"] == str(team)

    await db.update_user_field(user_id, extra_roots=json.dumps([str(cwd)]))
    denied = await client.post(f"{base}/showme/resolve", json={"text": str(repo / "plan.md")})
    assert denied.status_code == 403
    assert captured == {}


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


