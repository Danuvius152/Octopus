"""Process-level service ownership and failure boundaries."""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def provider_homes(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "default-codex-home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "default-claude-home"))


@pytest.mark.parametrize("origin", ["task", "thread"])
def test_storage_failure_exits_the_process_without_waiting_for_cleanup(origin):
    program = """
import asyncio
import sys
import threading
import time
from server.service_health import ServiceHealth

async def main():
    health = ServiceHealth()
    try:
        if sys.argv[1] == "thread":
            threading.Thread(target=lambda: health.fail(OSError("storage outcome unknown"))).start()
            time.sleep(60)
        else:
            async def failed_writer():
                health.fail(OSError("storage outcome unknown"))
            asyncio.create_task(failed_writer())
            await asyncio.Event().wait()
    finally:
        print("ordinary cleanup entered", flush=True)
        await asyncio.Event().wait()

asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", program, origin],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 70, result.stderr
    assert "ordinary cleanup entered" not in result.stdout


@pytest.mark.parametrize("failed_sync", ["file", "directory", "ancestor"])
def test_startup_cannot_admit_work_with_an_unconfirmed_master_key(tmp_path, failed_sync):
    config, config_file = installation_settings(tmp_path)
    if failed_sync == "ancestor":
        config.master_key_file = str(tmp_path / "created" / "nested" / "master.key")
        values = json.loads(config_file.read_text())
        values["master_key_file"] = config.master_key_file
        config_file.write_text(json.dumps(values))
    program = """
import asyncio,json,os,stat,sys
from pathlib import Path
from server.config import settings
for name,value in json.loads(Path(sys.argv[1]).read_text()).items(): setattr(settings,name,value)
settings.master_key=''
key=Path(settings.master_key_file)
fsync=os.fsync
def interrupted_sync(fd):
    name=Path(os.readlink('/proc/self/fd/'+str(fd)))
    directory=stat.S_ISDIR(os.fstat(fd).st_mode)
    is_key=(name==key or name.name.startswith('.master-key-'))
    if ((sys.argv[2]=='file' and is_key)
        or (sys.argv[2]=='directory' and directory and name==key.parent)
        or (sys.argv[2]=='ancestor' and directory and name==key.parent.parent)):
        raise OSError('master key durability unconfirmed')
    return fsync(fd)
os.fsync=interrupted_sync
from server.main import app
async def main():
    async with app.router.lifespan_context(app):
        Path(sys.argv[3]).touch()
asyncio.run(main())
"""
    admitted = tmp_path / "admitted"
    previous = None
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-c", program, str(config_file), failed_sync, str(admitted)],
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 70, result.stderr
        assert not admitted.exists()
        key = Path(config.master_key_file)
        if key.exists():
            current = key.read_bytes()
            assert previous is None or previous == current
            previous = current
    recovered = subprocess.run(
        [sys.executable, "-c", program, str(config_file), "available", str(admitted)],
        capture_output=True, text=True, timeout=10,
    )
    assert recovered.returncode == 0, recovered.stderr
    assert admitted.exists()
    if previous is not None:
        assert Path(config.master_key_file).read_bytes() == previous


@pytest.mark.parametrize("journal", ["available", "full"])
def test_fatal_diagnostic_is_redacted_and_a_full_journal_cannot_delay_exit(journal):
    with tempfile.TemporaryDirectory(prefix="octo-fatal-") as scratch:
        path = str(Path(scratch) / "journal")
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
            receiver.bind(path)
            receiver.settimeout(.5)
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_NONBLOCK) as filler:
                if journal == "full":
                    for _ in range(10000):
                        try:
                            filler.sendto(b"fill", path)
                        except BlockingIOError:
                            break
                    else:
                        pytest.fail("the journal socket was not held full")
                program = (
                    "import sys\nfrom server import service_health as health\n"
                    "health._JOURNAL_SOCKET=sys.argv[1]\n"
                    "health.ServiceHealth().fail(OSError('never-log-this-secret'))\n"
                )
                result = subprocess.run([sys.executable, "-c", program, path], capture_output=True, timeout=5)
                assert result.returncode == 70
                if journal == "available":
                    record = receiver.recv(4096)
                    assert b"OCTOPUS_CAUSE=OSError" in record
                    assert b"exit=70" in record
                    assert b"never-log-this-secret" not in record


def installation_settings(tmp_path):
    from server.config import Settings

    tmp_path.mkdir(parents=True, exist_ok=True)
    paths = {
        "db_path": str(tmp_path / "main.db"),
        "metrics_db_path": str(tmp_path / "metrics.db"),
        "master_key_file": str(tmp_path / "master.key"),
        "users_root": str(tmp_path / "users"),
        "attachments_dir": str(tmp_path / "attachments"),
        "large_prompts_dir": str(tmp_path / "prompts"),
        "codex_home_dir": str(tmp_path / "codex"),
        "research_dir": str(tmp_path / "research"),
        "wiki_dir": str(tmp_path / "wiki"),
        "agents_dir": str(tmp_path / "agents"),
        "applications_dir": str(tmp_path / "apps"),
        "default_working_dir": str(tmp_path / "workspace"),
        "host": "127.0.0.1", "port": 43211, "enable_tunnel": False, "debug": False,
    }
    config_file = tmp_path / "settings.json"
    config_file.write_text(json.dumps(paths))
    return Settings(_env_file=None, **paths), config_file


def test_another_process_cannot_own_the_same_installation(tmp_path):
    from server.service_installation import InstallationOwner

    config, config_file = installation_settings(tmp_path)
    owner = InstallationOwner(config)
    owner.acquire()
    program = """
import json
import sys
from pathlib import Path
from server.config import Settings
from server.service_installation import InstallationError, InstallationOwner

config = Settings(_env_file=None, **json.loads(Path(sys.argv[1]).read_text()))
try:
    owner = InstallationOwner(config)
    owner.acquire()
except InstallationError as error:
    print(str(error), file=sys.stderr)
    raise SystemExit(78)
owner.close()
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", program, str(config_file)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=5,
        )
        assert result.returncode == 78 and "already owned" in result.stderr, result.stderr
        assert not Path(config.db_path).exists()
    finally:
        owner.close()
    successor = InstallationOwner(config)
    successor.acquire()
    successor.close()
    assert not Path(config.db_path).exists()


def test_managed_resources_cannot_be_reopened_as_standalone(tmp_path):
    from server.service_installation import (
        InstallationError,
        InstallationOwner,
        configure_service,
    )

    config, _ = installation_settings(tmp_path)
    configure_service(config, unit="octopus-managed-test.service", manager="user")
    with pytest.raises(InstallationError, match="managed-test.service"):
        InstallationOwner(config).acquire()
    # Merely changing the DB still shares Wiki, attachments and provider state.
    changed_db = config.model_copy(update={"db_path": str(tmp_path / "other.db")})
    with pytest.raises(InstallationError, match="managed installation"):
        InstallationOwner(changed_db).acquire()
    assert not Path(config.db_path).exists()
    assert not Path(changed_db.db_path).exists()


def test_installation_lock_does_not_live_on_in_an_executed_child(tmp_path):
    from server.service_installation import InstallationOwner

    config, _ = installation_settings(tmp_path)
    owner = InstallationOwner(config)
    owner.acquire()
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.read()"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, close_fds=False,
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        owner.close()
        successor = InstallationOwner(config)
        successor.acquire()
        successor.close()
        assert child.poll() is None
    finally:
        owner.close()
        child.communicate(timeout=5)


def test_ephemeral_metrics_do_not_require_a_persistent_installation_marker(tmp_path):
    from server.service_installation import InstallationOwner

    config, _ = installation_settings(tmp_path)
    owner = InstallationOwner(config.model_copy(update={"metrics_db_path": ":memory:"}))
    owner.acquire()
    owner.close()


@pytest.mark.parametrize("override", ["reload", "workers"])
def test_managed_profile_refuses_development_process_modes(tmp_path, override, monkeypatch):
    from server.service_installation import (
        InstallationError,
        configure_service,
        read_service_profile,
    )

    config, _ = installation_settings(tmp_path)
    configure_service(config, unit="octopus-managed-test.service", manager="user")
    if override == "reload":
        config = config.model_copy(update={"debug": True})
    else:
        monkeypatch.setenv("WEB_CONCURRENCY", "2")
    with pytest.raises(InstallationError, match="reload|workers"):
        read_service_profile(config)


def test_cli_rejects_service_process_without_a_managed_profile(tmp_path, monkeypatch, capsys):
    from server import cli, config

    isolated, _ = installation_settings(tmp_path)
    monkeypatch.setattr(config, "settings", isolated)
    args = cli.build_parser().parse_args(["serve", "--service-process"])
    with pytest.raises(SystemExit) as error:
        cli.do_serve(args)
    assert error.value.code == 78
    assert "managed" in capsys.readouterr().err.lower()
    assert not Path(isolated.db_path).exists()


def test_isolated_settings_cannot_reuse_the_managed_default_provider_home(tmp_path):
    from server.service_installation import (
        InstallationError,
        configure_service,
        read_service_profile,
    )

    managed, _ = installation_settings(tmp_path / "managed")
    isolated, _ = installation_settings(tmp_path / "development")
    configure_service(managed, unit="octopus-managed-test.service", manager="user")
    with pytest.raises(InstallationError, match="managed installation"):
        read_service_profile(isolated)


def test_isolated_settings_cannot_share_the_managed_working_directory(tmp_path, monkeypatch):
    from server.service_installation import (
        InstallationError,
        configure_service,
        read_service_profile,
    )

    managed, _ = installation_settings(tmp_path / "managed")
    isolated, _ = installation_settings(tmp_path / "development")
    isolated.default_working_dir = managed.default_working_dir
    configure_service(managed, unit="octopus-managed-test.service", manager="user")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "development-provider-home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "development-claude-home"))
    with pytest.raises(InstallationError, match="managed installation"):
        read_service_profile(isolated)


def test_setup_cannot_replace_the_active_token_with_stale_inputs(tmp_path):
    from dotenv import dotenv_values

    from server.service_installation import (
        InstallationError,
        configure_service,
        environment_path,
    )

    config, _ = installation_settings(tmp_path)
    config.auth_token = "current-service-token"
    configure_service(config, unit="octopus-managed-test.service", manager="user")
    config.auth_token = "obsolete-setup-token"
    with pytest.raises(InstallationError, match="token differs"):
        configure_service(config, unit="octopus-managed-test.service", manager="user")
    assert dotenv_values(environment_path(config), interpolate=False)["OCTOPUS_AUTH_TOKEN"] == "current-service-token"


def test_cleared_optional_configuration_is_owned_by_the_active_file(tmp_path):
    from dotenv import dotenv_values

    from server.service_installation import configure_service, environment_path

    config, _ = installation_settings(tmp_path)
    config.public_base_url = "https://original.example"
    configure_service(config, unit="octopus-managed-test.service", manager="user")
    config.public_base_url = None
    # Stop after the active file is published, before any replacement unit is
    # installed. EnvironmentFile must override an older inherited value too.
    configure_service(config, unit="octopus-managed-test.service", manager="user")
    inherited = dict(os.environ, OCTOPUS_PUBLIC_BASE_URL="https://inherited.example")
    inherited.update({key: value for key, value in dotenv_values(environment_path(config), interpolate=False).items()
                      if value is not None})
    inherited["OCTOPUS_ENV_FILE"] = str(environment_path(config))
    result = subprocess.run(
        [sys.executable, "-c", "import json; from server.config import settings; print(json.dumps(settings.public_base_url))"],
        env=inherited, capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) is None


def test_a_different_installation_cannot_register_the_same_managed_vault(tmp_path, monkeypatch):
    from server.config import settings
    from server.service_installation import configure_service
    from server.wiki.registry import registry
    from server.wiki.store import WikiError

    managed, _ = installation_settings(tmp_path / "managed")
    isolated, _ = installation_settings(tmp_path / "development")
    vault = tmp_path / "notes"
    vault.mkdir()
    note = vault / "note.md"
    note.write_text("# Original\nKeep these words.\n")
    for name, value in managed.model_dump().items():
        monkeypatch.setattr(settings, name, value)
    registry.configure(None, vault, "curator")
    configure_service(managed, unit="octopus-managed-test.service", manager="user")

    for name, value in isolated.model_dump().items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "development-provider-home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "development-claude-home"))
    with pytest.raises(WikiError, match="managed installation"):
        registry.configure(None, vault, "another-curator")
    assert not Path(isolated.wiki_dir, "index.sqlite3").exists()
    assert note.read_text() == "# Original\nKeep these words.\n"


@pytest.mark.parametrize("parent_first", [False, True])
def test_managed_vaults_cannot_overlap_across_installations(tmp_path, monkeypatch, parent_first):
    from server.config import settings
    from server.service_installation import configure_service
    from server.wiki.registry import registry
    from server.wiki.store import WikiError

    parent = tmp_path / "notes"
    child = parent / "nested"
    child.mkdir(parents=True)
    original = child / "note.md"
    original.write_text("# Retained\nOriginal words.\n")
    roots = (parent, child) if parent_first else (child, parent)
    for index, root in enumerate(roots):
        config, _ = installation_settings(tmp_path / str(index))
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / str(index) / "default-codex"))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / str(index) / "default-claude"))
        configure_service(config, unit=f"octopus-overlap-{index}.service", manager="user")
        for name, value in config.model_dump().items():
            monkeypatch.setattr(settings, name, value)
        if index == 0:
            registry.configure(None, root, "curator")
        else:
            with pytest.raises(WikiError) as error:
                registry.configure(None, root, "other-curator")
            assert error.value.status == 409
            assert not Path(config.wiki_dir, "index.sqlite3").exists()
    assert original.read_text() == "# Retained\nOriginal words.\n"


def test_one_managed_vault_cannot_have_two_account_ledgers(tmp_path, monkeypatch):
    from server.config import settings
    from server.service_installation import configure_service
    from server.wiki.registry import registry
    from server.wiki.store import WikiError

    config, _ = installation_settings(tmp_path / "managed")
    configure_service(config, unit="octopus-managed-test.service", manager="user")
    for name, value in config.model_dump().items():
        monkeypatch.setattr(settings, name, value)
    vault = tmp_path / "notes"
    vault.mkdir()
    first = registry.configure("owner-a", vault, "curator-a")
    with pytest.raises(WikiError) as error:
        registry.configure("owner-b", vault, "curator-b")
    assert error.value.status == 409
    assert registry.config("owner-a") == first
    assert not (registry.state("owner-b") / "index.sqlite3").exists()
    registry.close()
    assert registry.store("owner-a").vault_id == first["vault_id"]


@pytest.mark.parametrize("operation", ["open", "setup"])
@pytest.mark.parametrize("nested", [False, True])
def test_existing_conflicting_registrations_do_not_elect_the_first_opener(tmp_path, monkeypatch, operation, nested):
    from server.config import settings
    from server.service_installation import (
        InstallationError,
        configure_service,
        profile_path,
    )
    from server.wiki.registry import registry
    from server.wiki.store import WikiError, WikiStore

    config, _ = installation_settings(tmp_path / "legacy")
    for name, value in config.model_dump().items():
        monkeypatch.setattr(settings, name, value)
    parent = tmp_path / "notes"
    child = parent / "nested" if nested else parent
    child.mkdir(parents=True)
    registrations = {}
    for owner, root in ((None, child), ("owner-b", parent)):
        state = registry.state(owner)
        state.mkdir(parents=True, exist_ok=True)
        vault_id = "00000000-0000-0000-0000-000000000001"
        ledger = state / "index.sqlite3"
        WikiStore.create_database(root, state, vault_id, ledger, health=registry.health)
        raw = json.dumps({"root": str(root), "curator_agent_id": "curator", "vault_id": vault_id,
                          "registration_protocol": 1, "registration_generation": "00000000000000000000000000000002"})
        (state / "config.json").write_text(raw)
        registrations[state] = raw, ledger.read_bytes()
    if operation == "open":
        for owner in (None, "owner-b"):
            with pytest.raises(WikiError, match="conflicting registrations"):
                registry.store(owner)
    else:
        with pytest.raises(InstallationError, match="conflicting registrations"):
            configure_service(config, unit="octopus-legacy-test.service", manager="user")
        assert not profile_path(config).exists()
    for state, (raw, ledger) in registrations.items():
        assert (state / "config.json").read_text() == raw
        assert (state / "index.sqlite3").read_bytes() == ledger
    assert not (parent / ".octopus-vault").exists()
    assert not (child / ".octopus-vault").exists()


def test_valid_registration_keeps_its_body_range_when_the_ledger_is_damaged(tmp_path, monkeypatch):
    from server.config import settings
    from server.service_installation import configure_service
    from server.wiki.registry import registry
    from server.wiki.store import WikiError

    config, _ = installation_settings(tmp_path / "managed")
    for name, value in config.model_dump().items():
        monkeypatch.setattr(settings, name, value)
    vault = tmp_path / "notes"
    vault.mkdir()
    (vault / "note.md").write_bytes(b"# Original retained body\n")
    state = registry.state(None)
    state.mkdir(parents=True)
    raw = json.dumps({"root": str(vault), "curator_agent_id": "curator",
                      "vault_id": "00000000-0000-0000-0000-000000000001",
                      "registration_protocol": 1, "registration_generation": "00000000000000000000000000000002"})
    (state / "config.json").write_text(raw)
    damaged = b"Original damaged ledger; retain for operator verification\n"
    (state / "index.sqlite3").write_bytes(damaged)
    with pytest.raises(WikiError, match="conflicting registrations") as error:
        registry.configure("other-owner", vault, "other-curator")
    assert error.value.status == 409
    assert not (vault / ".octopus-vault").exists()
    configure_service(config, unit="octopus-damaged-ledger-test.service", manager="user")
    assert (vault / ".octopus-vault").read_text().strip() == str(state.resolve())
    assert (state / "config.json").read_text() == raw
    assert (state / "index.sqlite3").read_bytes() == damaged
    assert not (registry.state("other-owner") / "index.sqlite3").exists()
    assert (vault / "note.md").read_bytes() == b"# Original retained body\n"


@pytest.mark.parametrize("failure", ["lock", "scan"])
def test_unavailable_claim_boundaries_do_not_publish_a_writer(tmp_path, monkeypatch, failure):
    import fcntl

    from server.config import settings
    from server.wiki.registry import registry

    config, _ = installation_settings(tmp_path / "isolated")
    for name, value in config.model_dump().items():
        monkeypatch.setattr(settings, name, value)
    vault = tmp_path / "notes"
    vault.mkdir()
    (vault / "note.md").write_bytes(b"# Original retained body\n")
    flock, scandir = fcntl.flock, os.scandir

    def unavailable_lock(descriptor, mode):
        number = descriptor if isinstance(descriptor, int) else descriptor.fileno()
        if Path(os.readlink(f"/proc/self/fd/{number}")) == vault:
            raise PermissionError("claim lock unavailable")
        return flock(descriptor, mode)

    def unavailable_scan(path):
        if Path(path) == vault:
            raise PermissionError("claim scan unavailable")
        return scandir(path)

    with monkeypatch.context() as fault:
        if failure == "lock":
            fault.setattr(fcntl, "flock", unavailable_lock)
        else:
            fault.setattr(os, "scandir", unavailable_scan)
        with pytest.raises(PermissionError, match=f"claim {failure} unavailable"):
            registry.configure(None, vault, "curator")
        assert not (vault / ".octopus-vault").exists()
        assert not (registry.state(None) / "config.json").exists()
        assert not (registry.state(None) / "index.sqlite3").exists()
    registered = registry.configure(None, vault, "curator")
    assert registry.store(None).vault_id == registered["vault_id"]
    assert (vault / "note.md").read_bytes() == b"# Original retained body\n"


@pytest.mark.parametrize("first_scope", ["same", "parent", "child"])
def test_two_processes_cannot_claim_overlapping_vaults(tmp_path, first_scope):
    vault = tmp_path / "shared-vault"
    child = vault / "nested"
    child.mkdir(parents=True)
    roots = (vault, vault) if first_scope == "same" else ((vault, child) if first_scope == "parent" else (child, vault))
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    program = """
import asyncio,fcntl,json,os,sys,time
from pathlib import Path
label,config_file,vault,first_root,barrier=sys.argv[1:]
vault,first_root,barrier=Path(vault),Path(first_root),Path(barrier)
os.environ['CODEX_HOME']=str(Path(config_file).parent/'default-codex')
os.environ['CLAUDE_CONFIG_DIR']=str(Path(config_file).parent/'default-claude')
from server.config import Settings,settings
from server.service_installation import configure_service
from server.wiki.registry import registry
from server.wiki.store import WikiError
config=Settings(_env_file=None,**json.loads(Path(config_file).read_text()))
for name,value in config.model_dump().items(): setattr(settings,name,value)
configure_service(config,unit='octopus-'+label+'.service',manager='user')
flock=fcntl.flock
def controlled_flock(descriptor,mode):
    actual=os.fstat(descriptor if isinstance(descriptor,int) else descriptor.fileno())
    coordinates={(path.stat().st_dev,path.stat().st_ino) for path in (vault,first_root)}
    relevant=(actual.st_dev,actual.st_ino) in coordinates
    if label=='b' and relevant and not mode & fcntl.LOCK_UN:
        (barrier/'b.attempted').touch()
    result=flock(descriptor,mode)
    if label=='a' and relevant and mode & fcntl.LOCK_EX:
        (barrier/'a.locked').touch()
        deadline=time.monotonic()+5
        while not (barrier/'b.result').exists():
            if time.monotonic()>deadline: raise TimeoutError('claim interleave')
            time.sleep(.01)
    return result
fcntl.flock=controlled_flock
if label=='b':
    deadline=time.monotonic()+5
    while not (barrier/'a.locked').exists():
        if time.monotonic()>deadline: raise TimeoutError('first range was not locked')
        time.sleep(.01)
try:
    registry.configure(None,vault,'curator')
except WikiError as error:
    result={'accepted':False,'status':error.status}
else:
    result={'accepted':True}
(barrier/(label+'.result')).write_text(json.dumps(result))
deadline=time.monotonic()+8
while not (barrier/'stop').exists():
    if time.monotonic()>deadline: raise TimeoutError('writer release')
    time.sleep(.01)
asyncio.run(registry.stop())
"""
    processes = []
    try:
        for label, root in zip(("a", "b"), roots, strict=True):
            _, config_file = installation_settings(tmp_path / label)
            processes.append(subprocess.Popen(
                [sys.executable, "-c", program, label, str(config_file), str(root), str(roots[0]), str(barrier)],
                cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            ))
        deadline = time.monotonic() + 8
        while not all((barrier / (label + ".result")).exists() for label in ("a", "b")):
            assert time.monotonic() < deadline, "registration processes did not report their outcomes"
            time.sleep(.01)
        results = [json.loads((barrier / (label + ".result")).read_text()) for label in ("a", "b")]
        assert (barrier / "a.locked").exists()
        assert (barrier / "b.attempted").exists()
        assert results == [{"accepted": True}, {"accepted": False, "status": 409}]
    finally:
        (barrier / "stop").touch()
        outcomes = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            outcomes.append((process.returncode, stdout + stderr))
        assert all(code == 0 for code, _ in outcomes), outcomes


def test_rejected_vault_change_leaves_the_other_directory_available(tmp_path, monkeypatch):
    from server.config import settings
    from server.service_installation import configure_service
    from server.wiki.registry import registry
    from server.wiki.store import WikiError

    managed, _ = installation_settings(tmp_path / "managed")
    isolated, _ = installation_settings(tmp_path / "development")
    first, other = tmp_path / "first", tmp_path / "other"
    first.mkdir()
    other.mkdir()
    for name, value in managed.model_dump().items():
        monkeypatch.setattr(settings, name, value)
    registry.configure(None, first, "curator")
    configure_service(managed, unit="octopus-managed-test.service", manager="user")
    with pytest.raises(WikiError, match="requires a separate migration"):
        registry.configure(None, other, "curator")

    for name, value in isolated.model_dump().items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "development-provider-home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "development-claude-home"))
    assert registry.configure(None, other, "other-curator")["root"] == str(other)


@pytest.mark.parametrize("invalid", ["partial", "identity", "protocol"])
def test_service_setup_does_not_claim_a_vault_from_invalid_registration(tmp_path, monkeypatch, invalid):
    from server.config import settings
    from server.service_installation import configure_service
    from server.wiki.registry import registry
    from server.wiki.store import WikiError

    managed, _ = installation_settings(tmp_path / "managed")
    isolated, _ = installation_settings(tmp_path / "development")
    vault = tmp_path / "notes"
    vault.mkdir()
    registration = {"root": str(vault)}
    if invalid != "partial":
        registration |= {"vault_id": "bad-uuid", "curator_agent_id": "curator"}
    if invalid == "protocol":
        registration |= {"vault_id": "00000000-0000-0000-0000-000000000001", "registration_protocol": 99}
    state = Path(managed.wiki_dir)
    state.mkdir()
    raw = json.dumps(registration)
    (state / "config.json").write_text(raw)
    for name, value in managed.model_dump().items():
        monkeypatch.setattr(settings, name, value)
    with pytest.raises(WikiError, match="registration is invalid"):
        registry.config(None)
    configure_service(managed, unit="octopus-managed-test.service", manager="user")
    assert (state / "config.json").read_text() == raw

    for name, value in isolated.model_dump().items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "development-provider-home"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "development-claude-home"))
    assert registry.configure(None, vault, "other-curator")["root"] == str(vault)
