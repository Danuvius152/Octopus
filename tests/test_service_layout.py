"""Account installations do not manufacture unused single-user storage."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from server.database import Database
from server.service_installation import (
    InstallationError,
    _paths,
    configure_service,
    environment_path,
    legacy_shells,
    profile_path,
    prune_legacy_shells,
    read_service_profile,
)
from tests.test_service_runtime import installation_settings


@pytest.fixture
async def account_installation(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "provider-codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "provider-claude"))
    config, _ = installation_settings(tmp_path / "installation")
    assert all(Path(raw).is_relative_to(tmp_path) for raw in _paths(config).values())
    database = Database(config.db_path)
    await database.initialize()
    try:
        await database.create_user(user_id="owner", username="owner", password_hash="unused",
                                   created_at="2026-10-21", is_admin=True)
    finally:
        await database.close()
    account = Path(config.users_root) / "owner"
    (account / "workspace").mkdir(parents=True)
    return config, account


def legacy_directories(config):
    return [Path(getattr(config, name)) for name in (
        "attachments_dir", "large_prompts_dir", "codex_home_dir", "research_dir",
        "wiki_dir", "agents_dir", "applications_dir", "default_working_dir",
    )]


async def test_account_setup_and_profile_checks_do_not_create_legacy_directories(account_installation):
    config, account = account_installation
    profile = configure_service(config, unit="octopus-layout-test.service", manager="user")
    assert all(not path.exists() for path in legacy_directories(config))
    assert (account / "workspace").is_dir()
    assert read_service_profile(config) == profile
    assert configure_service(config, unit="octopus-layout-test.service", manager="user") == profile
    assert all(not path.exists() for path in legacy_directories(config))


async def test_existing_single_user_profile_survives_account_cleanup(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "provider-codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "provider-claude"))
    config, _ = installation_settings(tmp_path / "installation")
    assert all(Path(raw).is_relative_to(tmp_path) for raw in _paths(config).values())
    profile = configure_service(config, unit="octopus-layout-test.service", manager="user")
    assert all(path.is_dir() for path in legacy_directories(config))
    database = Database(config.db_path)
    await database.initialize()
    try:
        await database.create_user(user_id="owner", username="owner", password_hash="unused",
                                   created_at="2026-10-21", is_admin=True)
    finally:
        await database.close()
    account = Path(config.users_root) / "owner" / "workspace"
    account.mkdir(parents=True)
    assert set(prune_legacy_shells(config)) == set(legacy_directories(config))
    assert all(not path.exists() for path in legacy_directories(config))
    assert account.is_dir()
    assert read_service_profile(config) == profile
    assert configure_service(config, unit="octopus-layout-test.service", manager="user") == profile
    assert all(not path.exists() for path in legacy_directories(config))


@pytest.mark.parametrize("link_root", [False, True])
async def test_cleanup_preserves_symlinks_and_their_targets(account_installation, link_root):
    config, account = account_installation
    root = Path(config.research_dir)
    target = account / "original-research"
    target.mkdir()
    if link_root:
        root.symlink_to(target, target_is_directory=True)
    else:
        root.mkdir()
        (root / "linked-research").symlink_to(target, target_is_directory=True)
    configure_service(config, unit="octopus-layout-test.service", manager="user")
    assert root not in legacy_shells(config)
    assert prune_legacy_shells(config) == []
    assert root.exists() and target.is_dir()
    assert (root if link_root else root / "linked-research").is_symlink()
    assert read_service_profile(config) is not None


async def test_cleanup_removes_owned_shells_and_preserves_account_and_legacy_data(account_installation):
    config, account = account_installation
    retained = Path(config.attachments_dir)
    retained.mkdir()
    original = retained / "original-upload.txt"
    original.write_bytes(b"original upload")
    current = account / "wiki" / "index.sqlite3"
    current.parent.mkdir()
    current.write_bytes(b"original account ledger")
    configure_service(config, unit="octopus-layout-test.service", manager="user")
    root = Path(config.research_dir)
    (root / "old-empty-job" / "cwd").mkdir(parents=True)
    (root / ".octopus-installation").write_text(str(profile_path(config)) + "\n")
    assert root in legacy_shells(config)
    assert prune_legacy_shells(config) == [root]
    assert not root.exists()
    assert original.read_bytes() == b"original upload"
    assert current.read_bytes() == b"original account ledger"
    assert read_service_profile(config) is not None
    assert prune_legacy_shells(config) == []
    assert read_service_profile(config) is not None


async def test_foreign_or_unmarked_directories_are_not_cleanup_targets(account_installation):
    config, _ = account_installation
    root = Path(config.research_dir)
    root.mkdir()
    assert root not in legacy_shells(config)
    marker = root / ".octopus-installation"
    marker.write_text(str(root.parent / "other.db.service.json") + "\n")
    assert root not in legacy_shells(config)
    with pytest.raises(InstallationError, match="another managed installation"):
        configure_service(config, unit="octopus-layout-test.service", manager="user")
    assert marker.read_text().strip() == str(root.parent / "other.db.service.json")


def test_single_user_setup_still_claims_its_real_default_directories(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "provider-codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "provider-claude"))
    config, _ = installation_settings(tmp_path / "installation")
    profile = configure_service(config, unit="octopus-layout-test.service", manager="user")
    assert legacy_shells(config) == []
    assert all(path.is_dir() for path in legacy_directories(config))
    assert read_service_profile(config) == profile


async def test_new_data_during_cleanup_is_retained_and_its_marker_restored(account_installation, monkeypatch):
    config, _ = account_installation
    configure_service(config, unit="octopus-layout-test.service", manager="user")
    root = Path(config.research_dir)
    root.mkdir()
    marker = root / ".octopus-installation"
    marker.write_text(str(profile_path(config)) + "\n")
    rmdir = Path.rmdir

    def create_before_removal(directory):
        if directory == root:
            (root / "late-report.md").write_bytes(b"retain concurrent report")
        return rmdir(directory)

    monkeypatch.setattr(Path, "rmdir", create_before_removal)
    with pytest.raises(OSError):
        prune_legacy_shells(config)
    assert (root / "late-report.md").read_bytes() == b"retain concurrent report"
    assert marker.read_text().strip() == str(profile_path(config))
    assert read_service_profile(config) is not None


async def test_an_empty_saved_working_directory_remains_in_use(account_installation):
    config, _ = account_installation
    root = Path(config.default_working_dir)
    project = root / "original-project"
    project.mkdir(parents=True)
    (root / ".octopus-installation").write_text(str(profile_path(config)) + "\n")
    database = Database(config.db_path)
    await database.initialize()
    try:
        await database.save_session("original", "Original work", str(project), "2026-10-21", user_id="owner")
    finally:
        await database.close()
    configure_service(config, unit="octopus-layout-test.service", manager="user")
    assert root not in legacy_shells(config)
    assert prune_legacy_shells(config) == []
    assert project.is_dir()
    assert read_service_profile(config) is not None


@pytest.mark.parametrize("nested", [False, True])
async def test_an_empty_granted_root_without_sessions_remains_in_use(account_installation, nested):
    config, _ = account_installation
    root = Path(config.default_working_dir)
    granted = root / "approved-project" if nested else root
    granted.mkdir(parents=True)
    (root / ".octopus-installation").write_text(str(profile_path(config)) + "\n")
    database = Database(config.db_path)
    await database.initialize()
    try:
        await database.update_user_field("owner", extra_roots=json.dumps([str(granted)]))
    finally:
        await database.close()
    configure_service(config, unit="octopus-layout-test.service", manager="user")
    assert root not in legacy_shells(config)
    assert prune_legacy_shells(config) == []
    assert granted.is_dir()
    assert read_service_profile(config) is not None


async def test_release_check_rejects_shells_then_checks_the_cleaned_layout(account_installation):
    config, _ = account_installation
    configure_service(config, unit="octopus-layout-test.service", manager="user")
    root = Path(config.wiki_dir)
    root.mkdir()
    (root / ".octopus-installation").write_text(str(profile_path(config)) + "\n")
    command = [sys.executable, str(Path(__file__).resolve().parents[1] / "scripts/check-storage-layout.py")]
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("OCTOPUS_")}
    environment["OCTOPUS_ENV_FILE"] = str(environment_path(config))
    rejected = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=10)
    assert rejected.returncode == 1, rejected.stderr
    assert json.loads(rejected.stdout)["unused_legacy_directories"] == [str(root)]
    cleaned = subprocess.run([*command, "--prune"], env=environment, capture_output=True, text=True, timeout=10)
    assert cleaned.returncode == 0, cleaned.stderr
    assert json.loads(cleaned.stdout)["removed"] == [str(root)]
    assert not root.exists()
    checked = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=10)
    assert checked.returncode == 0, checked.stderr
    assert json.loads(checked.stdout)["unused_legacy_directories"] == []


async def test_hermetic_checks_do_not_reuse_managed_host_storage(tmp_path, monkeypatch):
    host = tmp_path / "managed-host"
    monkeypatch.setenv("CODEX_HOME", str(host / "provider-codex"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(host / "provider-claude"))
    config, _ = installation_settings(host)
    assert all(Path(raw).is_relative_to(host) for raw in _paths(config).values())
    configure_service(config, unit="octopus-layout-test.service", manager="user")

    def stored_files():
        return {str(path.relative_to(host)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in host.rglob("*") if path.is_file()}

    before = stored_files()
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("OCTOPUS_")}
    environment["OCTOPUS_ENV_FILE"] = str(environment_path(config))
    result = subprocess.run(
        [sys.executable, "-m", "pytest",
         "tests/test_bootstrap.py::test_bootstrap_adopts_registered_wiki_state_and_exact_root",
         "--basetemp", str(tmp_path / "isolated-checks"), "-q", "--tb=short"],
        cwd=Path(__file__).resolve().parents[1], env=environment,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert stored_files() == before
