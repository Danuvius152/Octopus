"""Claude real-CLI proof for full-copy forks (session-fork.md).

Codex's native fork proof uses the product saga in test_codex_app_server_real.py.

Each asserts the END-TO-END truth the unit tests can't: that the real CLI
actually RESUMES a transcript the harness copied to a new id/location and recalls
context from it. This is the exact check that de-risked the design (the manual
probe).

These call the real backend, so they're inherently subject to the environment:
the login can be expired/revoked and the provider can rate-limit/overload —
NONE of which is a product bug. So a CLI call that fails for an environmental
reason SKIPS the test (the cli_gate.py philosophy); only a usable CLI that
fails to recall the copied context FAILS. Auto-skips entirely unless the backend
CLI is installed and a signed-in home is found.
"""

import json
import subprocess
import uuid

import pytest

from server.harness import claude_code as cc
from server.harness.run import _which_with_fallback

pytestmark = pytest.mark.real

# Substrings that mark an ENVIRONMENTAL CLI failure (auth/quota/provider), not a
# fork-logic bug — when a non-zero call shows one, we skip instead of fail.
_ENV_FAIL_MARKERS = (
    "401", "unauthorized", "revoked", "token_invalidated", "invalidated",
    "sign in again", "log out", "session has ended",
    "rate limit", "rate limited", "temporarily limiting requests",
    "overloaded", "529", "500 internal", "503", "quota",
)


def _skip_if_env_failure(proc: subprocess.CompletedProcess, what: str) -> None:
    """Skip (not fail) when a non-zero CLI call looks environmental — a dead
    login or a provider throttle, not a fork-copy defect."""
    if proc.returncode == 0:
        return
    blob = ((proc.stdout or "") + "\n" + (proc.stderr or "")).lower()
    if any(m in blob for m in _ENV_FAIL_MARKERS):
        pytest.skip(f"{what}: environmental CLI failure (auth/rate-limit)")


@pytest.mark.claude_bin
@pytest.mark.asyncio
async def test_claude_native_copy_resume_recalls_context(tmp_path):
    exe = _which_with_fallback("claude")
    src = tmp_path / "src"
    src.mkdir()
    dst = tmp_path / "dst"
    dst.mkdir()

    r = subprocess.run(
        [exe, "--print", "--output-format=json", "--dangerously-skip-permissions",
         "Remember this exactly: the codeword is ZEBRA77. Acknowledge in 3 words."],
        cwd=str(src), capture_output=True, text=True, timeout=120,
    )
    _skip_if_env_failure(r, "claude create")
    assert r.returncode == 0, r.stderr
    sid = json.loads(r.stdout)["session_id"]

    nid = str(uuid.uuid4())
    art = await cc._fork_copy(
        parent_working_dir=str(src), parent_resume_id=sid,
        dest_working_dir=str(dst), new_resume_id=nid,
    )
    assert art.needs_replay is False and art.resume_id == nid

    try:
        r2 = subprocess.run(
            [exe, "--print", "--output-format=json", "--dangerously-skip-permissions",
             "--resume", nid,
             "What is the codeword I told you earlier? Reply with ONLY the word."],
            cwd=str(dst), capture_output=True, text=True, timeout=120,
        )
        _skip_if_env_failure(r2, "claude resume")
        assert r2.returncode == 0, r2.stderr
        # The real assertion: the COPIED transcript carried the context.
        assert "ZEBRA77" in json.loads(r2.stdout).get("result", "")
    finally:
        import shutil
        for wd in (str(src), str(dst)):
            shutil.rmtree(cc._claude_project_dir(wd), ignore_errors=True)
