"""Real end-to-end deep research against a signed-in harness CREDENTIAL.

Unlike the bare-CLI *_real tests (which use host ambient auth), deep research
runs through an Octopus credential. The codex credential is directory-backed
(its own CODEX_HOME with auth.json), so we discover a usable one on disk and
register its metadata in the isolated DB before binding a session to its id.
Authentication stays in that directory; no login secret is copied into the DB.
Gated + skipped when no usable codex credential exists.
Costs real API calls + ~30-60s. Drives the WHOLE stack:
ResearchManager → orchestrator → real codex web_search → cited report → inject.
"""

from __future__ import annotations

import asyncio
import functools
import os
from datetime import UTC, datetime

import pytest

from server.codex_login import codex_home_for
from server.database import Database
from server.research import manager as rm_mod
from server.research.manager import ResearchManager
from server.research.orchestrator import ResearchLimits
from server.research.orchestrator import run_research as _real_run
from server.session_manager import SessionManager
from tests.accounts import create_account
from tests.cli_gate import codex_home_works
from tests.submission import accepted_receipt


@functools.lru_cache(maxsize=1)
def _usable_codex_credential() -> str | None:
    """A signed-in, directory-backed codex credential id whose CODEX_HOME holds
    a WORKING auth.json (probed for real). None if none is usable."""
    base = os.path.dirname(codex_home_for("_probe_"))  # the per-credential root
    if not os.path.isdir(base):
        return None
    for cid in sorted(os.listdir(base)):
        home = os.path.join(base, cid)
        if not os.path.exists(os.path.join(home, "auth.json")):
            continue
        if codex_home_works(home):
            return cid
    return None


pytestmark = [pytest.mark.real, pytest.mark.real_codex]


@pytest.fixture(autouse=True)
def _require_signed_in_codex_credential():
    """This suite needs more than a signed-in CLI: it needs a usable codex
    credential resolvable from the DB. That probe is file-local (it shells
    out), so it cannot be a conftest marker — but it must still not run at
    import time. An autouse fixture runs at setup, which is the same
    deferral the markers get.
    """
    if _usable_codex_credential() is None:
        pytest.skip("no signed-in codex credential available for a real research run")


@pytest.mark.asyncio
async def test_real_codex_deep_research_end_to_end(tmp_path, monkeypatch):
    cred_id = _usable_codex_credential()
    db = Database(":memory:")
    await db.initialize()
    owner, _ = await create_account(db, extra_roots=[tmp_path])
    try:
        await db.save_credential(
            cred_id, "codex", "Configured Codex", "oauth", "",
            datetime.now(UTC).isoformat(), user_id=owner["id"],
        )
        mgr = SessionManager()
        await mgr.initialize(db)
        rm = ResearchManager()
        rm.bind(session_mgr=mgr, db=db)

        injected: list[str] = []

        async def fake_start_message(session_id, prompt, *, source, delivery_id, work_id, knowledge_context,
                                     question_session_id, question_id):
            assert source == "research"
            assert delivery_id.startswith("research:")
            assert work_id is None
            assert question_session_id is question_id is None
            assert knowledge_context.trusted_request == "What is the latest stable version of Python 3? Cite python.org."
            injected.append(prompt)
            return accepted_receipt()

        monkeypatch.setattr(mgr, "start_message", fake_start_message)

        # Shrink limits hard for speed — still REAL web searches.
        async def small_run(question, **kw):
            kw["limits"] = ResearchLimits(
                max_angles=1, max_findings_per_angle=3, max_claims=2,
                votes_per_claim=1, concurrency=2, leaf_timeout=180, reason_timeout=120,
            )
            return await _real_run(question, **kw)

        monkeypatch.setattr(rm_mod, "run_research", small_run)

        agent = await db.get_system_agent()
        # Binding validates the DB metadata; execution resolves CODEX_HOME.
        session = await mgr.create_session(
            agent["id"], "Research", str(tmp_path), backend="codex",
            credential_id=cred_id,
        )
        row = await rm.start(
            session.id,
            "What is the latest stable version of Python 3? Cite python.org.",
        )
        await asyncio.wait_for(rm._tasks[row["id"]], timeout=360)

        final = await db.get_research_job(row["id"])
        assert final["status"] == "completed", f"job did not complete: {final}"
        assert final["injection_status"] == "delivered"
        assert len(injected) == 1
        assert injected[0].startswith(f"[deep-research:{row['id']}]")
        assert final["report_path"], "no report file written"

        # Prove REAL research happened (not a degraded empty run): a substantial,
        # source-citing report read from the FILE (not the question-bearing
        # injection prefix, which would hollow-pass on the echoed question).
        with open(final["report_path"], encoding="utf-8") as fh:
            report = fh.read()
        assert len(report) > 120, f"report too short — degraded run?\n{report}"
        assert "http" in report.lower(), f"report cites no source:\n{report}"
    finally:
        await db.close()
