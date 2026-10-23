"""Real systemd boundaries, using disposable user units and isolated storage."""

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

from server.service_installation import configure_service, render_service_unit
from tests.test_service_runtime import installation_settings
from tests.test_service_runtime import provider_homes as provider_homes

pytestmark = [pytest.mark.real, pytest.mark.supervisor]

REPO = Path(__file__).resolve().parents[1]


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


def properties(unit):
    result = systemctl("show", unit, "--property=ActiveState", "--property=Result", "--property=MainPID", "--property=ExecMainStatus", "--property=NRestarts")
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def start_limited(unit):
    state = properties(unit)
    # systemd may preserve the original exit-code Result after refusing the
    # fourth start. Failed (rather than activating/auto-restart), three bounded
    # restarts and the exact number of executed mains prove the contract.
    return state if state["ActiveState"] == "failed" and int(state["NRestarts"]) == 3 else None


@contextmanager
def isolated_unit(tmp_path, *, program=None, precheck=None, configuration=None, inherited_environment=None):
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
        journal = subprocess.run(
            ["journalctl", "--user", "-u", unit, "--no-pager", "-n", "60"],
            capture_output=True, text=True, timeout=10,
        )
        print(journal.stdout)
        systemctl("stop", unit, check=False)
        systemctl("reset-failed", unit, check=False)
        path.unlink(missing_ok=True)
        systemctl("daemon-reload")


def request(config, path, body=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{config.port}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {config.auth_token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=1) as response:
        return json.load(response)


def ready(config):
    try:
        return request(config, "/ready").get("status") == "ready"
    except (OSError, urllib.error.URLError):
        return False


def test_generated_service_preserves_shell_configuration_without_a_project_env(tmp_path, monkeypatch):
    from server.service_installation import environment_path

    token = "review-shell-only-'quoted\"-dollar$-slash\\-20261020"
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", token)
    with isolated_unit(tmp_path) as (unit, config, project):
        wait_for(lambda: ready(config))
        assert not (project / ".env").exists()
        assert config.auth_token == token
        assert request(config, "/api/agents")
        assert environment_path(config).stat().st_mode & 0o777 == 0o600
        bad = urllib.request.Request(f"http://127.0.0.1:{config.port}/api/agents",
                                     headers={"Authorization": "Bearer changeme"})
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(bad, timeout=1)
        assert error.value.code == 401


def test_a_second_real_unit_cannot_enter_the_owned_installation(tmp_path):
    with isolated_unit(tmp_path) as (unit, config, _):
        wait_for(lambda: ready(config))
        original_pid = properties(unit)["MainPID"]
        directory = Path(os.environ["XDG_RUNTIME_DIR"]) / "systemd/user"
        contender = "octopus-check-" + uuid.uuid4().hex + ".service"
        path = directory / contender
        # Keep the same generated startup and storage configuration. Repeated
        # restart limiting has its own test; one rejected start is enough here.
        path.write_text((directory / unit).read_text().replace("Restart=on-failure", "Restart=no"))
        try:
            systemctl("daemon-reload")
            systemctl("start", contender, check=False)
            rejected = wait_for(lambda: properties(contender) if properties(contender)["ActiveState"] == "failed" else None)
            assert int(rejected["ExecMainStatus"]) != 0
            diagnostic = subprocess.run(
                ["journalctl", "--user", "-u", contender, "--no-pager"],
                check=True, capture_output=True, text=True, timeout=10,
            ).stdout
            assert "Installation is already owned by another service process" in diagnostic
            assert properties(unit)["MainPID"] == original_pid
            assert ready(config)
            assert request(config, "/api/agents")
        finally:
            systemctl("stop", contender, check=False)
            systemctl("reset-failed", contender, check=False)
            path.unlink(missing_ok=True)
            systemctl("daemon-reload")


def test_optional_configuration_can_be_cleared_without_republishing_the_unit(tmp_path):
    program = (
        "import json,time\nfrom pathlib import Path\nfrom server.config import settings\n"
        "Path('configuration.json').write_text(json.dumps(settings.public_base_url))\n"
        "time.sleep(60)\n"
    )
    with isolated_unit(
        tmp_path, program=program, configuration={"public_base_url": "https://original.example"},
        inherited_environment={"OCTOPUS_PUBLIC_BASE_URL": "https://inherited.example"},
    ) as (unit, config, project):
        output = project / "configuration.json"
        wait_for(lambda: output.exists() and output.read_text() == '"https://original.example"')
        config.public_base_url = None
        configure_service(config, unit=unit, manager="user")
        # Simulate interruption after configuration publication: keep the
        # loaded unit unchanged, including its inherited environment.
        systemctl("restart", unit)
        wait_for(lambda: output.read_text() == "null")


def test_systemd_retires_detached_forked_writers_before_the_next_server(tmp_path):
    with isolated_unit(tmp_path) as (unit, config, project):
        wait_for(lambda: ready(config))
        previous = properties(unit)["MainPID"]
        agents = request(config, "/api/agents")
        session = request(config, "/api/sessions", {
            "name": "Supervisor probe", "agent_id": agents[0]["id"],
            "working_dir": config.default_working_dir,
        })
        writer = project / "writer.py"
        writer.write_text(
            "import os,signal,time\nfrom pathlib import Path\n"
            "if os.fork():\n time.sleep(60)\n raise SystemExit(0)\n"
            "os.setsid()\nos.fork()\nsignal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
            f"root=Path({str(project)!r})\n"
            "(root / ('writer-'+str(os.getpid()))).write_text('ready')\n"
            "while True:\n"
            " with (root / 'effects').open('a') as out: out.write(str(os.getpid())+'\\n')\n"
            " time.sleep(.03)\n"
        )
        import shlex

        task = request(config, f"/api/sessions/{session['id']}/bg-tasks", {
            "command": shlex.join([sys.executable, str(writer)]), "description": "isolated writer probe",
        })
        wait_for(lambda: len(list(project.glob("writer-*"))) == 2)
        started = time.monotonic()
        systemctl("kill", "--kill-whom=main", "--signal=SIGKILL", unit)
        wait_for(lambda: properties(unit)["MainPID"] not in {"0", previous} and ready(config))
        elapsed = time.monotonic() - started
        effects = (project / "effects").read_bytes()
        time.sleep(.2)
        assert (project / "effects").read_bytes() == effects
        result = request(config, f"/api/sessions/{session['id']}/bg-tasks/{task['id']}")
        assert result["status"] == "interrupted"
        print(f"Detached forked writers stopped and successor ready in {elapsed:.3f}s")


def submit_message(config, session_id, content, client_id):
    from websockets.sync.client import connect

    with connect(f"ws://127.0.0.1:{config.port}/ws?token={config.auth_token}", open_timeout=3) as connection:
        connection.send(json.dumps({"type": "send_message", "session_id": session_id,
                                   "content": content, "client_request_id": client_id}))
        while True:
            event = json.loads(connection.recv(timeout=5))
            if event.get("type") == "submission" and event.get("client_request_id") == client_id:
                return event


def claim_window_program(window):
    return '''
import asyncio,json,os,sqlite3,sys
from dataclasses import replace
from pathlib import Path
from server.config import settings
from server.harness import get_harness
from server.harness.profile import StdinMode
from server.main import app
import uvicorn

root=Path.cwd()
model=root/'model.py'
model.write_text("import json,os,sys\\nfrom pathlib import Path\\n"
                 "Path('spawned').write_text(str(os.getpid()))\\n"
                 "message=json.loads(sys.stdin.readline())\\n"
                 "with Path('executed').open('a') as out: out.write(message['message']['content']+'\\\\n')\\n"
                 "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}),flush=True)\\n"
                 "sys.stdin.read()\\n")
harness=get_harness('codex')
harness.profile=replace(harness.profile,binary=sys.executable,prepare_resume=None,activity_observer=None,
    stdin_mode=StdinMode.STREAM_JSON,
    build_turn_argv=lambda context: ([sys.executable,'-u',str(model)],{'cwd':str(root)}))
spawn=asyncio.create_subprocess_exec
async def interrupted_spawn(*args,**kwargs):
    if str(model) not in args or (root/'loss.json').exists():
        return await spawn(*args,**kwargs)
    if 'WINDOW' in {'before_registration','late_spawn'}:
        process=await spawn(*args,**kwargs)
        async with asyncio.timeout(3):
            while not (root/'spawned').exists(): await asyncio.sleep(.01)
    with sqlite3.connect(settings.db_path) as db:
        row=db.execute("SELECT session_id,request_id,execution_state,process_group,execution_generation "
                       "FROM client_submissions WHERE client_id='original'").fetchone()
    assert row is not None and row[2]=='running' and row[3] is None, row
    (root/'loss.json').write_text(json.dumps(row))
    if 'WINDOW'=='late_spawn':
        while not (root/'release').exists(): await asyncio.sleep(.01)
        return process
    os._exit(73)
asyncio.create_subprocess_exec=interrupted_spawn
uvicorn.run(app,host=settings.host,port=settings.port,log_level='info')
'''.replace("WINDOW", window)


@pytest.mark.parametrize("window", ["before_spawn", "before_registration"])
def test_managed_restart_retires_claim_without_a_recorded_process_group(tmp_path, window):
    from websockets.exceptions import ConnectionClosed

    with isolated_unit(tmp_path, program=claim_window_program(window)) as (unit, config, project):
        wait_for(lambda: ready(config))
        previous = properties(unit)["MainPID"]
        session = request(config, "/api/sessions", {
            "name": "Claim before registration", "agent_id": request(config, "/api/agents")[0]["id"],
            "working_dir": config.default_working_dir, "backend": "codex",
        })
        # The transport may lose either the response or just its completion.
        try:
            submit_message(config, session["id"], "original effects", "original")
        except ConnectionClosed:
            pass
        loss = project / "loss.json"
        wait_for(lambda: loss.exists())
        evidence = json.loads(loss.read_text())
        assert evidence[0] == session["id"] and evidence[2:4] == ["running", None]
        assert (project / "spawned").exists() is (window == "before_registration")
        old_pid = int((project / "spawned").read_text()) if window == "before_registration" else None
        wait_for(lambda: properties(unit)["MainPID"] not in {"0", previous} and ready(config))
        if old_pid is not None:
            assert not Path(f"/proc/{old_pid}").exists(), "The old unit's writer survived recovery"
        status = request(config, f"/api/sessions/{session['id']}/submissions/original")
        assert status["request_id"] == evidence[1]
        assert status["execution"]["state"] == "unknown"
        duplicate = submit_message(config, session["id"], "original effects", "original")
        assert duplicate["request_id"] == evidence[1] and duplicate["execution"]["state"] == "unknown"
        successor = submit_message(config, session["id"], "new effects", "successor")
        assert successor["accepted"] is True, successor
        wait_for(lambda: request(config, f"/api/sessions/{session['id']}/submissions/successor").get("outcome"))
        assert (project / "executed").read_text().splitlines() == ["new effects"]
        assert request(config, f"/api/sessions/{session['id']}/submissions/original")["execution"]["state"] == "unknown"


def test_managed_startup_proof_does_not_release_a_current_generations_late_spawn(tmp_path):
    from websockets.sync.client import connect

    with isolated_unit(tmp_path, program=claim_window_program("late_spawn")) as (unit, config, project):
        wait_for(lambda: ready(config))
        previous = properties(unit)["MainPID"]
        session = request(config, "/api/sessions", {
            "name": "Current generation retirement", "agent_id": request(config, "/api/agents")[0]["id"],
            "working_dir": config.default_working_dir, "backend": "codex",
        })
        original = submit_message(config, session["id"], "original effects", "original")
        wait_for(lambda: (project / "loss.json").exists())
        pid = int((project / "spawned").read_text())
        current = request(config, f"/api/sessions/{session['id']}")
        dispatch = current["dispatch_state"]
        with connect(f"ws://127.0.0.1:{config.port}/ws?token={config.auth_token}") as connection:
            connection.send(json.dumps({"type": "interrupt", "session_id": session["id"],
                                        "client_command_id": "stop-late-spawn", "request_id": dispatch["active_request_id"],
                                        "attempt_id": (current.get("active_attempt") or {}).get("attempt_id"),
                                        "generation": dispatch["generation"], "revision": dispatch["revision"]}))
            while True:
                event = json.loads(connection.recv(timeout=3))
                if event.get("type") == "dispatch_state" and event["dispatch_state"]["paused"]:
                    break
        successor = submit_message(config, session["id"], "new effects", "successor")
        assert successor["accepted"] is True
        snapshot = request(config, f"/api/sessions/{session['id']}")["dispatch_state"]
        assert snapshot["active_request_id"] == original["request_id"]
        assert [item["request_id"] for item in snapshot["items"]] == [successor["request_id"]]
        assert not (project / "executed").exists(), "Successor bypassed the original spawn's retirement"
        assert Path(f"/proc/{pid}").exists(), "The delayed original spawn was not exercised"
        (project / "release").touch()
        wait_for(lambda: request(config, f"/api/sessions/{session['id']}/submissions/successor").get("outcome"))
        assert not Path(f"/proc/{pid}").exists()
        assert (project / "executed").read_text().splitlines() == ["new effects"]
        assert request(config, f"/api/sessions/{session['id']}/submissions/original")["outcome"]["status"] == "interrupted"
        assert properties(unit)["MainPID"] == previous, "Same-generation retirement required a restart"


def test_fatal_restarts_are_bounded_without_entering_ordinary_cleanup(tmp_path):
    program = (
        "import asyncio\nfrom pathlib import Path\n"
        "from server.service_health import ServiceHealth\n"
        "async def main():\n"
        " with Path('starts').open('a') as out: out.write('started\\n')\n"
        " try: ServiceHealth().fail(OSError('unconfirmed authoritative commit'))\n"
        " finally:\n"
        "  Path('cleanup').touch()\n"
        "  await asyncio.Event().wait()\n"
        "asyncio.run(main())\n"
    )
    with isolated_unit(tmp_path, program=program) as (unit, _, project):
        final = wait_for(lambda: start_limited(unit))
        assert final["ActiveState"] == "failed"
        assert final["ExecMainStatus"] == "70"
        assert (project / "starts").read_text().splitlines() == ["started"] * 3
        assert not (project / "cleanup").exists()


def test_prestart_refuses_other_live_tasks_and_never_dispatches_main(tmp_path):
    precheck = (
        "import subprocess,sys\nfrom pathlib import Path\n"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
        f"result=subprocess.run([sys.executable,'-I','-S',{str(REPO / 'server/service_scope.py')!r},'--unit=UNIT_NAME'],capture_output=True,text=True)\n"
        "Path('rejected').write_text(result.stderr)\n"
        "raise SystemExit(result.returncode)\n"
    )
    with isolated_unit(tmp_path, program="from pathlib import Path; Path('dispatched').touch()", precheck=precheck) as (unit, _, project):
        wait_for(lambda: start_limited(unit))
        assert "other tasks" in (project / "rejected").read_text()
        assert not (project / "dispatched").exists()
