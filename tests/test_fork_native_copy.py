"""Claude copy mechanics and cleanup of saved pre-native Codex fork artifacts.

Current Codex creation and cleanup use the native history saga, covered by
test_codex_app_server_fork.py and its real product-history counterpart.
"""

import json
from pathlib import Path

import pytest

from server.harness import claude_code as cc
from server.harness import get_harness
from server.harness.events import HarnessCredential

# ----------------------------------------------------------------- Claude

def test_claude_project_slug_matches_cli(monkeypatch, tmp_path):
    # Pin the slug to the REAL CLI behavior (verified by the gated real test):
    # every non-alphanumeric char -> '-', case preserved, runs NOT collapsed.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    base = tmp_path / ".claude" / "projects"
    cases = {
        # underscores AND the trailing -re0 from pytest tmp dirs
        "/tmp/pytest-of-x/pytest-99/test_claude_native_copy_re0/src":
            "-tmp-pytest-of-x-pytest-99-test-claude-native-copy-re0-src",
        # dotfile dir -> double dash, case preserved, no collapse
        "/home/u/.octopus/fork/Octopus-a0434":
            "-home-u--octopus-fork-Octopus-a0434",
    }
    for wd, slug in cases.items():
        assert cc._claude_project_dir(wd) == base / slug


def _write_claude_transcript(base_home, working_dir, sid):
    """Seed a minimal real-shape Claude transcript under <home>/projects/<slug>."""
    d = cc._claude_project_dir(working_dir)
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.jsonl"
    lines = [
        {"type": "operation", "sessionId": sid, "content": "x"},
        {"type": "user", "sessionId": sid, "cwd": working_dir, "message": "hi"},
        {"type": "assistant", "sessionId": sid, "cwd": working_dir, "message": "yo"},
    ]
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return f


@pytest.mark.asyncio
async def test_claude_fork_copy_rewrites_and_resumes(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    parent_wd, dest_wd = "/proj/parent", "/home/u/.octopus/fork/parent-abc"
    _write_claude_transcript(tmp_path, parent_wd, "pid-1")

    art = await cc._fork_copy(
        parent_working_dir=parent_wd, parent_resume_id="pid-1",
        dest_working_dir=dest_wd, new_resume_id="nid-2",
    )
    assert art.resume_id == "nid-2"
    assert art.needs_replay is False

    dest = cc._claude_project_dir(dest_wd) / "nid-2.jsonl"
    assert dest.is_file()
    rows = [json.loads(line) for line in dest.read_text().splitlines() if line]
    # Every sessionId rewritten; every cwd repointed to the copied dir.
    assert {r["sessionId"] for r in rows} == {"nid-2"}
    assert {r["cwd"] for r in rows if "cwd" in r} == {dest_wd}


@pytest.mark.asyncio
async def test_claude_fork_copy_fallback_when_no_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    # No parent resume id → replay fallback.
    art = await cc._fork_copy(
        parent_working_dir="/p", parent_resume_id=None,
        dest_working_dir="/d", new_resume_id="n",
    )
    assert art.resume_id is None and art.needs_replay is True
    # Resume id present but file missing → still fallback.
    art2 = await cc._fork_copy(
        parent_working_dir="/p", parent_resume_id="ghost",
        dest_working_dir="/d", new_resume_id="n",
    )
    assert art2.needs_replay is True


@pytest.mark.asyncio
async def test_claude_fork_cleanup_reraises_on_oserror(tmp_path, monkeypatch):
    # A real removal failure (not "already gone") must RE-RAISE so the saga
    # keeps the row for a retry instead of stranding the transcript (Vera).
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    dest_wd = "/x/y"
    d = cc._claude_project_dir(dest_wd)
    d.mkdir(parents=True, exist_ok=True)
    (d / "nid.jsonl").mkdir()  # a dir → unlink raises IsADirectoryError (OSError)
    with pytest.raises(OSError):
        await cc._fork_cleanup(dest_wd, "nid", "fork1")


@pytest.mark.asyncio
async def test_claude_fork_cleanup_removes_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    dest_wd = "/home/u/.octopus/fork/x-1"
    d = cc._claude_project_dir(dest_wd)
    d.mkdir(parents=True, exist_ok=True)
    (d / "nid.jsonl").write_text("{}\n")
    await cc._fork_cleanup(dest_wd, "nid", "fork1")
    assert not (d / "nid.jsonl").exists()
    # Idempotent + no-op for replay (resume id None).
    await cc._fork_cleanup(dest_wd, "nid", "fork1")
    await cc._fork_cleanup(dest_wd, None, "fork1")


async def test_claude_rejects_native_checkpoint_before_removing_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    transcript = _write_claude_transcript(tmp_path, "/fork", "native-id")
    cleanup = get_harness("claude-code").profile.fork_cleanup
    assert cleanup is not None
    with pytest.raises(ValueError, match="native fork checkpoint"):
        await cleanup("/fork", "native-id", "fork", artifact={
            "transport": "app-server", "phase": "created", "cleanup_allowed": True,
            "home": str(tmp_path), "source_thread_id": "source", "thread_id": "native-id",
        })
    assert transcript.exists()


# ----------------------------------------------------------------- Codex

def _write_codex_rollout(home_dir, rid):
    sess = Path(home_dir) / "sessions" / "2026" / "06" / "15"
    sess.mkdir(parents=True, exist_ok=True)
    f = sess / f"rollout-2026-06-15T00-00-00-{rid}.jsonl"
    lines = [
        {"type": "session_meta", "payload": {"id": rid, "cwd": "/whatever"}},
        {"type": "response_item", "payload": {"text": "hello"}},
    ]
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return f


@pytest.mark.asyncio
async def test_codex_fork_cleanup_reraises_on_oserror(tmp_path, monkeypatch):
    home = tmp_path / "codexhome"
    home.mkdir()
    _write_codex_rollout(home, "rid-1")
    cred = HarnessCredential(backend="codex", auth_type="oauth", home_dir=str(home))
    _write_codex_rollout(home, "rid-2")  # Already saved by the retired strategy.

    def _boom(self, *a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "unlink", _boom)
    with pytest.raises(OSError):
        await get_harness("codex").cleanup_incomplete_fork_artifacts("/y", "rid-2", "fork1", credential=cred)


@pytest.mark.asyncio
async def test_codex_fork_cleanup_removes_copy(tmp_path):
    home = tmp_path / "codexhome"
    home.mkdir()
    original = _write_codex_rollout(home, "rid-1")
    cred = HarnessCredential(backend="codex", auth_type="oauth", home_dir=str(home))
    copied = _write_codex_rollout(home, "rid-2")
    await get_harness("codex").cleanup_incomplete_fork_artifacts("/y", "rid-2", "fork1", credential=cred)
    assert not copied.exists()
    # The ORIGINAL rollout is untouched.
    assert original.exists()


@pytest.mark.asyncio
async def test_codex_fork_cleanup_removes_unreadable_and_legacy_copies(
    tmp_path, monkeypatch
):
    home = tmp_path / "codexhome"
    original = _write_codex_rollout(home, "rid-1")
    cred = HarnessCredential(backend="codex", auth_type="oauth", home_dir=str(home))
    copied = _write_codex_rollout(home, "rid-2")
    legacy = original.parent / "rollout-fork-rid-2.jsonl"
    legacy.write_text('{"type":"session_meta","payload":{"id":"rid-2"}}\n')

    def unreadable(self, *args, **kwargs):
        raise PermissionError("cannot read transcript")

    monkeypatch.setattr(Path, "open", unreadable)
    await get_harness("codex").cleanup_incomplete_fork_artifacts("/y", "rid-2", "fork1", credential=cred)
    assert not copied.exists()
    assert not legacy.exists()
    assert original.exists()
@pytest.mark.parametrize("authenticated", [False, True])
def test_codex_native_copy_auth_uses_the_selected_isolated_home(tmp_path, monkeypatch, authenticated):
    from tests.cli_gate import codex_home_with_auth

    selected = tmp_path / "selected-codex"
    selected.mkdir()
    if authenticated:
        (selected / "auth.json").write_text("{}")
    monkeypatch.setenv("CODEX_HOME", str(selected))
    assert codex_home_with_auth() == (str(selected) if authenticated else None)
