"""Shared helpers for testing the actual service process and supervisor."""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

from server.service_installation import configure_service, render_service_unit
from tests.installation_settings import installation_settings

REPO = Path(__file__).resolve().parents[1]


def is_service_launch_option(name):
    normalized = name.upper()
    return normalized.startswith(("OCTOPUS_", "UVICORN_")) or normalized == "WEB_CONCURRENCY"


@contextmanager
def isolated_launch_environment():
    environment = {name: value for name, value in os.environ.items() if not is_service_launch_option(name)}
    with patch.dict(os.environ, environment, clear=True):
        yield environment


def systemctl(*args, check=True):
    return subprocess.run(["systemctl", "--user", *args], check=check, capture_output=True, text=True, timeout=20)


def wait_for(observe, *, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = observe()
        if value:
            return value
        time.sleep(.05)
    raise AssertionError("Supervisor observation deadline expired")


def _confirm_unit_retired(unit):
    result = systemctl("show", unit, "--property=ActiveState,MainPID,Job,TasksCurrent")
    state = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if (
        state.get("ActiveState") not in {"inactive", "failed"}
        or state.get("MainPID") != "0"
        or state.get("Job") != ""
        or state.get("TasksCurrent") not in {"0", "[not set]"}
    ):
        raise AssertionError(f"Retirement of {unit} is unconfirmed: {state}")


def _print_journal(unit):
    journal = subprocess.run(
        ["journalctl", "--user", "-u", unit, "--no-pager", "-n", "60"],
        capture_output=True, text=True, timeout=10,
    )
    print(journal.stdout)


@contextmanager
def isolated_unit(
    tmp_path, *, program=None, precheck=None, configuration=None, inherited_environment=None, unset_environment=(),
):
    project = tmp_path / "instance"
    project.mkdir()
    (project / "server").symlink_to(REPO / "server", target_is_directory=True)
    config, _ = installation_settings(project)
    for name, value in (configuration or {}).items():
        setattr(config, name, value)
    Path(config.default_working_dir).mkdir()
    with socket.socket() as port:
        port.bind(("127.0.0.1", 0))
        config.port = port.getsockname()[1]
    unit = "octopus-check-" + uuid.uuid4().hex + ".service"
    configure_service(config, unit=unit, manager="user")
    text = render_service_unit(project, config, manager="user")
    if inherited_environment:
        text = text.replace("[Service]\n", "[Service]\n" + "".join(
            "Environment=" + json.dumps(name + "=" + value) + "\n"
            for name, value in inherited_environment.items()
        ))
    if unset_environment:
        text = text.replace("[Service]\n", "[Service]\nUnsetEnvironment=" + " ".join(unset_environment) + "\n")
    if program is not None:
        script = project / "main.py"
        script.write_text(program)
        text = "\n".join(
            "ExecStart=" + json.dumps(sys.executable) + " " + json.dumps(str(script))
            if line.startswith("ExecStart=") else line for line in text.splitlines()
        ) + "\n"
    if precheck is not None:
        script = project / "precheck.py"
        script.write_text(precheck.replace("UNIT_NAME", unit))
        text = "\n".join(
            "ExecStartPre=" + json.dumps(sys.executable) + " " + json.dumps(str(script))
            if line.startswith("ExecStartPre=") else line for line in text.splitlines()
        ) + "\n"
    # Runtime user units are scoped to this login and removed in finally.
    directory = Path(os.environ["XDG_RUNTIME_DIR"]) / "systemd/user"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / unit
    path.write_text(text)
    try:
        systemctl("daemon-reload")
        systemctl("start", unit, check=False)
        yield unit, config, project
    finally:
        # Attempt each cleanup step and collect diagnostics even if stop fails.
        with ExitStack() as cleanup:
            cleanup.callback(_print_journal, unit)
            cleanup.callback(systemctl, "daemon-reload")
            cleanup.callback(path.unlink, missing_ok=True)
            cleanup.callback(systemctl, "reset-failed", unit, check=False)
            cleanup.callback(_confirm_unit_retired, unit)
            systemctl("stop", unit)


def request(config, path, body=None, *, bearer=None):
    headers = {"Content-Type": "application/json"}
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(
        f"http://127.0.0.1:{config.port}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers,
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=1) as response:
        return json.load(response)


def setup_account(config):
    return request(config, "/api/auth/setup", {
        "username": "supervisor", "password": "owned-supervisor-password",
    }, bearer=config.auth_token)["token"]


def ready(config):
    try:
        return request(config, "/ready").get("status") == "ready"
    except (OSError, urllib.error.URLError):
        return False
