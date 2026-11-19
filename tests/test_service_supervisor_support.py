"""Failure handling for disposable service units, without a live supervisor."""

import subprocess
from pathlib import Path

import pytest

from tests.installation_settings import provider_homes as provider_homes
from tests.service_supervisor_support import isolated_launch_environment, isolated_unit


@pytest.mark.parametrize(
    ("problem", "error"),
    [
        ("stop timeout", subprocess.TimeoutExpired),
        ("stop rejected", subprocess.CalledProcessError),
        ("still active", AssertionError),
        ("reset timeout", subprocess.TimeoutExpired),
    ],
)
def test_disposable_unit_attempts_all_cleanup_and_reports_failure(tmp_path, monkeypatch, problem, error):
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    operations = []

    def run(argv, *, check=False, **_options):
        action = argv[2] if argv[0] == "systemctl" else "journal"
        operations.append(action)
        if (action, problem) in {("stop", "stop timeout"), ("reset-failed", "reset timeout")}:
            raise subprocess.TimeoutExpired(argv, 20)
        returncode = int(action == "stop" and problem == "stop rejected")
        stdout = ""
        if action == "show":
            if problem == "still active":
                stdout = "ActiveState=active\nMainPID=1234\nJob=\nTasksCurrent=1\n"
            else:
                stdout = "ActiveState=inactive\nMainPID=0\nJob=\nTasksCurrent=[not set]\n"
        result = subprocess.CompletedProcess(argv, returncode, stdout=stdout)
        if check:
            result.check_returncode()
        return result

    monkeypatch.setattr(subprocess, "run", run)
    with isolated_launch_environment(), pytest.raises(error):
        with isolated_unit(tmp_path) as (unit, _, _):
            unit_path = Path(runtime, "systemd", "user", unit)
            assert unit_path.is_file()

    assert operations == ["daemon-reload", "start", "stop", "show", "reset-failed", "daemon-reload", "journal"]
    assert not unit_path.exists()
