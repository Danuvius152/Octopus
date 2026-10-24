"""Retiring global file shells must preserve account writes and cold checks."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from server.harness import HarnessEvent
from server.large_prompts import LARGE_PROMPT_THRESHOLD_BYTES
from server.service_installation import (
    configure_service,
    environment_path,
    profile_path,
    read_service_profile,
)
from tests.fake_run import FakeRunBase
from tests.test_account_file_layout import managed_files as managed_files
from tests.test_service_layout import account_installation as account_installation


async def test_account_files_remain_restartable_after_global_shell_cleanup(managed_files, monkeypatch):
    config, account, profile, session, client, manager = managed_files
    old_roots = [Path(config.attachments_dir), Path(config.large_prompts_dir)]
    for root in old_roots:
        root.mkdir()
        (root / ".octopus-installation").write_text(str(profile_path(config)) + "\n")
    profile_bytes = profile_path(config).read_bytes()
    assert read_service_profile(config) == profile
    original = b"Account bytes survive retirement of the global roots"
    uploaded = await client.post(f"/api/sessions/{session.id}/attachments",
                                 files={"file": ("original.bin", original, "application/octet-stream")})
    assert uploaded.status_code == 201
    identifier = uploaded.json()["id"]
    assert (account / "attachments" / session.id / f"{identifier}__original.bin").read_bytes() == original

    received = []

    class RecordingRun(FakeRunBase):
        async def start(self, prompt, working_dir, resume_id=None, credential=None):
            received.append(prompt)

        def stream(self):
            async def events():
                yield HarnessEvent(type="session_started", session_id="cleanup-account-input")
                yield HarnessEvent(type="result", session_id="cleanup-account-input", cost=0.0, num_turns=1)
            return events()

        async def stop(self):
            pass

        async def interrupt(self):
            pass

    monkeypatch.setattr(manager, "_make_run", lambda *args: RecordingRun())
    prompt = "[bg-task-result] " + "完整账户输入" * (LARGE_PROMPT_THRESHOLD_BYTES // 12 + 1)
    assert len(prompt.encode()) > LARGE_PROMPT_THRESHOLD_BYTES

    async def submit():
        receipt = await manager.start_message(session.id, prompt)
        assert receipt.accepted
        assert (await asyncio.wait_for(receipt.wait(), 5)).outcome.status == "success"

    await submit()
    files = list((account / "large-prompts" / session.id).glob("*.txt"))
    assert len(files) == 1 and files[0].read_text() == prompt
    assert str(files[0]) in received[0]
    environment = {name: value for name, value in os.environ.items() if not name.upper().startswith("OCTOPUS_")}
    environment["OCTOPUS_ENV_FILE"] = str(environment_path(config))
    command = [sys.executable, "scripts/check-storage-layout.py"]
    root = Path(__file__).resolve().parents[1]
    rejected = await asyncio.to_thread(subprocess.run, command, cwd=root, env=environment,
                                      capture_output=True, text=True, timeout=10)
    assert rejected.returncode == 1, rejected.stdout + rejected.stderr
    report = json.loads(rejected.stdout)
    assert "unused_legacy_directories" in report, report
    assert set(report["unused_legacy_directories"]) == {str(path) for path in old_roots}
    cleaned = await asyncio.to_thread(subprocess.run, [*command, "--prune"], cwd=root, env=environment,
                                     capture_output=True, text=True, timeout=10)
    assert cleaned.returncode == 0, cleaned.stdout + cleaned.stderr
    assert set(json.loads(cleaned.stdout)["removed"]) == {str(path) for path in old_roots}
    assert all(not path.exists() for path in old_roots)
    cold = await asyncio.to_thread(subprocess.run, command, cwd=root, env=environment,
                                  capture_output=True, text=True, timeout=10)
    assert cold.returncode == 0, cold.stdout + cold.stderr
    assert json.loads(cold.stdout)["passed"]
    assert read_service_profile(config) == profile
    assert configure_service(config, unit=profile.unit, manager=profile.manager) == profile
    assert profile_path(config).read_bytes() == profile_bytes
    assert all(not path.exists() for path in old_roots)
    downloaded = await client.get(f"/api/sessions/{session.id}/attachments/{identifier}")
    assert downloaded.status_code == 200 and downloaded.content == original
    another = await client.post(f"/api/sessions/{session.id}/attachments",
                                files={"file": ("new.bin", b"New account bytes", "application/octet-stream")})
    assert another.status_code == 201
    await submit()
    assert all(path.read_text() == prompt for path in (account / "large-prompts" / session.id).glob("*.txt"))
    assert len(received) == 2
    assert all(not path.exists() for path in old_roots)
