"""Complete account transfer preserves unfinished application cleanup safely."""

import shutil
from pathlib import Path

import pytest

from server.applications import (
    ApplicationError,
    ApplicationManager,
    allocate_app_dir,
    data_dir_for,
    runtime_dir_for,
)
from server.config import settings
from server.database import Database
from server.portability import export_account, import_account
from server.sessions import SessionManager
from tests.accounts import create_account
from tests.test_service_runtime import installation_settings


async def test_current_github_account_archive_preserves_installation_grant_and_agent_links(tmp_path, monkeypatch):
    import hashlib
    import json
    import sqlite3
    import zipfile

    from server.connector_manager import ConnectorManager
    from server.crypto import encrypt
    from server.users import UserManager

    home = tmp_path / "source"
    home.mkdir()
    working = home / "workspace"
    working.mkdir()
    config, _ = installation_settings(home)
    for name, value in config.model_dump().items():
        monkeypatch.setattr(settings, name, value)
    database = Database(config.db_path)
    await database.initialize()
    try:
        owner, _ = await create_account(database, extra_roots=[working])
        agent = await database.get_system_agent(owner["id"])
        key = await UserManager(database).data_key_for(owner["id"])
        await database.save_connector_installation(installation_id="github-original", kind="github", label="Renamed login",
            auth_type="oauth", external_account_id="91001", scopes=["repo"], enable_by_default=True,
            secret_encrypted=encrypt(json.dumps({"access_token": "original-account-grant", "refresh_token": None,
                "expires_at_epoch": 0, "scopes": ["repo"]}), key), created_at="original-time", user_id=owner["id"])
        await database.set_agent_connector(agent["id"], "github-original", True)
        original = await database.get_connector_installation("github-original", owner["id"])
        await database.save_session("excluded-chat", "Source conversation", str(working), "time", user_id=owner["id"])
    finally:
        await database.close()
    archive = tmp_path / "current-account.zip"
    export_account(config, owner["id"], archive, home=home)
    restored = import_account(archive, home=tmp_path / "restored")
    for name, value in restored.model_dump().items():
        monkeypatch.setattr(settings, name, value)
    copied = Database(restored.db_path)
    await copied.initialize()
    try:
        assert await copied.get_connector_installation("github-original", owner["id"]) == original
        enabled = await ConnectorManager(copied).enabled_installations_for_agent(agent["id"])
        assert [row.id for row in enabled] == ["github-original"]
        token = await ConnectorManager(copied).get_access_token(enabled[0].id, owner["id"])
        assert token["access_token"] == "original-account-grant"
        assert await copied.load_sessions(user_id=owner["id"], include_archived=True) == []
    finally:
        await copied.close()

    # A correctly inventoried archive with an old marker must be refused before
    # creating target files. Import is not a second publication converter.
    with zipfile.ZipFile(archive) as package:
        entries = {name: package.read(name) for name in package.namelist()}
    manifest = json.loads(entries["manifest.json"])
    db_name = Path(config.db_path).relative_to(home).as_posix()
    with sqlite3.connect(":memory:") as old:
        old.deserialize(entries["files/" + db_name])
        old.execute("PRAGMA user_version=8")
        old_bytes = old.serialize()
    entries["files/" + db_name] = old_bytes
    manifest["entries"][db_name].update(size=len(old_bytes), sha256=hashlib.sha256(old_bytes).hexdigest())
    entries["manifest.json"] = json.dumps(manifest).encode()
    old_archive = tmp_path / "old-marker-account.zip"
    with zipfile.ZipFile(old_archive, "w") as package:
        for name, content in entries.items():
            package.writestr(name, content)
    target = tmp_path / "refused"
    with pytest.raises(RuntimeError, match="current storage format"):
        import_account(old_archive, home=target)
    assert not target.exists()


@pytest.mark.parametrize("phase", ["retiring", "files"])
@pytest.mark.parametrize("identity_path", [False, True])
async def test_export_import_preserves_remaining_application_files_and_explicit_retry(
    tmp_path, monkeypatch, phase, identity_path,
):
    home = tmp_path / "source"
    home.mkdir()
    working = home / "workspace"
    working.mkdir()
    config, _ = installation_settings(home)
    for name, value in config.model_dump().items():
        monkeypatch.setattr(settings, name, value)
    database = Database(config.db_path)
    await database.initialize()
    owner, _ = await create_account(database, extra_roots=[working])
    code = (Path(allocate_app_dir("Partial", "partial", owner["id"])) if identity_path
            else Path(config.users_root) / owner["id"] / "applications" / "partial")
    data, runtime = Path(data_dir_for(str(code))), Path(runtime_dir_for(str(code)))
    for directory in (code, data, runtime):
        directory.mkdir(parents=True)
        (directory / "original.txt").write_text(directory.name)
    await database.save_application(app_id="partial", name="Partial", app_dir=str(code),
                                    created_at="time", updated_at="time", user_id=owner["id"])
    manager = SessionManager()
    await manager.initialize(database)
    agent = await database.get_system_agent(owner["id"])
    await database.update_agent(agent["id"], model="source-model")
    await database.update_agent(agent["id"], model="source-model-updated")
    assert (await database.get_agent(agent["id"]))["context_settings_revision"] == 2
    conversation = await manager.create_session(agent["id"], "Source context", str(working))
    await manager.update_session_configuration(conversation.id, {"model": "conversation-model"}, owner["id"])
    assert (await database.load_sessions())[0]["context_state"] is not None
    applications = ApplicationManager()
    applications.bind(manager, database)
    try:
        with monkeypatch.context() as faults:
            if phase == "files":
                original = shutil.rmtree

                def fail_data(path, *args, **kwargs):
                    if Path(path) == data:
                        raise PermissionError("remaining application data")
                    return original(path, *args, **kwargs)

                faults.setattr(shutil, "rmtree", fail_data)
            else:
                from server.app_backends import backend_supervisor

                async def fail_stop(_):
                    raise RuntimeError("reader still retained")

                faults.setattr(backend_supervisor, "stop", fail_stop)
            with pytest.raises(ApplicationError):
                await applications.delete_application("partial")
        state = (await database.get_application("partial"))["deletion"]
        assert state["phase"] == phase
    finally:
        applications.shutdown()
        await database.close()

    archive = tmp_path / "account.zip"
    export_account(config, owner["id"], archive, home=home)
    target_home = tmp_path / "restored"
    restored = import_account(archive, home=target_home)
    restored_code = target_home / code.relative_to(home)
    assert restored_code.exists() is (phase == "retiring")
    assert (Path(data_dir_for(str(restored_code))) / "original.txt").read_text() == data.name
    assert not Path(runtime_dir_for(str(restored_code))).exists()
    for name, value in restored.model_dump().items():
        monkeypatch.setattr(settings, name, value)
    copied = Database(restored.db_path)
    await copied.initialize()
    replacement = SessionManager()
    await replacement.initialize(copied)
    retry = ApplicationManager()
    retry.bind(replacement, copied)
    try:
        row = await copied.get_application("partial")
        assert row["deletion"] == state and row["session_id"] is None
        assert await copied.load_sessions(include_archived=True) == []
        imported_agent = await copied.get_agent(agent["id"])
        assert imported_agent["context_settings_revision"] == 0
        assert imported_agent["model"] == "source-model-updated"
        with pytest.raises(ApplicationError, match="delet"):
            await retry.request_build("partial", "must not restart a writer")
        await retry.delete_application("partial")
        assert await copied.get_application("partial") is None
        assert not restored_code.exists() and not Path(data_dir_for(str(restored_code))).exists()
    finally:
        retry.shutdown()
        await copied.close()


@pytest.mark.parametrize("resource", ["code", "data", "other-code", "other-data"])
@pytest.mark.parametrize("overlap", ["same", "ancestor", "descendant"])
async def test_export_refuses_runtime_overlaps_required_resources(tmp_path, monkeypatch, resource, overlap):
    home = tmp_path / "source"
    home.mkdir()
    config, _ = installation_settings(home)
    for name, value in config.model_dump().items():
        monkeypatch.setattr(settings, name, value)
    database = Database(config.db_path)
    await database.initialize()
    owner, _ = await create_account(database)
    root = Path(config.users_root) / owner["id"] / "applications"
    resources = {}
    for app_id in ("original", "other"):
        code = root / app_id
        for label, path in (("code", code), ("data", Path(data_dir_for(str(code))))):
            path.mkdir(parents=True)
            (path / "original.txt").write_text(app_id + label)
            resources[(app_id, label)] = path
        await database.save_application(app_id=app_id, name=app_id, app_dir=str(code), status="ready",
                                        created_at="time", updated_at="time", user_id=owner["id"])
    target = resources[("other" if resource.startswith("other-") else "original", resource.split("-")[-1])]
    if overlap == "ancestor":
        target = target.parent
    elif overlap == "descendant":
        target = target / "durable-directory"
        target.mkdir()
        (target / "original.txt").write_text("durable descendant")
    runtime = Path(runtime_dir_for(str(root / "original")))
    runtime.symlink_to(target, target_is_directory=True)
    await database.close()

    archive = tmp_path / "account.zip"
    with pytest.raises(ValueError, match="original") as refused:
        export_account(config, owner["id"], archive, home=home)
    assert str(target) in str(refused.value)
    assert "runtime" in str(refused.value).lower()
    assert not archive.exists()
    assert runtime.is_symlink()
    for (app_id, label), path in resources.items():
        assert (path / "original.txt").read_text() == app_id + label


@pytest.mark.parametrize("sibling_aliases", [False, True])
@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("layout", ["leaf", "parent", "parent-and-leaf"])
async def test_account_transfer_preserves_app_directory_alias_and_its_actual_siblings(
    tmp_path, monkeypatch, sibling_aliases, external, layout,
):
    home = tmp_path / "source"
    home.mkdir()
    config, _ = installation_settings(home)
    for name, value in config.model_dump().items():
        monkeypatch.setattr(settings, name, value)
    database = Database(config.db_path)
    await database.initialize()
    owner, _ = await create_account(database)
    root = home / "legacy-applications" if external else Path(config.users_root) / owner["id"] / "applications"
    root.mkdir(parents=True, exist_ok=True)
    code_parent = root if layout == "leaf" else root / "actual-parent"
    code_parent.mkdir(exist_ok=True)
    code = code_parent / "actual-code"
    code.mkdir()
    (code / "index.html").write_text("original code")
    if layout == "leaf":
        alias = root / "alias"
        alias.symlink_to(code, target_is_directory=True)
    else:
        parent_alias = root / "parent-alias"
        parent_alias.symlink_to(code_parent, target_is_directory=True)
        alias = parent_alias / ("actual-code" if layout == "parent" else "alias")
        if layout == "parent-and-leaf":
            alias.symlink_to(code, target_is_directory=True)
    data = Path(data_dir_for(str(alias)))
    runtime = Path(runtime_dir_for(str(alias)))
    if sibling_aliases:
        for path, target in ((data, root / "actual-data"), (runtime, root / "actual-runtime")):
            target.mkdir()
            path.symlink_to(target, target_is_directory=True)
    else:
        data.mkdir()
        runtime.mkdir()
    (data / "original.txt").write_text("durable state")
    (runtime / "excluded.txt").write_text("derived dependencies")
    await database.save_application(app_id="alias-app", name="Alias", app_dir=str(alias), status="ready",
                                    created_at="time", updated_at="time", user_id=owner["id"])
    await database.close()

    archive = tmp_path / "account.zip"
    export_account(config, owner["id"], archive, home=home)
    target_home = tmp_path / "restored"
    restored = import_account(archive, home=target_home)
    restored_alias = target_home / alias.relative_to(home)
    assert restored_alias.is_symlink() == (layout != "parent")
    if layout != "leaf":
        assert restored_alias.parent.is_symlink()
    assert (restored_alias / "index.html").read_text() == "original code"
    assert (Path(data_dir_for(str(restored_alias))) / "original.txt").read_text() == "durable state"
    assert not Path(runtime_dir_for(str(restored_alias))).is_symlink()
    assert not Path(runtime_dir_for(str(restored_alias))).exists()
    assert not (target_home / root.relative_to(home) / "actual-runtime").exists()
    for name, value in restored.model_dump().items():
        monkeypatch.setattr(settings, name, value)
    copied = Database(restored.db_path)
    await copied.initialize()
    try:
        assert (await copied.get_application("alias-app"))["app_dir"] == str(restored_alias)
    finally:
        await copied.close()
