"""Integration tests for the /fork copy-dir saga (session-fork.md).

Drives `SessionManager.duplicate_session` against a real in-memory DB. Unlike
`fork_session` (/rewind), `duplicate_session` copies the WHOLE conversation onto
an independent full copy of the working directory and leaves the parent intact.
"""

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from server import session_manager as sm
from server.config import settings
from server.database import Database
from server.db.transaction import Transaction
from server.harness import HarnessEvent
from server.harness.claude_code import _claude_project_dir
from server.main import app
from server.session_manager import ForkError, SessionManager
from server.session_manager import session_manager as global_sm
from server.sessions import fork_directory
from server.sessions import forking as sm_forking
from server.workspace import WorkspaceError, paths_for
from tests.accounts import create_account
from tests.fake_run import FakeRunBase, model_run_factory
from tests.installation_settings import installation_settings


@pytest.fixture
async def manager(tmp_path):
    mgr = SessionManager()
    db = Database(":memory:")
    await db.initialize()
    await create_account(db, extra_roots=[tmp_path])
    await mgr.initialize(db)
    try:
        yield mgr
    finally:
        await db.close()


async def _seed_parent(mgr, working_dir, *, backend="claude-code", n_user=3):
    agent = await mgr.db.get_system_agent()
    parent = await mgr.create_session(
        agent["id"], "Parent", str(working_dir), backend=backend
    )
    seq = 0
    for i in range(n_user):
        await mgr.db.append_message(
            session_id=parent.id, seq=seq, role="user", type="text", content=f"q{i}"
        )
        seq += 1
        await mgr.db.append_message(
            session_id=parent.id, seq=seq, role="assistant", type="text",
            content=f"a{i}",
        )
        seq += 1
    parent._message_count = seq
    return parent


@pytest.mark.parametrize("configured_alias", [False, True])
async def test_default_account_can_fork_workspace_and_archive_twice(manager, tmp_path, monkeypatch, configured_alias):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    if configured_alias:
        alias = tmp_path / "configured-users"
        alias.symlink_to(Path(settings.users_root), target_is_directory=True)
        monkeypatch.setattr(settings, "users_root", str(alias))
    account, _ = await create_account(manager.db, username="default-fork-owner")
    agent = await manager.db.get_system_agent(account["id"])
    workspace = paths_for(account["id"]).workspace
    (workspace / "notes.txt").write_text("from the whole workspace")
    parent = await manager.create_session(agent["id"], "Workspace")

    fork = await manager.duplicate_session(parent.id, user_id=account["id"])
    copy = Path(fork.working_dir)
    assert (copy / "notes.txt").read_text() == "from the whole workspace"

    first = await manager.archive_session(fork.id)
    assert first is not None and first.working_dir == fork.working_dir
    second = await manager.archive_session(first.id)
    assert second is not None and second.working_dir == fork.working_dir
    assert copy.parent == workspace.parent.resolve() / "fork"
    assert manager.get_session(second.id, account["id"]) is second
    other, _ = await create_account(manager.db, username="other-fork-account")
    other_agent = await manager.db.get_system_agent(other["id"])
    with pytest.raises(WorkspaceError, match="outside your workspace"):
        await manager.create_session(other_agent["id"], "Foreign cwd", str(copy))
    with pytest.raises(ForkError, match="not found"):
        await manager.duplicate_session(second.id, user_id=other["id"])


def _repo(tmp_path):
    repo = tmp_path / "myproject"
    repo.mkdir()
    (repo / "code.py").write_text("print('hi')\n")
    sub = repo / "pkg"
    sub.mkdir()
    (sub / "mod.py").write_text("X = 1\n")
    return repo


def _git(repository, *args):
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    return subprocess.check_output(
        ["git", "-C", str(repository), "-c", "user.name=Fork Test",
         "-c", "user.email=fork@example.test", *args],
        env=env,
        timeout=10,
    )


def _split_git_storage(repo):
    common = (repo / ".git").rename(repo / "common")
    private = repo / "private"
    private.mkdir()
    (private / "HEAD").write_bytes((common / "HEAD").read_bytes())
    (common / "index").rename(private / "index")
    (private / "commondir").write_text("../common\n")
    (repo / ".git").write_text("gitdir: private\n")
    return private, common


@pytest.mark.parametrize(
    ("layout", "message"),
    [
        ("absolute-worktree", "core.worktree"),
        ("linked-worktree", "gitfile"),
        ("separate-gitdir", "gitfile"),
        ("common-dir", "commondir"),
        ("public-dir-external-worktree", "core.worktree"),
        ("external-git-symlink", "Git symlink"),
        ("refs-discovered-only-at-destination", "Git symlink refs"),
        ("refs-discovered-with-detached-head", "Git symlink refs"),
        ("worktree-behind-storage-link", "core.worktree"),
        ("nested-alternate", "object alternates"),
        ("second-quoted-alternate", "object alternates"),
        ("per-worktree-config", "core.worktree"),
        ("invalid-gitfile", "gitfile"),
        ("invalid-config", "Git could not inspect"),
    ],
)
async def test_duplicate_refuses_shared_git_administration_before_allocation(
    manager, tmp_path, monkeypatch, layout, message,
):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    repo = _repo(tmp_path)
    if layout == "separate-gitdir":
        _git(repo, "init", "-q", "--separate-git-dir", str(tmp_path / "external"))
    elif layout == "invalid-gitfile":
        (repo / ".git").write_text("invalid")
    else:
        _git(repo, "init", "-q")
        git_dir = repo / ".git"
        if layout == "absolute-worktree":
            _git(repo, "config", "core.worktree", str(repo))
        elif layout == "linked-worktree":
            _git(repo, "add", ".")
            _git(repo, "commit", "-qm", "initial")
            linked = tmp_path / "linked"
            _git(repo, "worktree", "add", "-qb", "linked", str(linked))
            repo = linked
        elif layout == "common-dir":
            (git_dir / "commondir").write_text(f"{git_dir}\n")
        elif layout == "public-dir-external-worktree":
            _git(repo, "add", ".")
            _git(repo, "commit", "-qm", "initial")
            _, common = _split_git_storage(repo)
            _git(repo, "config", "--file", str(common / "config"), "core.worktree", str(repo))
            assert Path(os.fsdecode(_git(common, "rev-parse", "--show-toplevel")).removesuffix("\n")) == repo
        elif layout == "external-git-symlink":
            external_refs = tmp_path / "external-refs"
            (git_dir / "refs").rename(external_refs)
            (git_dir / "refs").symlink_to(external_refs)
        elif layout in {"refs-discovered-only-at-destination", "refs-discovered-with-detached-head"}:
            if layout == "refs-discovered-with-detached-head":
                (git_dir / "HEAD").write_text("1" * 64 + "\n")
            owner = (await manager.db.get_system_agent())["user_id"]
            account_root = paths_for(owner).workspace.parent
            external_refs = account_root / f"shared-refs-{tmp_path.name}"
            (git_dir / "refs").rename(external_refs)
            (git_dir / "refs").symlink_to(f"../../../{external_refs.name}")
            assert not (git_dir / "refs").exists()
            with pytest.raises(subprocess.CalledProcessError, match="128"):
                _git(repo, "rev-parse", "--absolute-git-dir")
            projected_git = account_root / "fork" / "copy" / ".git"
            assert (projected_git / (git_dir / "refs").readlink()).resolve() == external_refs
        elif layout == "worktree-behind-storage-link":
            assets = repo / "assets"
            nested = assets / "nested"
            nested.mkdir(parents=True)
            _git(nested, "init", "-q", "--bare")
            _git(nested, "config", "core.bare", "false")
            _git(nested, "config", "core.worktree", str(repo))
            (git_dir / "objects" / "info" / "storage").symlink_to("../../../assets")
            assert Path(os.fsdecode(_git(nested, "rev-parse", "--show-toplevel")).removesuffix("\n")) == repo
        elif layout == "nested-alternate":
            _git(repo / "pkg", "init", "-q")
            alt = repo / "pkg" / ".git" / "objects" / "info" / "alternates"
            alt.write_text(f"{git_dir / 'objects'}\n")
        elif layout == "second-quoted-alternate":
            _git(repo / "pkg", "init", "-q")
            other = tmp_path / "other"
            other.mkdir()
            _git(other, "init", "-q", "--bare")
            (git_dir / "objects" / "info" / "alternates").write_text(
                f'"../../pkg/.git/objects" "{other / "objects"}"\n'
            )
            assert _git(repo, "count-objects", "-v").count(b"alternate: ") == 2
        elif layout == "per-worktree-config":
            _git(repo, "config", "extensions.worktreeConfig", "true")
            _git(repo, "config", "--worktree", "core.worktree", str(repo))
        elif layout == "invalid-config":
            (git_dir / "config").write_text("[unclosed\n")
    parent = await _seed_parent(manager, repo)
    row_inserts = []
    native_copies = []
    directory_creates = []
    copy_base = paths_for(parent.user_id).workspace.parent / "fork"
    insert = manager.db.create_fork_session
    mkdir = sm_forking.os.mkdir
    harness = sm_forking.get_harness(parent.backend)
    prepare = harness.prepare_fork_copy

    async def observe_insert(**kwargs):
        row_inserts.append(kwargs["fork_id"])
        return await insert(**kwargs)

    async def observe_prepare(**kwargs):
        native_copies.append(kwargs["dest_working_dir"])
        return await prepare(**kwargs)

    def observe_mkdir(path, *args, **kwargs):
        directory = kwargs.get("dir_fd")
        target = Path(path) if directory is None else fork_directory.fd_path(directory).resolve() / path
        if target.is_relative_to(copy_base):
            directory_creates.append(path)
        return mkdir(path, *args, **kwargs)

    monkeypatch.setattr(manager.db, "create_fork_session", observe_insert)
    monkeypatch.setattr(harness, "prepare_fork_copy", observe_prepare)
    monkeypatch.setattr(sm_forking.os, "mkdir", observe_mkdir)

    with pytest.raises(ForkError, match=f"{message}.*self-contained") as refused:
        await manager.duplicate_session(parent.id)
    assert refused.value.reason == "unsupported_git_layout"
    assert row_inserts == native_copies == directory_creates == []
    assert not copy_base.exists()

    assert manager.get_session(parent.id) is parent
    assert not parent._forking
    if layout == "absolute-worktree":
        _git(repo, "config", "--unset", "core.worktree")
        assert (await manager.duplicate_session(parent.id)).fork_status == "ready"


@pytest.mark.parametrize("layout", [
    "standard", "relative-gitfile", "relative-symlink", "submodule", "removed-submodule", "removed-module-link",
    "relative-alternate", "quoted-alternate-crlf", "inactive-include", "missing-include",
    "quoted-alternates", "inactive-worktree-config", "overridden-worktree", "included-option",
    "newline-in-path", "same-common-dir", "relative-common-dir", "unset-format", "gitfile-first", "storage-first",
    "plain-refs", "plain-commondir", "plain-ref-prefix",
])
async def test_duplicate_git_state_can_be_committed_independently(manager, tmp_path, monkeypatch, layout):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "unrelated-index"))
    repo = _repo(tmp_path)
    if layout == "newline-in-path":
        repo = repo.rename(tmp_path / "project\nwith-newline")
    worktree = repo
    if layout in {"gitfile-first", "storage-first"}:
        worktree = repo / "checkout"
        worktree.mkdir()
        (repo / "code.py").rename(worktree / "code.py")
    _git(worktree, "init", "-q")
    _git(worktree, "add", ".")
    _git(worktree, "commit", "-qm", "initial")
    git_dir = worktree / ".git"
    if layout in {"relative-gitfile", "relative-symlink"}:
        storage = repo / "admin"
        git_dir.rename(storage)
        if layout == "relative-gitfile":
            git_dir.write_text("gitdir: admin\n")
        else:
            git_dir.symlink_to("admin")
        _git(repo, "config", "core.worktree", "..")
        git_dir = storage
    elif layout in {"submodule", "removed-submodule", "removed-module-link"}:
        upstream = tmp_path / "upstream"
        upstream.mkdir()
        _git(upstream, "init", "-q")
        (upstream / "code.py").write_text("submodule\n")
        _git(upstream, "add", ".")
        _git(upstream, "commit", "-qm", "initial submodule")
        _git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(upstream), "sub")
        _git(repo, "commit", "-qm", "submodule checkout")
        if layout == "submodule":
            worktree = repo / "sub"
            git_dir = git_dir / "modules" / "sub"
        else:
            _git(repo, "rm", "-q", "sub")
            _git(repo, "commit", "-qm", "remove submodule")
            assert (git_dir / "modules" / "sub" / "HEAD").is_file()
            if layout == "removed-module-link":
                modules = (git_dir / "modules").rename(repo / "git-modules")
                (git_dir / "modules").symlink_to("../git-modules")
                _git(repo, "config", "--file", str(modules / "sub" / "config"), "core.worktree", "../../sub")
    elif layout in {"relative-alternate", "quoted-alternate-crlf", "quoted-alternates"}:
        _git(repo / "pkg", "init", "-q")
        ending = b"\r\n" if layout == "quoted-alternate-crlf" else b"\n"
        paths = b'"../../pkg/.git/objects"'
        if layout == "quoted-alternates":
            second = repo / "repo with trailing space "
            second.mkdir()
            _git(second, "init", "-q", "--bare")
            paths += b' "" "../../repo with trailing space /objects" '
        (git_dir / "objects" / "info" / "alternates").write_bytes(
            paths + ending
        )
        assert _git(repo, "count-objects", "-v").count(b"alternate: ") == (2 if layout == "quoted-alternates" else 1)
    elif layout == "inactive-include":
        _git(repo, "config", f"includeIf.gitdir:{tmp_path}/other/.git.path", "repository.inc")
        (git_dir / "repository.inc").write_text(f"[core]\nworktree = {repo}\n")
    elif layout == "missing-include":
        _git(repo, "config", "include.path", "optional.inc")
    elif layout == "inactive-worktree-config":
        (git_dir / "config.worktree").write_text(f"[core]\nworktree = {repo}\n")
    elif layout == "overridden-worktree":
        _git(repo, "config", "--add", "core.worktree", str(repo))
        _git(repo, "config", "--add", "core.worktree", "..")
    elif layout == "included-option":
        _git(repo, "config", "include.path", "repository.inc")
        (git_dir / "repository.inc").write_text(f"[core]\nworktree = {repo}\n")
    elif layout == "relative-common-dir":
        git_dir, _ = _split_git_storage(repo)
    elif layout in {"same-common-dir", "unset-format"}:
        _git(repo, "config", "core.worktree", str(repo))
        if layout == "same-common-dir":
            (git_dir / "commondir").write_text(".\n")
        else:
            _git(repo, "config", "core.repositoryformatversion", "-1")
    elif layout in {"gitfile-first", "storage-first"}:
        storage = repo / "git-data"
        git_dir.rename(storage)
        git_dir.write_text("gitdir: ../git-data\n")
        git_dir = storage
        walk = os.walk

        def ordered_walk(top, *args, **kwargs):
            for directory, subdirs, files in walk(top, *args, **kwargs):
                if Path(directory) == Path(top):
                    subdirs.sort(reverse=layout == "storage-first")
                yield directory, subdirs, files

        monkeypatch.setattr(sm_forking.os, "walk", ordered_walk)
    elif layout in {"plain-refs", "plain-commondir", "plain-ref-prefix"}:
        data = repo / "data"
        (data / "objects").mkdir(parents=True)
        head_text = "ref:\vrefs/heads/main\n" if layout == "plain-ref-prefix" else "A plain file\n"
        (data / "HEAD").write_text(head_text)
        external = tmp_path / "ordinary-resource"
        external.mkdir()
        if layout in {"plain-refs", "plain-ref-prefix"}:
            (data / "refs").symlink_to(external)
        else:
            (data / "commondir").write_text(f"{external}\n")
        with pytest.raises(subprocess.CalledProcessError, match="128"):
            _git(repo, "rev-parse", "--resolve-git-dir", str(data))

    ordinary_link = repo / "ordinary-link"
    ordinary_link.symlink_to(tmp_path / "other-working-file")
    parent = await _seed_parent(manager, repo)
    source_file = worktree / "code.py"
    source_bytes = source_file.read_bytes()
    source_head = _git(worktree, "rev-parse", "HEAD")
    source_refs = _git(worktree, "show-ref")
    source_status = _git(worktree, "status", "--porcelain")
    source_index = (git_dir / "index").read_bytes()

    fork = await manager.duplicate_session(parent.id)
    assert fork.fork_status == "ready"
    copy = Path(fork.working_dir)
    assert (copy / ordinary_link.name).readlink() == ordinary_link.readlink()
    if layout in {"plain-refs", "plain-ref-prefix"}:
        assert (copy / "data" / "refs").readlink() == (repo / "data" / "refs").readlink()
    elif layout == "plain-commondir":
        assert (copy / "data" / "commondir").read_bytes() == (repo / "data" / "commondir").read_bytes()
    child = copy / worktree.relative_to(repo)
    (child / "code.py").write_text("independent\n")
    _git(child, "add", "code.py")
    _git(child, "commit", "-qm", "independent change")
    assert _git(child, "show", "HEAD:code.py") == b"independent\n"
    assert _git(child, "rev-parse", "HEAD") != source_head
    assert source_file.read_bytes() == source_bytes
    assert (git_dir / "index").read_bytes() == source_index
    assert _git(worktree, "rev-parse", "HEAD") == source_head
    assert _git(worktree, "show-ref") == source_refs
    assert _git(worktree, "status", "--porcelain") == source_status
    assert not (tmp_path / "unrelated-index").exists()


async def test_duplicate_standalone_git_storage_can_create_only_a_copy_branch(manager, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    _git(repo, "init", "-q")
    _git(repo, "add", "code.py")
    _git(repo, "commit", "-qm", "main workspace")
    storage = repo / "retained-git"
    original_checkout = tmp_path / "checkout"
    original_checkout.mkdir()
    _git(original_checkout, "init", "-q", "--separate-git-dir", str(storage))
    (original_checkout / "saved").write_text("retained history\n")
    _git(original_checkout, "add", "saved")
    _git(original_checkout, "commit", "-qm", "retained commit")
    (original_checkout / ".git").unlink()
    (original_checkout / "saved").unlink()
    original_checkout.rmdir()
    with pytest.raises(subprocess.CalledProcessError, match="128"):
        _git(storage, "rev-parse", "--show-toplevel")
    retained_head = _git(storage, "rev-parse", "HEAD")
    retained_refs = _git(storage, "show-ref")
    parent_head = _git(repo, "rev-parse", "HEAD")
    parent = await _seed_parent(manager, repo)

    fork = await manager.duplicate_session(parent.id)

    child = Path(fork.working_dir)
    assert fork.fork_status == "ready"
    copied_storage = child / storage.name
    assert _git(copied_storage, "show", "HEAD:saved") == b"retained history\n"
    _git(copied_storage, "branch", "copy-only")
    assert _git(copied_storage, "rev-parse", "refs/heads/copy-only") == retained_head
    (child / "code.py").write_text("independent\n")
    _git(child, "add", "code.py")
    _git(child, "commit", "-qm", "copy workspace")
    assert _git(child, "show", "HEAD:code.py") == b"independent\n"
    assert _git(storage, "rev-parse", "HEAD") == retained_head
    assert _git(storage, "show-ref") == retained_refs
    assert _git(repo, "rev-parse", "HEAD") == parent_head


async def test_duplicate_checkout_and_bare_storage_at_same_path(manager, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    _git(repo, "init", "-q")
    _git(repo, "add", "code.py")
    _git(repo, "commit", "-qm", "initial")
    bare = tmp_path / "bare"
    _git(repo, "clone", "-q", "--bare", "--no-hardlinks", str(repo), str(bare))
    for entry in bare.iterdir():
        entry.rename(repo / entry.name)
    assert _git(repo, "rev-parse", "--git-dir") == b".git\n"
    assert _git(repo, "--git-dir=.", "rev-parse", "--is-bare-repository") == b"true\n"
    source_head = _git(repo, "rev-parse", "HEAD")
    source_refs = _git(repo, "show-ref")
    source_bare_refs = _git(repo, "--git-dir=.", "show-ref")
    source_index = (repo / ".git" / "index").read_bytes()
    parent = await _seed_parent(manager, repo)

    fork = await manager.duplicate_session(parent.id)

    assert fork.fork_status == "ready"
    copy = Path(fork.working_dir)
    (copy / "code.py").write_text("independent\n")
    _git(copy, "add", "code.py")
    _git(copy, "commit", "-qm", "copy workspace")
    assert _git(copy, "show", "HEAD:code.py") == b"independent\n"
    _git(copy, "--git-dir=.", "branch", "copy-only")
    assert _git(copy, "--git-dir=.", "rev-parse", "refs/heads/copy-only") == source_head
    assert _git(repo, "rev-parse", "HEAD") == source_head
    assert _git(repo, "show-ref") == source_refs
    assert _git(repo, "--git-dir=.", "show-ref") == source_bare_refs
    assert (repo / ".git" / "index").read_bytes() == source_index
    assert (repo / "code.py").read_text() == "print('hi')\n"


async def test_duplicate_unborn_head_link_creates_only_the_copy_ref(manager, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    _git(repo, "init", "-q")
    head = repo / ".git" / "HEAD"
    head.unlink()
    head.symlink_to("refs/heads/unborn")
    assert _git(repo, "symbolic-ref", "HEAD") == b"refs/heads/unborn\n"
    parent = await _seed_parent(manager, repo)

    fork = await manager.duplicate_session(parent.id)

    child = Path(fork.working_dir)
    assert fork.fork_status == "ready"
    _git(child, "add", "code.py")
    _git(child, "commit", "-qm", "first commit")
    assert _git(child, "show", "refs/heads/unborn:code.py") == b"print('hi')\n"
    assert head.is_symlink()
    assert not (repo / ".git" / "refs" / "heads" / "unborn").exists()
    assert not (repo / ".git" / "index").exists()


@pytest.mark.parametrize("at_source", [False, True])
async def test_duplicate_refuses_git_inherited_from_ancestor(manager, tmp_path, monkeypatch, at_source):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    owner = (await manager.db.get_system_agent())["user_id"]
    account = paths_for(owner).workspace.parent
    parent_dir = tmp_path / "parent" if at_source else account
    parent_dir.mkdir(exist_ok=True)
    repo = _repo(parent_dir if at_source else tmp_path)
    common = tmp_path / "common"
    common.mkdir()
    _git(common, "init", "-q", "--bare")
    (parent_dir / "HEAD").write_text("ref: refs/heads/main\n")
    (parent_dir / "commondir").write_text(f"{common}\n")
    assert _git(parent_dir, "rev-parse", "--absolute-git-dir").decode().rstrip() == str(parent_dir)
    parent = await _seed_parent(manager, repo)

    async def unexpected_insert(**kwargs):
        pytest.fail("initializing fork was inserted")

    monkeypatch.setattr(manager.db, "create_fork_session", unexpected_insert)
    with pytest.raises(ForkError, match="ancestor.*self-contained"):
        await manager.duplicate_session(parent.id)
    assert not (account / "fork").exists()
    assert not parent._forking


@pytest.mark.parametrize("marker", ["ancestor", "empty-dot-git", "dot-git-with-readme", "bare-like"])
async def test_duplicate_plain_directory_uses_git_discovery(manager, tmp_path, monkeypatch, marker):
    home = tmp_path / "home"
    (home / ".git").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    (tmp_path / ".git").mkdir()
    repo = _repo(tmp_path)
    if marker in {"empty-dot-git", "dot-git-with-readme"}:
        (repo / ".git").mkdir()
        if marker == "dot-git-with-readme":
            (repo / ".git" / "README").write_text("A plain directory\n")
    elif marker == "bare-like":
        (repo / "HEAD").write_text("A plain file\n")
        (repo / "objects").mkdir()
        (repo / "refs").mkdir()
    parent = await _seed_parent(manager, repo)

    fork = await manager.duplicate_session(parent.id)

    assert fork.fork_status == "ready"
    assert (Path(fork.working_dir) / "code.py").is_file()
    if marker == "empty-dot-git":
        assert not list((Path(fork.working_dir) / ".git").iterdir())
    elif marker == "dot-git-with-readme":
        assert (Path(fork.working_dir) / ".git" / "README").read_text() == "A plain directory\n"
    elif marker == "bare-like":
        assert (Path(fork.working_dir) / "HEAD").read_text() == "A plain file\n"


async def test_duplicate_compensates_when_git_layout_changes_before_copy(manager, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    _git(repo, "init", "-q")
    parent = await _seed_parent(manager, repo)
    copy = fork_directory.CopyDirectory.copy_from
    events = []
    copied_dirs = []
    ready = []
    finish = manager.db.finish_full_copy_fork

    def change_then_copy(directory, src):
        _git(repo, "config", "core.worktree", str(repo))
        copy(directory, src)
        copied_dirs.append(directory.path)

    async def observe_publish(event, **kwargs):
        events.append(event)

    async def observe_finish(session_id, *args):
        ready.append(session_id)
        await finish(session_id, *args)

    async def unexpected_native_copy(**kwargs):
        pytest.fail("native history preparation started")

    async def unexpected_native_cleanup(*args, **kwargs):
        pytest.fail("native history cleanup started without a preparation attempt")

    harness = sm_forking.get_harness(parent.backend)
    monkeypatch.setattr(harness, "prepare_fork_copy", unexpected_native_copy)
    monkeypatch.setattr(harness, "cleanup_incomplete_fork_artifacts", unexpected_native_cleanup)
    monkeypatch.setattr(fork_directory.CopyDirectory, "copy_from", change_then_copy)
    monkeypatch.setattr(manager, "_broadcast", observe_publish)
    monkeypatch.setattr(manager.db, "finish_full_copy_fork", observe_finish)
    with pytest.raises(ForkError, match="core.worktree.*self-contained"):
        await manager.duplicate_session(parent.id)

    destination, = copied_dirs
    assert not destination.exists()
    assert not await manager.db.load_incomplete_forks()
    assert [row["id"] for row in await manager.db.load_sessions(include_archived=True)] == [parent.id]
    assert ready == events == []
    assert manager.get_session(parent.id) is parent
    assert not parent._forking


# ----------------------------------------------------------------- happy path


@pytest.mark.parametrize("backend", ["claude-code", "codex"])
@pytest.mark.parametrize("relation", ["same", "descendant", "relative", "symlink"])
async def test_duplicate_refuses_self_contained_destination_before_creating_anything(
    manager, tmp_path, monkeypatch, backend, relation,
):
    repo = _repo(tmp_path)
    parent = await _seed_parent(manager, repo, backend=backend)
    alias = tmp_path / "alias"
    alias.symlink_to(repo, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    destination = {
        "same": str(repo), "descendant": str(repo / "new" / "copy"),
        "relative": "myproject/new/copy", "symlink": str(alias / "new" / "copy"),
    }[relation]
    monkeypatch.setattr(manager, "_fork_copy_dest", lambda *_: destination)
    before = await manager.db.load_sessions(include_archived=True)
    with pytest.raises(ForkError) as refused:
        await manager.duplicate_session(parent.id)
    assert refused.value.reason == "invalid_copy_destination"
    assert await manager.db.load_sessions(include_archived=True) == before
    assert not (repo / "new").exists()
    assert (repo / "code.py").read_text() == "print('hi')\n"
    assert manager.get_session(parent.id) is parent and not parent._forking


async def test_default_copy_base_inside_source_is_rejected_without_creating_base(manager, tmp_path, monkeypatch):
    owner = (await manager.db.get_system_agent())["user_id"]
    source = paths_for(owner).workspace.parent
    parent = await _seed_parent(manager, source)
    before = await manager.db.load_sessions(include_archived=True)
    with pytest.raises(ForkError) as refused:
        await manager.duplicate_session(parent.id)
    assert refused.value.reason == "invalid_copy_destination"
    assert not (source / "fork").exists()
    assert await manager.db.load_sessions(include_archived=True) == before


@pytest.mark.parametrize("backend", ["claude-code", "codex"])
async def test_duplicate_accepts_relative_sibling_with_same_name_prefix(manager, tmp_path, monkeypatch, backend):
    repo = _repo(tmp_path)
    parent = await _seed_parent(manager, repo, backend=backend)
    base = paths_for(parent.user_id).workspace.parent / "fork"
    base.mkdir()
    monkeypatch.chdir(base)
    monkeypatch.setattr(manager, "_fork_copy_dest", lambda *_: "myproject-copy")
    fork = await manager.duplicate_session(parent.id)
    copied = Path(fork.working_dir)
    assert copied == base / "myproject-copy"
    assert (copied / "code.py").read_text() == "print('hi')\n"
    assert (copied / "pkg" / "mod.py").read_text() == "X = 1\n"
    (copied / "code.py").write_text("Independent edit")
    assert (repo / "code.py").read_text() == "print('hi')\n"
    assert manager.get_session(parent.id) is parent


@pytest.mark.asyncio
async def test_duplicate_copies_dir_and_history(manager, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    parent = await _seed_parent(manager, repo)

    fork = await manager.duplicate_session(parent.id, label="my fork")

    # New, independent session under the same lineage.
    assert fork.origin == "fork"
    assert fork.forked_from_session_id == parent.id
    assert fork.name == "my fork"
    assert fork.fork_status == "ready"
    # fork_after_seq stays = last copied seq (the HISTORY_REPLAY cutoff); the
    # full-copy flag is what suppresses the rewind badge in the UI.
    assert fork.fork_after_seq == 5
    assert json.loads(fork.fork_metadata)["full_copy"] is True

    # The independent copy lives next to the account workspace.
    dest = Path(fork.working_dir)
    assert dest != repo
    assert dest.parent == paths_for(parent.user_id).workspace.parent / "fork"
    assert dest.is_dir()
    assert (dest / "code.py").read_text() == "print('hi')\n"
    assert (dest / "pkg" / "mod.py").read_text() == "X = 1\n"

    # Edits to the copy don't touch the original.
    (dest / "code.py").write_text("changed\n")
    assert (repo / "code.py").read_text() == "print('hi')\n"

    # Whole history carried (3 user/assistant pairs = 6 messages).
    copied = await manager.db.load_messages(fork.id)
    assert [m["seq"] for m in copied] == [0, 1, 2, 3, 4, 5]
    assert fork._message_count == 6


@pytest.mark.asyncio
async def test_duplicate_native_copy_when_parent_has_transcript(manager, tmp_path, monkeypatch):
    # When the parent has a real Claude transcript, the fork copies it and
    # resumes natively (no history replay): fork_needs_replay is False and the
    # fork gets a fresh resume id pointing at the copied transcript.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    from server.harness import claude_code as cc

    repo = _repo(tmp_path)
    parent = await _seed_parent(manager, repo)
    # Pretend the parent ran turns: give it a resume id + a native transcript.
    parent.claude_session_id = "parent-sid"
    await manager.db.update_session_field(parent.id, claude_session_id="parent-sid")
    pdir = cc._claude_project_dir(str(repo))
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "parent-sid.jsonl").write_text(
        json.dumps({"type": "user", "sessionId": "parent-sid", "cwd": str(repo),
                    "message": "remember X"}) + "\n"
    )

    fork = await manager.duplicate_session(parent.id)

    assert fork.fork_needs_replay is False
    assert fork.claude_session_id not in (None, "parent-sid")
    # The copied transcript exists at the fork's own slug under its new id.
    copied = cc._claude_project_dir(fork.working_dir) / f"{fork.claude_session_id}.jsonl"
    assert copied.is_file()
    rows = [json.loads(line) for line in copied.read_text().splitlines() if line]
    assert {r["sessionId"] for r in rows} == {fork.claude_session_id}
    assert {r["cwd"] for r in rows if "cwd" in r} == {fork.working_dir}


@pytest.mark.asyncio
async def test_duplicate_pins_cleanup_credential_for_codex(manager, tmp_path, monkeypatch):
    # A Codex fork pins the fork-time effective credential id in fork_metadata so
    # the startup sweep can find the right CODEX_HOME even if the agent's
    # credential later changes. Claude (no per-credential store) does NOT pin.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    cx_parent = await _seed_parent(manager, repo, backend="codex")
    await manager.db.save_credential("cred-1", "codex", "owned", "oauth", "", "now", user_id=cx_parent.user_id)
    cx_parent.credential_id = "cred-1"
    await manager.db.update_session_field(cx_parent.id, credential_id="cred-1")
    cx_fork = await manager.duplicate_session(cx_parent.id)
    assert json.loads(cx_fork.fork_metadata)["cleanup_credential_id"] == "cred-1"

    b = tmp_path / "b"
    b.mkdir()
    cl_parent = await _seed_parent(manager, _repo(b), backend="claude-code")
    await manager.db.save_credential("cred-2", "claude-code", "owned", "oauth", "", "now", user_id=cl_parent.user_id)
    cl_parent.credential_id = "cred-2"
    await manager.db.update_session_field(cl_parent.id, credential_id="cred-2")
    cl_fork = await manager.duplicate_session(cl_parent.id)
    assert "cleanup_credential_id" not in json.loads(cl_fork.fork_metadata)


@pytest.mark.asyncio
async def test_duplicate_leaves_parent_untouched(manager, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    parent = await _seed_parent(manager, repo)

    fork = await manager.duplicate_session(parent.id)

    # Parent stays live (NOT archived — the key difference from /rewind).
    assert parent.id in manager.sessions
    assert parent.working_dir == str(repo)
    rows = {r["id"]: r for r in await manager.db.load_sessions(include_archived=True)}
    assert rows[parent.id]["archived"] == 0
    assert rows[fork.id]["archived"] == 0
    # Default label is "<parent> (fork)".
    assert fork.name == "Parent (fork)"


@pytest.mark.asyncio
async def test_duplicate_broadcasts_session_forked(manager, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    parent = await _seed_parent(manager, repo)

    events = []
    owners = []

    async def capture(event, *, user_id=None):
        events.append(event)
        owners.append(user_id)

    monkeypatch.setattr(manager, "_broadcast", capture)

    fork = await manager.duplicate_session(parent.id)
    evt = next(e for e in events if e.get("type") == "session_forked")
    assert evt["parent_session_id"] == parent.id
    assert evt["fork_session_id"] == fork.id
    assert evt["name"] == fork.name
    assert owners == [parent.user_id]


async def _noop():
    return None


# ------------------------------------------------------------------- guards


@pytest.mark.asyncio
async def test_duplicate_unknown_parent(manager):
    with pytest.raises(ForkError) as ei:
        await manager.duplicate_session("nope")
    assert ei.value.reason == "parent_not_found"
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_duplicate_refused_active_task(manager, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    parent = await _seed_parent(manager, repo)

    await manager.initialize(manager.db)
    started, release = asyncio.Event(), asyncio.Event()

    class HeldParent(FakeRunBase):
        async def _start(self, *_args, **_kwargs):
            started.set()

        async def _stream(self):
            await release.wait()
            yield HarnessEvent(type="result")

        async def interrupt(self):
            release.set()

        async def _stop_process(self):
            release.set()

    monkeypatch.setattr(manager, "_make_run", model_run_factory(lambda *_args, **_kwargs: HeldParent()))
    receipt = await manager.start_message(parent.id, "Keep this parent active")
    try:
        await asyncio.wait_for(started.wait(), 1)
        with pytest.raises(ForkError) as ei:
            await manager.duplicate_session(parent.id)
        assert ei.value.reason == "fork_blocked_parent_turn_active"
    finally:
        await manager.interrupt(parent.id)
        await asyncio.wait_for(receipt.wait(), 1)


@pytest.mark.asyncio
async def test_duplicate_refused_queued_message(manager, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    parent = await _seed_parent(manager, repo)
    await manager.interrupt(parent.id)
    queued = await manager.start_message(parent.id, "later", source="bg")
    assert manager.dispatch_snapshot(parent.id)["items"][0]["request_id"] == queued.request_id
    with pytest.raises(ForkError) as ei:
        await manager.duplicate_session(parent.id)
    assert ei.value.reason == "fork_blocked_parent_turn_active"


@pytest.mark.asyncio
async def test_duplicate_backend_not_supported(manager, tmp_path, monkeypatch):
    from server.sessions import owner_runtime

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    parent = await _seed_parent(manager, repo)

    class _NoFork:
        can_fork = False
        backend_kind = "claude_code"

    monkeypatch.setattr(owner_runtime, "get_harness", lambda b: _NoFork())
    from server.harness import BackendForkNotSupported

    with pytest.raises(BackendForkNotSupported):
        await manager.duplicate_session(parent.id)


@pytest.mark.asyncio
async def test_duplicate_prepare_fork_failure_compensates(manager, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    parent = await _seed_parent(manager, repo)

    harness = sm_forking.get_harness(parent.backend)

    async def boom(*a, **k):
        raise RuntimeError("prepare blew up")

    async def fake_cleanup(*a, **k):
        return None

    monkeypatch.setattr(harness, "prepare_fork_copy", boom)
    monkeypatch.setattr(harness, "cleanup_incomplete_fork_artifacts", fake_cleanup)

    with pytest.raises(RuntimeError):
        await manager.duplicate_session(parent.id)

    # Compensation: no orphan session row, no orphan copied dir, parent freed.
    rows = {r["id"]: r for r in await manager.db.load_sessions(include_archived=True)}
    assert parent.id in rows
    assert len(rows) == 1  # only the parent
    fork_base = paths_for(parent.user_id).workspace.parent / "fork"
    assert not fork_base.exists() or not any(fork_base.iterdir())
    assert parent._forking is False


# -------------------------------------------------------------- route layer


@pytest.fixture
async def client(tmp_path):
    db = Database(":memory:")
    await db.initialize()
    _, bearer = await create_account(db, extra_roots=[tmp_path])
    global_sm.sessions.clear()
    global_sm._admission_closed = False
    await global_sm.initialize(db)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {bearer}"}) as c:
        yield c
    await db.close()


@pytest.mark.parametrize("legacy_state", ["consumed_receipt", "no_receipt", "deleted_source"])
async def test_legacy_copy_archive_uses_current_account_directory_grant(client, tmp_path, monkeypatch, legacy_state):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    db = global_sm.db
    assert db is not None
    owner, owner_token = await create_account(db, username="legacy-copy-owner")
    other, other_token = await create_account(db, username="legacy-copy-neighbor")
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    other_headers = {"Authorization": f"Bearer {other_token}"}
    owner_agent = await db.get_system_agent(owner["id"])
    other_agent = await db.get_system_agent(other["id"])
    parent = await global_sm.create_session(owner_agent["id"], "Source", backend="claude-code")
    await db.append_message(session_id=parent.id, seq=0, role="user", type="text", content="Copied conversation")

    # A ready copy saved by the old implementation uses the global layout.
    fork_id = "legacyfork01"
    copy = tmp_path / "home" / ".octopus" / "fork" / f"work-{fork_id}"
    copy.mkdir(parents=True)
    (copy / "notes.txt").write_text("legacy working copy")
    sibling = copy.with_name("unrelated-copy")
    sibling.mkdir()
    native = _claude_project_dir(str(copy)) / "native.jsonl"
    native.parent.mkdir(parents=True)
    transcript = json.dumps({"type": "user", "cwd": str(copy), "sessionId": "native",
                             "message": {"role": "user", "content": "Copied conversation"}}) + "\n"
    native.write_text(transcript)
    metadata = {"full_copy": True, "duplicated_from": parent.name}
    if legacy_state != "no_receipt":
        metadata["owned_copy_dir"] = str(copy)
    await db.create_fork_session(
        fork_id=fork_id, name="Legacy copy", working_dir=str(copy), created_at=parent.created_at,
        parent_id=parent.id, backend=parent.backend, user_id=owner["id"], agent_id=owner_agent["id"],
        credential_id=None, resume_id="native", fork_after_seq=0, fork_metadata=json.dumps(metadata),
    )
    await db.update_session_field(fork_id, fork_status="ready", fork_needs_replay=False, archived=True)
    url = f"/api/sessions/{fork_id}"

    async def restore(operation_id):
        state = await client.get(f"{url}/restoration", headers=owner_headers)
        assert state.status_code == 200, state.text
        response = await client.post(f"{url}/unarchive", headers=owner_headers, json={
            "operation_id": operation_id, "service_generation": state.json()["service_generation"],
        })
        assert response.status_code == 200 and response.json()["state"] == "finished", response.text

    await restore("legacy-fixture")
    if legacy_state == "consumed_receipt":
        await _record_fork_installation(db, global_sm.sessions[fork_id])
        assert "owned_copy_dir" not in json.loads(global_sm.sessions[fork_id].fork_metadata)
    if legacy_state == "deleted_source":
        assert (await client.delete(f"/api/sessions/{parent.id}", headers=owner_headers)).status_code == 204

    events = []
    global_sm.on_broadcast(fork_id, lambda event: asyncio.sleep(0, result=events.append(event)))
    try:
        refused = await client.post(f"{url}/archive", headers=owner_headers)
        assert refused.status_code == 500, refused.text
        receipt = refused.json()
        assert receipt["archived"] is True and receipt["old_session_id"] == fork_id
        assert receipt["new_session_id"] is None and receipt["failure_stage"] == "successor_creation"
        detail = (await client.get(url, headers=owner_headers)).json()
        assert detail["archived"] is True and detail["working_dir"] == str(copy)
        assert [message["content"] for message in detail["messages"]] == ["Copied conversation"]
        notices = [event for event in events if event["type"] == "session_archived"]
        assert len(notices) == 1 and notices[0]["archive_result"] == receipt

        grants = f"/api/auth/users/{owner['id']}"
        opened = await client.post(f"{grants}/workspace-edits", json={"flow_id": "legacy-copy-grant"})
        assert opened.status_code == 200, opened.text
        granted = await client.put(f"{grants}/extra-roots", json={
            "flow_id": "legacy-copy-grant", "service_generation": opened.json()["service_generation"],
            "sequence": 1, "extra_roots": [str(copy)],
        })
        assert granted.status_code == 200 and granted.json()["extra_roots"] == [str(copy)], granted.text
        assert (await client.post("/api/sessions", headers=owner_headers, json={
            "agent_id": owner_agent["id"], "working_dir": str(sibling),
        })).status_code == 400
        assert (await client.post("/api/sessions", headers=other_headers, json={
            "agent_id": other_agent["id"], "working_dir": str(copy),
        })).status_code == 400

        await restore("after-directory-grant")
        assert (await client.post(f"{url}/archive", headers=other_headers)).status_code == 404
        created = await client.post(f"{url}/archive", headers=owner_headers)
        assert created.status_code == 201, created.text
        successor = created.json()
        assert successor["id"] != fork_id and successor["working_dir"] == str(copy)
        assert successor["agent_id"] == owner_agent["id"] and successor["message_count"] == 0
        assert (await client.get(f"/api/sessions/{successor['id']}", headers=other_headers)).status_code == 404
        notices = [event for event in events if event["type"] == "session_archived"]
        assert len(notices) == 2 and notices[1]["new_session_id"] == successor["id"]
        assert (copy / "notes.txt").read_text() == "legacy working copy"
        assert native.read_text() == transcript
        assert str(copy) in receipt["message"] and "outside your workspace" in receipt["message"]
        assert "Ask an administrator to verify ownership and grant access to this exact directory" in receipt["message"]
        assert "restore the archived session and run /archive again" in receipt["message"]
        assert "start a new session in an allowed directory" in receipt["message"]
    finally:
        global_sm.remove_broadcast(fork_id)


@pytest.mark.asyncio
async def test_duplicate_route(client, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    parent = await _seed_parent(global_sm, repo)

    H = {"Authorization": client.headers["Authorization"]}
    res = await client.post(
        f"/api/sessions/{parent.id}/duplicate",
        headers=H,
        json={"label": "routed fork"},
    )
    assert res.status_code == 201
    body = res.json()
    assert body["name"] == "routed fork"
    assert body["forked_from_session_id"] == parent.id
    assert body["id"] != parent.id
    # Parent still listed (not archived).
    listed = (await client.get("/api/sessions", headers=H)).json()
    assert parent.id in {s["id"] for s in listed}


@pytest.mark.asyncio
async def test_duplicate_route_unknown_parent(client):
    res = await client.post(
        "/api/sessions/missing/duplicate",
        headers={"Authorization": client.headers["Authorization"]},
        json={},
    )
    assert res.status_code == 404
    assert res.json()["detail"]["reason"] == "parent_not_found"


@pytest.mark.asyncio
async def test_duplicate_route_exposes_full_copy_flag(client, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    parent = await _seed_parent(global_sm, repo)
    H = {"Authorization": client.headers["Authorization"]}
    body = (
        await client.post(f"/api/sessions/{parent.id}/duplicate", headers=H, json={})
    ).json()
    # The UI distinguishes a copy-dir duplicate from a rewind via this flag.
    assert body["fork_is_full_copy"] is True
    # The parent (a normal session) reports False.
    detail = (await client.get(f"/api/sessions/{parent.id}", headers=H)).json()
    assert detail["fork_is_full_copy"] is False


# ----------------------------------------------------- replay-cutoff (Vera #1)


@pytest.mark.asyncio
async def test_duplicate_replay_cutoff_covers_full_history(manager, tmp_path, monkeypatch):
    # The HISTORY_REPLAY first turn injects parent messages with
    # seq <= fork_after_seq. For a duplicate that cutoff MUST cover the whole
    # carried-over conversation — otherwise the fork continues with no context.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    parent = await _seed_parent(manager, repo, n_user=3)  # 6 messages, seq 0..5

    fork = await manager.duplicate_session(parent.id)
    assert fork.fork_after_seq == 5  # last copied seq, not -1

    replayed = [
        m for m in await manager.db.load_messages(fork.id)
        if m["seq"] <= fork.fork_after_seq
    ]
    assert [m["seq"] for m in replayed] == [0, 1, 2, 3, 4, 5]


# ------------------------------------------------ copy/cleanup leaks (Vera #3/#4)


@pytest.mark.asyncio
async def test_failed_rewind_recovery_preserves_its_full_copy_parent(manager, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    parent = await _seed_parent(manager, _repo(tmp_path))
    copied = await manager.duplicate_session(parent.id)
    sentinel = Path(copied.working_dir) / "user-change.txt"
    sentinel.write_text("Changes made after the full copy must survive.")
    harness = sm_forking.get_harness(copied.backend)
    cleanup = harness.cleanup_incomplete_fork_artifacts
    update = manager.db.update_session_field

    async def fail_replay_metadata(session_id, **fields):
        if session_id != copied.id and fields.get("fork_needs_replay"):
            raise OSError("rewind metadata save failed")
        return await update(session_id, **fields)

    async def fail_cleanup(*args, **kwargs):
        raise OSError("temporary artifact cleanup failure")

    monkeypatch.setattr(manager.db, "update_session_field", fail_replay_metadata)
    monkeypatch.setattr(harness, "cleanup_incomplete_fork_artifacts", fail_cleanup)
    with pytest.raises(OSError, match="rewind metadata save failed"):
        await manager.fork_session(copied.id, 2)
    retained = await manager.db.load_incomplete_forks()
    assert len(retained) == 1 and retained[0]["resume_id"] is None
    await manager._recover_incomplete_forks()
    assert await manager.db.load_incomplete_forks() == retained
    assert sentinel.read_text() == "Changes made after the full copy must survive."

    monkeypatch.setattr(harness, "cleanup_incomplete_fork_artifacts", cleanup)
    recovered = SessionManager()
    await recovered.initialize(manager.db)
    assert sentinel.read_text() == "Changes made after the full copy must survive."
    assert recovered.get_session(copied.id, copied.user_id) is not None
    assert not await manager.db.load_incomplete_forks()
    rows = {row["id"]: row for row in await manager.db.load_sessions(include_archived=True)}
    assert rows[copied.id]["fork_status"] == "ready"
    assert rows[copied.id]["archived"] == 0


@pytest.mark.asyncio
async def test_duplicate_does_not_copy_before_recovery_row_is_committed(manager, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    parent = await _seed_parent(manager, _repo(tmp_path))
    copied = []
    copy = fork_directory.CopyDirectory.copy_from

    def observe_copy(directory, src):
        copied.append(directory.path)
        copy(directory, src)

    async def fail_insert(**kwargs):
        raise OSError("session insert failed")

    monkeypatch.setattr(fork_directory.CopyDirectory, "copy_from", observe_copy)
    monkeypatch.setattr(manager.db, "create_fork_session", fail_insert)
    with pytest.raises(OSError, match="session insert failed"):
        await manager.duplicate_session(parent.id)
    assert copied == []
    assert parent._forking is False
    assert Path(parent.working_dir).is_dir()


@pytest.mark.asyncio
async def test_duplicate_preserves_an_existing_destination(manager, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    parent = await _seed_parent(manager, _repo(tmp_path))
    destination = Path(manager._fork_copy_dest(parent.working_dir, "existing", parent.user_id))
    destination.mkdir(parents=True)
    sentinel = destination / "existing.txt"
    sentinel.write_text("This directory was not created by the new fork.")
    monkeypatch.setattr(manager, "_fork_copy_dest", lambda *_: str(destination))

    with pytest.raises(ForkError) as failure:
        await manager.duplicate_session(parent.id)
    assert failure.value.reason == "copy_failed"
    assert sentinel.read_text() == "This directory was not created by the new fork."
    recovered = SessionManager()
    await recovered.initialize(manager.db)
    assert sentinel.read_text() == "This directory was not created by the new fork."
    assert parent._forking is False


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_command", [False, True], ids=["waiter", "original-command"])
async def test_duplicate_cancellation_retains_its_copy_writer(manager, tmp_path, monkeypatch, cancel_command):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    parent = await _seed_parent(manager, _repo(tmp_path))
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    destinations = []
    copy = fork_directory.CopyDirectory.copy_from

    def held_copy(directory, src):
        destinations.append(str(directory.path))
        loop.call_soon_threadsafe(started.set)
        if not release.wait(5):
            raise TimeoutError("copy barrier was not released")
        copy(directory, src)

    monkeypatch.setattr(fork_directory.CopyDirectory, "copy_from", held_copy)
    waiter = asyncio.create_task(manager.duplicate_session(parent.id))
    command = None
    try:
        await asyncio.wait_for(started.wait(), 1)
        command = next(task for task in manager._owned_tasks if task.get_name().startswith("agent-operation-"))
        (command if cancel_command else waiter).cancel()
        # Let cancellation reach the actual copy await without releasing it.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert parent._forking is True
        pending = await manager.db.load_incomplete_forks()
        assert len(pending) == 1
        assert pending[0]["working_dir"] == destinations[0]
    finally:
        release.set()
        await asyncio.gather(waiter, return_exceptions=True)
        if command is not None:
            await asyncio.gather(command, return_exceptions=True)

    if cancel_command:
        assert not Path(destinations[0]).exists()
        assert not await manager.db.load_incomplete_forks()
    else:
        rows = [row for row in await manager.db.load_sessions() if row["id"] != parent.id]
        assert len(rows) == 1 and rows[0]["fork_status"] == "ready"
        assert (Path(destinations[0]) / "code.py").read_text() == "print('hi')\n"


@pytest.mark.asyncio
async def test_duplicate_allocation_failure_releases_parent(manager, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    parent = await _seed_parent(manager, _repo(tmp_path))
    allocate = manager._fork_copy_dest

    def fail_allocate(*args):
        raise PermissionError("cannot create copy base")

    monkeypatch.setattr(manager, "_fork_copy_dest", fail_allocate)
    with pytest.raises(PermissionError, match="cannot create copy base"):
        await manager.duplicate_session(parent.id)
    assert parent._forking is False
    monkeypatch.setattr(manager, "_fork_copy_dest", allocate)
    copied = await manager.duplicate_session(parent.id)
    assert copied.fork_status == "ready"


@pytest.mark.parametrize("interruption", ["before-retirement", "after-retirement"])
async def test_duplicate_directory_cleanup_failure_retains_recovery(manager, tmp_path, monkeypatch, interruption):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    parent = await _seed_parent(manager, _repo(tmp_path))
    harness = sm_forking.get_harness(parent.backend)

    async def fail_prepare(*args, **kwargs):
        raise RuntimeError("native preparation failed")

    monkeypatch.setattr(harness, "prepare_fork_copy", fail_prepare)
    recovered = SessionManager()
    try:
        with monkeypatch.context() as injected:
            def fail_remove(*args, **kwargs):
                raise PermissionError("cannot remove copied directory")

            if interruption == "before-retirement":
                injected.setattr(sm_forking, "remove_copy", fail_remove)
            else:
                injected.setattr(fork_directory, "_empty_directory", fail_remove)
            with pytest.raises(PermissionError, match="cannot remove copied directory"):
                await manager.duplicate_session(parent.id)
            pending, = await manager.db.load_incomplete_forks()
            destination = Path(pending["working_dir"])
            retained = destination
            if interruption == "after-retirement":
                creation = json.loads(pending["fork_metadata"])["copy_creation"]
                retained = destination.parent / f".fork-remove-{creation}" / "copy"
                assert not destination.exists()
            assert (retained / "code.py").read_text() == "print('hi')\n"
            assert manager.get_session(pending["id"], parent.user_id) is None

            await recovered.initialize(manager.db)
            assert [row["id"] for row in await manager.db.load_incomplete_forks()] == [pending["id"]]
            assert retained.exists()
        await recovered._recover_incomplete_forks()
        await recovered._recover_incomplete_forks()
        assert not retained.exists()
        assert not await manager.db.load_incomplete_forks()
        assert (Path(parent.working_dir) / "code.py").read_text() == "print('hi')\n"
    finally:
        await recovered.close_session_owners()


@pytest.mark.asyncio
async def test_unconfirmed_directory_receipt_does_not_start_copy_or_grant_cleanup(manager, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    parent = await _seed_parent(manager, _repo(tmp_path))
    update = manager.db.update_session_field

    async def fail_receipt(session_id, **fields):
        if "owned_copy_dir" in fields.get("fork_metadata", ""):
            raise OSError("directory receipt was not confirmed")
        await update(session_id, **fields)

    monkeypatch.setattr(manager.db, "update_session_field", fail_receipt)
    with pytest.raises(OSError, match="directory receipt was not confirmed"):
        await manager.duplicate_session(parent.id)
    row, = await manager.db.load_incomplete_forks()
    destination = Path(row["working_dir"])
    staging, = destination.parent.glob(".fork-create-*")
    assert not destination.exists() and not list((staging / "copy").iterdir())
    assert "owned_copy_dir" not in json.loads(row["fork_metadata"])
    monkeypatch.setattr(manager.db, "update_session_field", update)
    recovered = SessionManager()
    await recovered.initialize(manager.db)
    assert not destination.exists() and (staging / "copy").is_dir()
    assert [row["id"] for row in await manager.db.load_incomplete_forks()] == [row["id"]]
    assert parent._forking is False


@pytest.mark.parametrize("at", ["receipt", "writer", "ready-lock", "ready-sql", "ready-driver", "ready-commit", "lost-ready-ack"])
async def test_duplicate_never_writes_or_publishes_a_redirected_copy(manager, tmp_path, monkeypatch, at):
    parent = await _seed_parent(manager, _repo(tmp_path))
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    sentinel = foreign / "keep.txt"
    sentinel.write_text("Unrelated data")
    writes = []
    published = []
    displaced = tmp_path / "displaced"
    redirected = None
    update = manager.db.update_session_field
    copy = fork_directory.CopyDirectory.copy_from
    finish = manager.db.finish_full_copy_fork
    reached_ready = asyncio.Event()

    def redirect(destination):
        nonlocal redirected
        redirect_parent = at == "receipt"
        redirected = destination.parent if redirect_parent else destination
        if at != "writer":
            redirected.rename(displaced)
        if at.startswith("ready") or at == "lost-ready-ack":
            foreign.rename(redirected)
        else:
            redirected.symlink_to(foreign, target_is_directory=True)

    async def update_receipt(session_id, **fields):
        await update(session_id, **fields)
        metadata = json.loads(fields.get("fork_metadata") or "{}")
        if at == "receipt" and metadata.get("owned_copy_dir"):
            redirect(Path(metadata["owned_copy_dir"]["path"]))

    def observe_copy(directory, source):
        writes.append(directory.path)
        if at == "writer":
            redirect(directory.path)
        copy(directory, source)

    async def observe_finish(session_id, *args):
        if at in {"ready-commit", "lost-ready-ack"}:
            await dispatch(manager.db.conn._conn.set_authorizer, authorize)
        reached_ready.set()
        await finish(session_id, *args)
        if at == "lost-ready-ack":
            raise asyncio.CancelledError

    execute = Transaction.execute
    dispatch = manager.db.conn._execute

    async def observe_sql(transaction, sql, parameters=()):
        result = await execute(transaction, sql, parameters)
        if at == "ready-sql" and "fork_status='ready'" in sql:
            assert result.rowcount == 1
            redirect(writes[0])
        return result

    async def observe_driver(fn, *args, **kwargs):
        if (at == "ready-driver" and reached_ready.is_set() and redirected is None
                and fn.__name__ == "commit"):
            redirect(writes[0])
        return await dispatch(fn, *args, **kwargs)

    async def publish(event):
        published.append(event)

    def authorize(action, first, second, database, source):
        if (at in {"ready-commit", "lost-ready-ack"} and reached_ready.is_set()
                and redirected is None and action == sqlite3.SQLITE_TRANSACTION and first == "COMMIT"):
            redirect(writes[0])
        return sqlite3.SQLITE_OK

    monkeypatch.setattr(manager.db, "update_session_field", update_receipt)
    monkeypatch.setattr(manager.db, "finish_full_copy_fork", observe_finish)
    monkeypatch.setattr(Transaction, "execute", observe_sql)
    monkeypatch.setattr(manager.db.conn, "_execute", observe_driver)
    monkeypatch.setattr(fork_directory.CopyDirectory, "copy_from", observe_copy)
    manager.on_broadcast("redirected-copy", publish, parent.user_id)
    if at == "ready-lock":
        # Hold native completion until the ready transaction will wait on an
        # unrelated writer, then replace the bound directory before it enters.
        entered, release = asyncio.Event(), asyncio.Event()
        harness = sm_forking.get_harness(parent.backend)
        prepare = harness.prepare_fork_copy

        async def hold_prepare(**kwargs):
            artifact = await prepare(**kwargs)
            entered.set()
            await release.wait()
            return artifact

        monkeypatch.setattr(harness, "prepare_fork_copy", hold_prepare)
        command = asyncio.create_task(manager.duplicate_session(parent.id))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            async with manager.db.transaction():
                release.set()
                await asyncio.wait_for(reached_ready.wait(), 3)
                assert (writes[0] / "code.py").exists()
                redirect(writes[0])
        finally:
            release.set()
        with pytest.raises(ForkError, match="cwd changed"):
            await asyncio.wait_for(command, 3)
    elif at == "lost-ready-ack":
        with pytest.raises(asyncio.CancelledError):
            await manager.duplicate_session(parent.id)
        ready, = [row for row in await manager.db.load_sessions() if row["id"] != parent.id]
        assert ready["fork_status"] == "ready"
        assert manager.get_session(ready["id"], parent.user_id) is None
    else:
        with pytest.raises(ForkError, match="cwd changed"):
            await manager.duplicate_session(parent.id)
    recovered = SessionManager()
    await recovered.initialize(manager.db)
    incomplete = await manager.db.load_incomplete_forks()
    if at == "lost-ready-ack":
        assert not incomplete
        pending, = [row for row in await manager.db.load_sessions() if row["id"] != parent.id]
        assert pending["fork_status"] == "ready"
        assert json.loads(pending["fork_metadata"])["copy_delivery_pending"] is True
    else:
        pending, = incomplete
        assert pending["fork_status"] == "initializing"
    assert manager.get_session(pending["id"], parent.user_id) is None
    assert not any(event["type"] == "session_forked" for event in published)
    assert redirected is not None
    retained = redirected if at.startswith("ready") or at == "lost-ready-ack" else foreign
    assert {path.name for path in retained.iterdir()} == {"keep.txt"}
    assert (retained / "keep.txt").read_text() == "Unrelated data"
    if at == "receipt":
        staging, = displaced.glob(".fork-create-*")
        assert not writes and not list((staging / "copy").iterdir())
    elif at != "writer":
        assert (displaced / "code.py").read_text() == "print('hi')\n"

    assert recovered.get_session(pending["id"], parent.user_id) is None
    assert redirected.is_dir() and (retained / "keep.txt").read_text() == "Unrelated data"


@pytest.mark.parametrize("changed_parent", ["fork-parent", "account-root", "new-fork-parent"])
async def test_duplicate_refuses_parent_replacement_during_initializing_insert(
    manager, tmp_path, monkeypatch, changed_parent,
):
    account, _ = await create_account(manager.db, username="replaced-fork-parent", extra_roots=[tmp_path])
    agent = await manager.db.get_system_agent(account["id"])
    parent = await manager.create_session(agent["id"], "Parent", str(_repo(tmp_path)))
    account_root = paths_for(account["id"]).workspace.parent
    fork_root = account_root / "fork"
    if changed_parent == "fork-parent":
        fork_root.mkdir()
    binding = account_root if changed_parent == "account-root" else fork_root
    displaced = tmp_path / "displaced"
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "keep.txt").write_text("Unrelated data")
    published = []
    native_preparations = []
    harness = sm_forking.get_harness(parent.backend)
    prepare = harness.prepare_fork_copy

    async def publish(event):
        published.append(event)

    async def observe_native_preparation(**kwargs):
        native_preparations.append(kwargs["dest_working_dir"])
        return await prepare(**kwargs)

    def swap_on_insert(action, table, *_):
        if action == sqlite3.SQLITE_INSERT and table == "sessions":
            if changed_parent != "new-fork-parent":
                binding.rename(displaced)
            foreign.rename(binding)
        return sqlite3.SQLITE_OK

    monkeypatch.setattr(harness, "prepare_fork_copy", observe_native_preparation)
    manager.on_broadcast("replaced-fork-parent", publish, account["id"])
    dispatch = manager.db.conn._execute
    set_authorizer = manager.db.conn._conn.set_authorizer
    await dispatch(set_authorizer, swap_on_insert)
    try:
        with pytest.raises(ForkError, match="parent.*changed") as rejected:
            await manager.duplicate_session(parent.id, user_id=account["id"])
    finally:
        await dispatch(set_authorizer, None)

    assert rejected.value.reason == "invalid_copy_destination"
    pending, = await manager.db.load_incomplete_forks()
    assert pending["fork_status"] == "initializing"
    assert "owned_copy_dir" not in json.loads(pending["fork_metadata"])
    assert manager.get_session(pending["id"], account["id"]) is None
    assert displaced.is_dir() == (changed_parent != "new-fork-parent")
    assert {item.name for item in binding.iterdir()} == {"keep.txt"}
    assert (binding / "keep.txt").read_text() == "Unrelated data"
    if changed_parent == "new-fork-parent":
        assert not list(account_root.glob(".fork-parent-*"))
        assert not list(account_root.parent.glob(".fork-parent-*"))
    assert not native_preparations
    assert not any(event["type"] == "session_forked" for event in published)


async def test_concurrent_first_forks_share_the_account_root(manager, tmp_path, monkeypatch):
    account, _ = await create_account(manager.db, username="concurrent-first-forks")
    agent = await manager.db.get_system_agent(account["id"])
    workspace = paths_for(account["id"]).workspace
    (workspace / "note.txt").write_text("original")
    first = await manager.create_session(agent["id"], "First")
    second = await manager.create_session(agent["id"], "Second")
    root = workspace.parent / "fork"
    assert not root.exists()

    first_insert = asyncio.Event()
    allow_insert = asyncio.Event()
    first_native = asyncio.Event()
    allow_native = asyncio.Event()
    create = manager.db.create_fork_session
    harness = sm_forking.get_harness(first.backend)
    prepare = harness.prepare_fork_copy
    first_id = None

    async def hold_first_insert(**kwargs):
        nonlocal first_id
        if kwargs["parent_id"] == first.id:
            first_id = kwargs["resume_id"]
            first_insert.set()
            await allow_insert.wait()
        await create(**kwargs)

    async def hold_first_native(**kwargs):
        if kwargs["new_resume_id"] == first_id:
            first_native.set()
            await allow_native.wait()
        return await prepare(**kwargs)

    monkeypatch.setattr(manager.db, "create_fork_session", hold_first_insert)
    monkeypatch.setattr(harness, "prepare_fork_copy", hold_first_native)
    duplicate_first = asyncio.create_task(manager.duplicate_session(first.id, user_id=account["id"]))
    duplicate_second = None
    try:
        await asyncio.wait_for(first_insert.wait(), 5)
        assert not root.exists()
        duplicate_second = asyncio.create_task(manager.duplicate_session(second.id, user_id=account["id"]))
        allow_insert.set()
        await asyncio.wait_for(first_native.wait(), 5)
        other = await asyncio.wait_for(asyncio.shield(duplicate_second), 5)
        assert not duplicate_first.done()
    finally:
        allow_insert.set()
        allow_native.set()
        tasks = [duplicate_first] if duplicate_second is None else [duplicate_first, duplicate_second]
        await asyncio.gather(*tasks, return_exceptions=True)

    branch = duplicate_first.result()
    assert branch.id != other.id
    for fork in (branch, other):
        target = Path(fork.working_dir)
        assert target.parent == root
        assert fork.fork_status == "ready"
        assert (target / "note.txt").read_text() == "original"


@pytest.mark.parametrize("shared_users_root", [False, True])
async def test_duplicate_publishes_the_prepared_parent_binding(manager, tmp_path, monkeypatch, shared_users_root):
    account, _ = await create_account(manager.db, username="prepared-fork-parent", extra_roots=[tmp_path])
    agent = await manager.db.get_system_agent(account["id"])
    parent = await manager.create_session(agent["id"], "Parent", str(_repo(tmp_path)))
    account_root = paths_for(account["id"]).workspace.parent
    fork_root = account_root / "fork"
    if shared_users_root:
        account_root.parent.chmod(0o775)
    expected_stage = account_root if shared_users_root else account_root.parent
    assert not fork_root.exists()
    installations = []
    move = fork_directory._move

    def observe_parent_installation(source_parent, source, dest_parent, dest):
        if dest == "fork":
            private = fork_directory.fd_path(source_parent) / source
            staging_parent = os.fstat(source_parent)
            assert os.path.samestat(staging_parent, expected_stage.stat())
            assert staging_parent.st_uid == os.geteuid()
            assert staging_parent.st_mode & 0o022 == 0
            assert os.path.samestat(os.fstat(dest_parent), account_root.stat())
            assert not fork_root.exists()
            assert private.stat().st_mode & 0o777 == 0o700
            assert not list(private.iterdir())
            installations.append(private.stat())
        return move(source_parent, source, dest_parent, dest)

    monkeypatch.setattr(fork_directory, "_move", observe_parent_installation)
    fork = await manager.duplicate_session(parent.id, user_id=account["id"])

    installed, = installations
    assert os.path.samestat(installed, fork_root.stat())
    receipt = json.loads(fork.fork_metadata)["owned_copy_dir"]
    assert receipt["parent"] == [installed.st_dev, installed.st_ino]
    assert (Path(fork.working_dir) / "code.py").is_file()


async def test_duplicate_first_parent_requires_service_controlled_staging(manager, tmp_path):
    account, _ = await create_account(manager.db, username="shared-fork-parent", extra_roots=[tmp_path])
    agent = await manager.db.get_system_agent(account["id"])
    parent = await manager.create_session(agent["id"], "Parent", str(_repo(tmp_path)))
    account_root = paths_for(account["id"]).workspace.parent
    account_root.parent.chmod(0o775)
    account_root.chmod(0o775)
    existing_entries = set(account_root.iterdir())

    with pytest.raises(ForkError, match="staging parent writable only by the service user") as failure:
        await manager.duplicate_session(parent.id, user_id=account["id"])

    assert failure.value.status_code == 409
    assert set(account_root.iterdir()) == existing_entries
    assert not list(account_root.parent.glob(".fork-parent-*"))
    pending, = await manager.db.load_incomplete_forks()
    assert "owned_copy_dir" not in json.loads(pending["fork_metadata"])
    assert manager.get_session(pending["id"], account["id"]) is None


def test_fork_plan_does_not_use_a_fallback_ancestor_as_users_root(tmp_path, monkeypatch):
    users_root = tmp_path / "users"
    account_root = users_root / "account"
    account_root.mkdir(parents=True)
    monkeypatch.setattr(settings, "users_root", str(users_root))
    root = account_root / "fork"

    @contextmanager
    def planned_bindings(path, *, allow_missing=False, start=None):
        # Exercise candidate selection with fixed traversal return values.
        bound = {users_root: tmp_path, root: account_root}[path]
        with fork_directory._directory(str(bound)) as fd:
            yield bound, fd

    monkeypatch.setattr(fork_directory, "_parents", planned_bindings)
    with fork_directory.CopyLocation.planned(root / "copy", "account") as location:
        assert location.planned_staging is None


async def test_duplicate_checks_published_parent_before_allocating_copy(manager, tmp_path, monkeypatch):
    account, _ = await create_account(manager.db, username="check-published-fork-parent", extra_roots=[tmp_path])
    agent = await manager.db.get_system_agent(account["id"])
    source = _repo(tmp_path)
    parent = await manager.create_session(agent["id"], "Parent", str(source))
    account_root = paths_for(account["id"]).workspace.parent
    root = account_root / "fork"
    check_binding = fork_directory._require_parent_binding

    def reject_published_parent(path, fd):
        check_binding(path, fd)
        if path == root:
            assert not list(root.iterdir())
            raise fork_directory.ForkDirectoryError("Parent binding could not be confirmed")

    monkeypatch.setattr(fork_directory, "_require_parent_binding", reject_published_parent)
    with pytest.raises(ForkError, match="parent changed during installation") as failure:
        await manager.duplicate_session(parent.id, user_id=account["id"])

    assert failure.value.status_code == 409
    assert not list(root.iterdir())
    assert not list(account_root.parent.glob(".fork-parent-*"))
    assert (source / "code.py").read_text() == "print('hi')\n"
    pending, = await manager.db.load_incomplete_forks()
    assert "owned_copy_dir" not in json.loads(pending["fork_metadata"])
    assert manager.get_session(pending["id"], account["id"]) is None


async def test_startup_admits_a_committed_copy_with_an_intact_binding(manager, tmp_path, monkeypatch):
    parent = await _seed_parent(manager, _repo(tmp_path))
    finish = manager.db.finish_full_copy_fork

    async def lose_ack(session_id, *args):
        await finish(session_id, *args)
        raise asyncio.CancelledError

    monkeypatch.setattr(manager.db, "finish_full_copy_fork", lose_ack)
    with pytest.raises(asyncio.CancelledError):
        await manager.duplicate_session(parent.id, user_id=parent.user_id)
    row, = [row for row in await manager.db.load_sessions() if row["id"] != parent.id]
    assert manager.get_session(row["id"], parent.user_id) is None

    recovered = SessionManager()
    try:
        await recovered.initialize(manager.db)
        branch = recovered.get_session(row["id"], parent.user_id)
        assert branch is not None and branch.fork_status == "ready"
        assert (Path(branch.working_dir) / "code.py").read_text() == "print('hi')\n"
        saved, = [saved for saved in await manager.db.load_sessions() if saved["id"] == row["id"]]
        assert json.loads(saved["fork_metadata"])["copy_delivery_pending"] is True
    finally:
        await recovered.close_session_owners()


async def test_cold_fork_checks_binding_after_all_async_recovery(manager, tmp_path, monkeypatch):
    parent = await _seed_parent(manager, _repo(tmp_path))
    finish = manager.db.finish_full_copy_fork

    async def lose_ack(session_id, *args):
        await finish(session_id, *args)
        raise asyncio.CancelledError

    monkeypatch.setattr(manager.db, "finish_full_copy_fork", lose_ack)
    with pytest.raises(asyncio.CancelledError):
        await manager.duplicate_session(parent.id, user_id=parent.user_id)
    row, = [row for row in await manager.db.load_sessions() if row["id"] != parent.id]
    copy = Path(row["working_dir"])
    recovered = SessionManager()
    sweep = recovered._recover_incomplete_forks

    async def replace_during_startup():
        await sweep()
        copy.rename(tmp_path / "displaced-fork")
        copy.mkdir()
        (copy / "keep.txt").write_text("Unrelated data")

    async def unexpected_ack(session_id):
        pytest.fail("Startup attempted to acknowledge publication before admission")

    monkeypatch.setattr(recovered, "_recover_incomplete_forks", replace_during_startup)
    monkeypatch.setattr(manager.db, "acknowledge_full_copy_delivery", unexpected_ack)
    try:
        await recovered.initialize(manager.db)
        assert recovered.get_session(row["id"], parent.user_id) is None
        assert (copy / "keep.txt").read_text() == "Unrelated data"
        assert not await manager.db.load_incomplete_forks()
        saved, = [saved for saved in await manager.db.load_sessions() if saved["id"] == row["id"]]
        assert json.loads(saved["fork_metadata"])["copy_delivery_pending"] is True
    finally:
        await recovered.close_session_owners()


@pytest.mark.parametrize("interruption", ["no-input", "publication", "during-confirmation", "after-confirmation"])
async def test_published_native_fork_survives_lost_delivery_ack_and_moved_cwd(manager, tmp_path, monkeypatch, interruption):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    parent = await _seed_parent(manager, _repo(tmp_path))
    parent.claude_session_id = "parent-sid"
    await manager.db.update_session_field(parent.id, claude_session_id=parent.claude_session_id)
    project = _claude_project_dir(parent.working_dir)
    project.mkdir(parents=True)
    (project / "parent-sid.jsonl").write_text(json.dumps({
        "type": "user", "sessionId": "parent-sid", "cwd": parent.working_dir,
    }) + "\n")

    class SettledRun(FakeRunBase):
        async def _start(self, *_args, **_kwargs):
            pass

        async def _stream(self):
            yield HarnessEvent(type="result")

    monkeypatch.setattr(manager, "_make_run", model_run_factory(lambda *args, **kwargs: SettledRun()))
    branch = None

    async def accept_input_then_interrupt_delivery(event, **kwargs):
        nonlocal branch
        if event["type"] == "session_forked":
            branch = manager.get_session(event["fork_session_id"], parent.user_id)
            assert branch is not None and not branch.fork_needs_replay
            if interruption != "no-input":
                receipt = await manager.start_message(branch.id, "Already-used fork", client_request_id="fork-input")
                assert (await asyncio.wait_for(receipt.wait(), 3)).outcome.status == "success"
                assert await manager.db.get_client_submission(branch.id, "fork-input") is not None
            if interruption in {"no-input", "publication"}:
                raise asyncio.CancelledError

    acknowledge = manager.db.acknowledge_full_copy_delivery

    async def interrupt_confirmation(session_id):
        if interruption == "after-confirmation":
            await acknowledge(session_id)
        raise asyncio.CancelledError

    monkeypatch.setattr(manager, "_broadcast", accept_input_then_interrupt_delivery)
    monkeypatch.setattr(manager.db, "acknowledge_full_copy_delivery", interrupt_confirmation)
    with pytest.raises(asyncio.CancelledError):
        await manager.duplicate_session(parent.id, user_id=parent.user_id)
    assert branch is not None
    native = _claude_project_dir(branch.working_dir) / f"{branch.claude_session_id}.jsonl"
    assert native.is_file()
    await manager.close_session_owners()
    Path(branch.working_dir).rename(tmp_path / "moved-copy")
    recovered = SessionManager()
    try:
        await recovered.initialize(manager.db)
        installed = recovered.get_session(branch.id, parent.user_id)
        assert (installed is None) == (interruption == "no-input")
        assert native.is_file()
        submission = await manager.db.get_client_submission(branch.id, "fork-input")
        assert (submission is None) == (interruption == "no-input")
        stored, = [row for row in await manager.db.load_sessions() if row["id"] == branch.id]
        assert stored["fork_status"] == "ready"
        assert bool(json.loads(stored["fork_metadata"]).get("copy_delivery_pending")) == (
            interruption != "after-confirmation"
        )
        assert (tmp_path / "moved-copy" / "code.py").is_file()
    finally:
        await recovered.close_session_owners()


async def test_duplicate_never_populates_an_unrelated_subdirectory(manager, tmp_path, monkeypatch):
    source = _repo(tmp_path)
    (source / "nested").mkdir(mode=0o700)
    (source / "nested" / "notes.txt").write_text("Source notes")
    parent = await _seed_parent(manager, source)
    foreign = tmp_path / "unrelated-subdirectory"
    foreign.mkdir()
    foreign.chmod(0o755)
    (foreign / "keep.txt").write_text("Unrelated data")
    os.utime(foreign, ns=(1_000_000_000, 2_000_000_000))
    binding = None
    update = manager.db.update_session_field
    mkdir = os.mkdir

    async def observe_receipt(session_id, **fields):
        nonlocal binding
        await update(session_id, **fields)
        receipt = json.loads(fields.get("fork_metadata") or "{}").get("owned_copy_dir")
        if receipt:
            binding = Path(receipt["path"])

    def replace_new_child(path, mode=0o777, *, dir_fd=None):
        mkdir(path, mode, dir_fd=dir_fd)
        if path == "nested" and dir_fd is not None and fork_directory.fd_path(dir_fd).resolve() == binding:
            (binding / "nested").rmdir()
            foreign.rename(binding / "nested")

    monkeypatch.setattr(manager.db, "update_session_field", observe_receipt)
    monkeypatch.setattr(os, "mkdir", replace_new_child)
    branch = await manager.duplicate_session(parent.id, user_id=parent.user_id)
    assert (Path(branch.working_dir) / "nested" / "notes.txt").read_text() == "Source notes"
    assert {item.name for item in foreign.iterdir()} == {"keep.txt"}
    assert foreign.stat().st_mode & 0o777 == 0o755
    assert foreign.stat().st_mtime_ns == 2_000_000_000


async def test_startup_retains_an_interrupted_allocation_before_the_directory_receipt(manager, tmp_path, monkeypatch):
    parent = await _seed_parent(manager, _repo(tmp_path))

    def interrupt_allocation(location, token):
        staging = location.path.parent / f".fork-create-{token}" / "copy"
        staging.mkdir(parents=True)
        (staging / "keep.txt").write_text("Unconfirmed directory")
        raise RuntimeError("Interrupted before installing the copy")

    monkeypatch.setattr(fork_directory.CopyLocation, "create", interrupt_allocation)
    with pytest.raises(RuntimeError, match="Interrupted before installing"):
        await manager.duplicate_session(parent.id)
    pending, = await manager.db.load_incomplete_forks()
    metadata = json.loads(pending["fork_metadata"])
    assert "owned_copy_dir" not in metadata
    path = Path(pending["working_dir"])
    staging = path.parent / f".fork-create-{metadata['copy_creation']}" / "copy"

    recovered = SessionManager()
    try:
        await recovered.initialize(manager.db)
        assert not path.exists()
        assert (staging / "keep.txt").read_text() == "Unconfirmed directory"
        assert await manager.db.load_incomplete_forks() == [pending]
    finally:
        await recovered.close_session_owners()


@pytest.mark.parametrize("binding_survives", [True, False], ids=["same-binding", "changed-binding"])
async def test_complete_backup_recovery_observes_restored_directory_binding(tmp_path, monkeypatch, binding_survives):
    home = tmp_path / "installation"
    config, _ = installation_settings(home)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "claude"))
    for name, value in config.model_dump().items():
        monkeypatch.setattr(settings, name, value)
    database = Database(config.db_path)
    await database.initialize()
    manager = SessionManager()
    try:
        account, _ = await create_account(database)
        agent = await database.get_system_agent(account["id"])
        await manager.initialize(database)
        parent = await manager.create_session(agent["id"], "Source", backend="claude-code")
        Path(parent.working_dir, "work.txt").write_text("Preserve on uncertain recovery")
        parent.claude_session_id = "source-resume"
        await database.update_session_field(parent.id, claude_session_id=parent.claude_session_id)
        original_history = _claude_project_dir(parent.working_dir) / "source-resume.jsonl"
        original_history.parent.mkdir(parents=True)
        original_history.write_text(json.dumps({"sessionId": "source-resume", "cwd": parent.working_dir}) + "\n")

        async def fail_install(*args):
            raise OSError("Stop before final installation")

        monkeypatch.setattr(database, "finish_full_copy_fork", fail_install)
        with pytest.raises(OSError, match="Stop before final installation"):
            await manager.duplicate_session(parent.id, user_id=account["id"])
        pending, = await database.load_incomplete_forks()
        copy = Path(pending["working_dir"])
        native = _claude_project_dir(str(copy)) / f"{pending['resume_id']}.jsonl"
        assert copy.is_dir() and native.is_file()
    finally:
        await manager.close_session_owners()
        await database.close()

    # A cold complete inventory includes the original directory binding in
    # the same-filesystem case. File-by-file restore changes both directory
    # identities even when the path, xattrs and contents all match.
    backup = home.rename(tmp_path / "complete-backup")
    assert (backup / copy.relative_to(home) / "work.txt").is_file()
    assert (backup / native.relative_to(home)).is_file()
    assert (backup / Path(config.db_path).relative_to(home)).is_file()
    users = Path(config.users_root).relative_to(home)
    if binding_survives:
        shutil.copytree(backup, home, symlinks=True, ignore=shutil.ignore_patterns(users.name))
        (backup / users).rename(home / users)
    else:
        shutil.copytree(backup, home, symlinks=True)
    target = Database(config.db_path)
    await target.initialize()
    recovered = SessionManager()
    try:
        await recovered.initialize(target)
        assert await target.get_user(account["id"]) is not None
        assert not native.exists() and original_history.is_file()
        pending_rows = await target.load_incomplete_forks()
        if binding_survives:
            assert not copy.exists() and pending_rows == []
        else:
            assert (copy / "work.txt").read_text() == "Preserve on uncertain recovery"
            assert pending_rows == [pending]
        assert recovered.get_session(pending["id"], account["id"]) is None
    finally:
        await recovered.close_session_owners()
        await target.close()


@pytest.mark.asyncio
async def test_duplicate_copy_failure_removes_partial_dir(manager, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    parent = await _seed_parent(manager, repo)

    captured = {}

    def boom_copy(directory, src):
        (fork_directory.fd_path(directory.target) / "partial").write_text("x")
        captured["dest"] = directory.path
        raise OSError("disk full mid-copy")

    monkeypatch.setattr(fork_directory.CopyDirectory, "copy_from", boom_copy)

    with pytest.raises(ForkError) as ei:
        await manager.duplicate_session(parent.id)
    assert ei.value.reason == "copy_failed"
    # The partial dir and its recovery row are retired; parent freed.
    assert not Path(captured["dest"]).exists()
    rows = await manager.db.load_sessions(include_archived=True)
    assert len(rows) == 1
    assert parent._forking is False


@pytest.mark.asyncio
async def test_duplicate_cleanup_failure_leaves_row_and_dir(manager, tmp_path, monkeypatch):
    # If artifact cleanup ALSO fails after native-copy preparation, leave both the
    # 'initializing' row AND the copied dir for the startup sweep to retry — do
    # NOT strand the row pointing at a deleted working_dir.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    parent = await _seed_parent(manager, repo)

    harness = sm_forking.get_harness(parent.backend)
    cleanup = harness.cleanup_incomplete_fork_artifacts

    async def boom(*a, **k):
        raise RuntimeError("prepare blew up")

    async def cleanup_boom(*a, **k):
        raise RuntimeError("cleanup blew up too")

    monkeypatch.setattr(harness, "prepare_fork_copy", boom)
    monkeypatch.setattr(harness, "cleanup_incomplete_fork_artifacts", cleanup_boom)

    with pytest.raises(RuntimeError):
        await manager.duplicate_session(parent.id)

    rows = {r["id"]: r for r in await manager.db.load_sessions(include_archived=True)}
    fork_rows = [r for r in rows.values() if r["id"] != parent.id]
    assert len(fork_rows) == 1
    fr = fork_rows[0]
    assert fr["fork_status"] == "initializing"
    # The copied dir survives for retry.
    assert Path(fr["working_dir"]).is_dir()
    assert Path(fr["working_dir"]).parent == paths_for(parent.user_id).workspace.parent / "fork"
    recovered = SessionManager()
    await recovered.initialize(manager.db)
    assert [row["id"] for row in await manager.db.load_incomplete_forks()] == [fr["id"]]
    monkeypatch.setattr(harness, "cleanup_incomplete_fork_artifacts", cleanup)
    await recovered._recover_incomplete_forks()
    assert not Path(fr["working_dir"]).exists()
    assert not await manager.db.load_incomplete_forks()


@pytest.mark.asyncio
@pytest.mark.parametrize("ownership", ["path-only", "redirected", "legacy", "missing", "different", "installed"])
async def test_recovery_requires_confirmed_uninstalled_copy_ownership(manager, tmp_path, monkeypatch, caplog, ownership):
    # Historical receipts name a path without proving its directory identity.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    copied = Path(manager._fork_copy_dest(str(tmp_path / "proj"), "deadbeef0001", None))
    copied.mkdir(parents=True)
    (copied / "f.txt").write_text("x")
    parent = await _seed_parent(manager, tmp_path / "proj")

    metadata = {"full_copy": True}
    if ownership in ("path-only", "redirected", "installed"):
        metadata["owned_copy_dir"] = str(copied)
    elif ownership == "different":
        metadata["owned_copy_dir"] = str(copied.parent / "other-copy")
    elif ownership == "missing":
        shutil.rmtree(copied)
    await manager.db.create_fork_session(
        fork_id="deadbeef0001", name="o", working_dir=str(copied),
        created_at="2026-06-15T00:00:00+00:00", parent_id=parent.id,
        backend="claude-code", agent_id=parent.agent_id, credential_id=None,
        resume_id="resume-xyz", fork_after_seq=5,
        fork_metadata=json.dumps(metadata),
    )
    if ownership == "installed":
        await manager.db.update_session_field("deadbeef0001", fork_status="ready")
    if ownership == "redirected":
        displaced = copied.parent.rename(tmp_path / "original-fork-parent")
        other_parent = tmp_path / "foreign-legacy"
        foreign = other_parent / copied.name
        foreign.mkdir(parents=True)
        (foreign / "keep.txt").write_text("Foreign legacy data")
        copied.parent.symlink_to(other_parent, target_is_directory=True)

    harness = sm_forking.get_harness("claude-code")

    async def fake_cleanup(*a, **k):
        return None

    monkeypatch.setattr(harness, "cleanup_incomplete_fork_artifacts", fake_cleanup)
    await manager._recover_incomplete_forks()

    rows = await manager.db.load_sessions(include_archived=True)
    retained = ownership != "missing"
    assert any(r["id"] == "deadbeef0001" for r in rows) is retained
    assert copied.exists() is retained
    if ownership in ("path-only", "legacy", "different"):
        assert "no confirmed directory creation" in caplog.text
    if ownership == "redirected":
        assert (displaced / copied.name / "f.txt").read_text() == "x"
        assert (foreign / "keep.txt").read_text() == "Foreign legacy data"


@pytest.mark.asyncio
async def test_resolve_credential_require_auth_false(manager, tmp_path, monkeypatch):
    # For artifact cleanup/copy we need the Codex home dir even when auth.json is
    # absent/revoked (the rollout still lives there); a secret-backed (Claude)
    # credential has no per-credential store, so it resolves to None (Vera).
    from server.codex_login import codex_home_for

    cid = "cred-xyz"
    owner = (await manager.db.get_system_agent())["user_id"]
    await manager.db.save_credential(cid, "codex", "owned", "oauth", "", "now", user_id=owner)
    home = codex_home_for(cid)
    with pytest.raises(ValueError, match="completed login"):
        await manager.resolve_credential_by_id(cid, backend="codex", user_id=owner)
    cred = await manager.resolve_credential_by_id(
        cid, backend="codex", require_auth=False, user_id=owner,
    )
    assert cred is not None and cred.home_dir == home
    # Secret-backed: no on-disk store keyed by credential → None for cleanup.
    await manager.db.save_credential("secret", "claude-code", "owned", "oauth", "", "now", user_id=owner)
    assert await manager.resolve_credential_by_id(
        "secret", backend="claude-code", require_auth=False, user_id=owner,
    ) is None


@pytest.mark.asyncio
async def test_full_copy_marker_survives_input_history_installation(manager, tmp_path, monkeypatch):
    # `fork_metadata` is cleared when native input history is installed,
    # but the durable `full_copy` identity MUST persist so the UI keeps treating
    # it as a copy-dir fork rather than a rewind (Vera review).
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    parent = await _seed_parent(manager, repo)
    fork = await manager.duplicate_session(parent.id)
    # Sanity: replay flag + full_copy both set right after duplicate.
    assert fork.fork_needs_replay in (True, False)
    await _record_fork_installation(manager.db, fork)

    assert fork.fork_needs_replay is False
    meta = json.loads(fork.fork_metadata)
    assert meta["full_copy"] is True
    assert "prefilled_prompt" not in meta
    # The public flag stays true → UI still renders "full copy".
    fields = sm.fork_info_fields(
        backend=fork.backend,
        forked_from_session_id=fork.forked_from_session_id,
        fork_after_seq=fork.fork_after_seq,
        fork_metadata=fork.fork_metadata,
        fork_revert_record=fork.fork_revert_record,
    )
    assert fields["fork_is_full_copy"] is True
    # And it's durable in the DB, not just in memory.
    rows = {r["id"]: r for r in await manager.db.load_sessions(include_archived=True)}
    persisted = json.loads(rows[fork.id]["fork_metadata"])
    assert persisted["full_copy"] is True


@pytest.mark.asyncio
async def test_rewind_metadata_fully_cleared_on_input_installation(manager, tmp_path, monkeypatch):
    # A /rewind fork has no durable keys → its fork_metadata clears to None
    # (so the composer doesn't re-prefill the rewound message).
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = _repo(tmp_path)
    parent = await _seed_parent(manager, repo, n_user=3)
    fork = await manager.fork_session(parent.id, 2)  # rewind to seq 2
    assert json.loads(fork.fork_metadata).get("prefilled_prompt") is not None
    await _record_fork_installation(manager.db, fork)

    assert fork.fork_metadata is None


async def _record_fork_installation(db, fork):
    # Exercise persistence with an explicitly supplied native-storage observation.
    # The actual protocol proof is tested independently in test_fork_replay_input.
    await db.update_session_field(fork.id, fork_needs_replay=True)
    await db.admit_client_submission(fork.id, "first", "digest", "input",
        payload={"content": "First input", "attachment_ids": [], "order": 1}, receipt={"accepted": True})
    await db.claim_client_submission(fork.id, "input", "input", "generation")
    await db.record_input_history(fork.id, "input", "attempt", "generation", "claude-code", "native", True)
    fork.claude_session_id, fork.fork_needs_replay, fork.fork_metadata = await db.session_input_history(fork.id, "input")


async def test_delivery_ack_preserves_input_metadata_consumed_during_broadcast(manager, tmp_path, monkeypatch):
    parent = await _seed_parent(manager, _repo(tmp_path))

    async def install_during_broadcast(event, **kwargs):
        if event["type"] == "session_forked":
            branch = manager.get_session(event["fork_session_id"], parent.user_id)
            assert branch is not None
            await _record_fork_installation(manager.db, branch)
            assert "owned_copy_dir" not in json.loads(branch.fork_metadata)

    monkeypatch.setattr(manager, "_broadcast", install_during_broadcast)
    branch = await manager.duplicate_session(parent.id, user_id=parent.user_id)
    saved, = [row for row in await manager.db.load_sessions() if row["id"] == branch.id]
    assert not saved["fork_needs_replay"]
    assert json.loads(saved["fork_metadata"]) == json.loads(branch.fork_metadata) == {
        "full_copy": True, "duplicated_from": parent.name,
    }
