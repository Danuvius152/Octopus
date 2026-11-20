"""Complete account transfer preserves unfinished application cleanup safely."""

import hashlib
import json
import shutil
import sqlite3
import zipfile
from contextlib import closing
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
        await database.set_owned_agent_connector(agent["id"], "github-original", True, owner["id"])
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


@pytest.mark.parametrize(("phase", "identity_path"), [
    (phase, path) for phase in ("retiring", "files") for path in (False, True, "alias")
] + [("link", "alias")])
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
    if identity_path == "alias":
        content = code.with_name("original-code")
        content.mkdir(parents=True)
        code.symlink_to(content, target_is_directory=True)
    data, runtime = Path(data_dir_for(str(code))), Path(runtime_dir_for(str(code)))
    for directory in (code, data, runtime):
        directory.mkdir(parents=True, exist_ok=True)
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
            elif phase == "link":
                unlink = Path.unlink

                def fail_link(path, *args, **kwargs):
                    if path == code:
                        raise PermissionError("remaining application access link")
                    return unlink(path, *args, **kwargs)

                faults.setattr(Path, "unlink", fail_link)
            else:
                from server.app_backends import backend_supervisor

                async def fail_stop(_):
                    raise RuntimeError("reader still retained")

                faults.setattr(backend_supervisor, "stop", fail_stop)
            with pytest.raises(ApplicationError):
                await applications.delete_application("partial")
        state = (await database.get_application("partial"))["deletion"]
        assert state["phase"] == ("files" if phase == "link" else phase)
    finally:
        applications.shutdown()
        await database.close()

    archive = tmp_path / "account.zip"
    export_account(config, owner["id"], archive, home=home)
    if phase == "files" and identity_path is False:
        with zipfile.ZipFile(archive) as package:
            members = {name: package.read(name) for name in package.namelist()}
        manifest = json.loads(members["manifest.json"])
        _remove_archive_resource(manifest, members, data.relative_to(home).as_posix())
        incomplete = tmp_path / "lost-remaining-data.zip"
        _write_modified_archive(incomplete, manifest, members)
        refused = tmp_path / "refused"
        with pytest.raises(ValueError, match="missing declared data"):
            import_account(incomplete, home=refused)
        assert not (refused / Path(config.db_path).relative_to(home)).exists()
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
    manager = SessionManager()
    await manager.initialize(copied)
    applications = ApplicationManager()
    applications.bind(manager, copied)
    try:
        assert (await copied.get_application("alias-app"))["app_dir"] == str(restored_alias)
        if not external:
            original_code = restored_alias.resolve()
            restored_data = Path(data_dir_for(str(restored_alias)))
            original_data = restored_data.resolve()
            await applications.delete_application("alias-app")
            assert await copied.get_application("alias-app") is None
            assert not original_code.exists() and not original_data.exists()
            assert not restored_alias.exists() and not restored_alias.is_symlink()
            assert not restored_data.exists() and not restored_data.is_symlink()
    finally:
        applications.shutdown()
        await manager.close_session_owners()
        await copied.close()


async def _export_parent_link_account(tmp_path, monkeypatch, *, external=True, include_data=True,
                                      runtime_link=False, key_in_traversal=False):
    source_home = tmp_path / "old user"
    source_home.mkdir()
    config, _ = installation_settings(source_home)
    if key_in_traversal:
        config = config.model_copy(update={"master_key_file": str(source_home / "project files" / "nested" / "master.key")})
    for name, value in config.model_dump().items():
        monkeypatch.setattr(settings, name, value)
    database = Database(config.db_path)
    await database.initialize()
    try:
        owner, _ = await create_account(database)
        actual = (source_home / "project files" if external else
                  Path(config.users_root) / owner["id"] / "applications" / "actual")
        traversal = actual / ("other.runtime" if runtime_link else "nested")
        traversal.mkdir(parents=True, exist_ok=True)
        (traversal / "unrelated.txt").write_text("not application content")
        access = source_home / "entry points"
        access.mkdir()
        (access / "jump").symlink_to(traversal, target_is_directory=True)
        stored = access / "jump" / ".." / "demo"
        for suffix, content in (("", "declared code"), (".data", "durable state"), (".runtime", "excluded runtime")):
            if suffix == ".data" and not include_data:
                continue
            directory = actual / ("demo" + suffix)
            directory.mkdir()
            (directory / "marker.txt").write_text(content)
        await database.save_application(app_id="demo", name="Demo", app_dir=str(stored), status="ready",
                                        created_at="time", updated_at="time", user_id=owner["id"])
        if runtime_link:
            other = actual / "other"
            other.mkdir()
            await database.save_application(app_id="other", name="Other", app_dir=str(other), status="ready",
                                            created_at="time", updated_at="time", user_id=owner["id"])
    finally:
        await database.close()

    archive = tmp_path / "account.zip"
    export_account(config, owner["id"], archive, home=source_home)
    return archive, Path(config.db_path).relative_to(source_home)


@pytest.mark.parametrize("external", [False, True])
async def test_account_transfer_resolves_parent_link_before_parent_step(tmp_path, monkeypatch, external):
    archive, _ = await _export_parent_link_account(tmp_path, monkeypatch, external=external)
    target_home = tmp_path / "new host" / "new user"
    decoy = target_home / "entry points" / "demo"
    decoy.mkdir(parents=True)
    (decoy / "marker.txt").write_text("unrelated target directory")
    restored = import_account(archive, home=target_home)

    copied = Database(restored.db_path)
    await copied.initialize()
    try:
        row = await copied.get_application("demo")
        assert row is not None
        restored_address = Path(row["app_dir"])
        assert restored_address == target_home / "entry points" / "jump" / ".." / "demo"
        assert (restored_address / "marker.txt").read_text() == "declared code"
        assert (Path(data_dir_for(str(restored_address))) / "marker.txt").read_text() == "durable state"
        assert not Path(runtime_dir_for(str(restored_address))).exists()
        assert (decoy / "marker.txt").read_text() == "unrelated target directory"
        if external:
            jump = target_home / "entry points" / "jump"
            assert jump.is_symlink() and jump.resolve() == target_home / "project files" / "nested"
            assert list(jump.iterdir()) == []
    finally:
        await copied.close()


def _remove_archive_resource(manifest, members, resource):
    manifest["roots"] = [name for name in manifest["roots"] if name != resource]
    for name in tuple(manifest["entries"]):
        if Path(name).is_relative_to(resource):
            del manifest["entries"][name]
            members.pop("files/" + name, None)


def _write_modified_archive(archive, manifest, members):
    members["manifest.json"] = json.dumps(manifest).encode()
    with zipfile.ZipFile(archive, "w") as package:
        for name, content in members.items():
            package.writestr(name, content)


def _set_archived_application_address(manifest, members, db_relative, address):
    db_name = "files/" + db_relative.as_posix()
    with closing(sqlite3.connect(":memory:")) as database:
        database.deserialize(members[db_name])
        with database:
            database.execute("UPDATE applications SET app_dir=? WHERE id='demo'", (address,))
        db_bytes = database.serialize()
    members[db_name] = db_bytes
    manifest["entries"][db_relative.as_posix()].update(size=len(db_bytes), sha256=hashlib.sha256(db_bytes).hexdigest())


@pytest.mark.parametrize(("problem", "message"), [
    ("link", "missing declared code"),
    ("data", "missing declared data"),
    ("code-scaffold", "missing declared code"),
    ("removed-code", "missing required code"),
    ("escape", "steps outside the selected home"),
])
async def test_account_import_refuses_unverified_application_paths(tmp_path, monkeypatch, problem, message):
    archive, db_relative = await _export_parent_link_account(tmp_path, monkeypatch)
    with zipfile.ZipFile(archive) as package:
        members = {name: package.read(name) for name in package.namelist()}
    manifest = json.loads(members["manifest.json"])
    if problem == "link":
        _remove_archive_resource(manifest, members, "entry points/jump")
        del manifest["entries"]["entry points"]
    elif problem == "data":
        _remove_archive_resource(manifest, members, "project files/demo.data")
    elif problem == "code-scaffold":
        root = manifest["roots"].index("project files/demo")
        manifest["roots"][root] = "project files/demo/marker.txt"
    elif problem == "removed-code":
        _remove_archive_resource(manifest, members, "project files/demo")
        manifest["applications"]["demo"]["code"] = None
    else:
        source_home = Path(manifest["source_home"])
        escaped = source_home / "entry points" / ".." / ".." / source_home.name / "entry points" / "jump" / ".." / "demo"
        _set_archived_application_address(manifest, members, db_relative, str(escaped))
    modified = tmp_path / "incomplete.zip"
    _write_modified_archive(modified, manifest, members)

    target_home = tmp_path / "unrelated host"
    decoy = target_home / "project files" / "demo.data" if problem == "data" else target_home / "entry points" / "demo"
    decoy.mkdir(parents=True)
    (decoy / "keep.txt").write_text("unrelated")
    if problem == "link":
        (decoy.parent / "jump").mkdir()
    with pytest.raises(ValueError, match=message):
        import_account(modified, home=target_home)
    assert (decoy / "keep.txt").read_text() == "unrelated"
    assert not (target_home / db_relative).exists()
    assert not (target_home / db_relative.parent / (db_relative.name + ".import.env")).exists()


@pytest.mark.parametrize("stored_suffix", ["", "/."])
async def test_account_import_refuses_existing_data_at_an_absent_application_resource(tmp_path, monkeypatch, stored_suffix):
    archive, db_relative = await _export_parent_link_account(tmp_path, monkeypatch, include_data=False)
    if stored_suffix:
        with zipfile.ZipFile(archive) as package:
            members = {name: package.read(name) for name in package.namelist()}
        manifest = json.loads(members["manifest.json"])
        stored = Path(manifest["source_home"]) / "entry points" / "jump" / ".." / "demo"
        _set_archived_application_address(manifest, members, db_relative, str(stored) + stored_suffix)
        archive = tmp_path / "stored-suffix.zip"
        _write_modified_archive(archive, manifest, members)
    empty_home = tmp_path / "empty user"
    import_account(archive, home=empty_home)
    assert (empty_home / "project files" / "demo").is_dir()
    assert not (empty_home / "project files" / "demo.data").exists()

    target_home = tmp_path / "existing user"
    existing_data = target_home / "project files" / "demo.data"
    existing_data.mkdir(parents=True)
    (existing_data / "keep.txt").write_text("unrelated")
    with pytest.raises(ValueError, match="Undeclared application path already exists"):
        import_account(archive, home=target_home)
    assert (existing_data / "keep.txt").read_text() == "unrelated"
    assert not (target_home / db_relative).exists()


async def test_account_import_reuses_shared_parent_of_key_and_application_link(tmp_path, monkeypatch):
    archive, _ = await _export_parent_link_account(tmp_path, monkeypatch, key_in_traversal=True)
    target_home = tmp_path / "new user"
    shared = target_home / "project files" / "nested"
    shared.mkdir(parents=True)
    (shared / "keep.txt").write_text("unrelated target file")
    copied = import_account(archive, home=target_home)

    jump = target_home / "entry points" / "jump"
    assert jump.resolve() == shared
    assert Path(copied.master_key_file) == shared / "master.key"
    assert {child.name for child in shared.iterdir()} == {"master.key", "keep.txt"}
    assert (shared / "keep.txt").read_text() == "unrelated target file"
    assert (jump / ".." / "demo" / "marker.txt").read_text() == "declared code"


@pytest.mark.parametrize("layout", [
    "retained-link", "generated-environment", "shared-parent", "undeclared-parent-link", "occupied-data",
])
async def test_account_transfer_checks_paths_after_application_files_were_removed(tmp_path, monkeypatch, layout):
    source_home = tmp_path / "old user"
    config, _ = installation_settings(source_home)
    for name, value in config.model_dump().items():
        monkeypatch.setattr(settings, name, value)
    if layout == "retained-link":
        directory = source_home / "project files" / "parent" / "nested"
        directory.mkdir(parents=True)
        (source_home / "jump").symlink_to(directory, target_is_directory=True)
        stored = source_home / "jump" / ".." / ".." / "demo"
    elif layout == "generated-environment":
        stored = Path(config.db_path + ".import.env")
    else:
        stored = source_home / "projects" / "demo"
        stored.parent.mkdir()
    deletion = {"phase": "files", "keep_files": False, "error": "unfinished cleanup"}
    database = Database(config.db_path)
    await database.initialize()
    try:
        owner, _ = await create_account(database)
        await database.save_application(app_id="removed", name="Removed", app_dir=str(stored), status="ready",
                                        created_at="time", updated_at="time", user_id=owner["id"])
        await database.set_application_deletion("removed", deletion)
    finally:
        await database.close()
    archive = tmp_path / "removed.zip"
    export_account(config, owner["id"], archive, home=source_home)

    target_home = tmp_path / "new user"
    restored_address = target_home / stored.relative_to(source_home)
    if layout in {"shared-parent", "undeclared-parent-link", "occupied-data"}:
        target_home.mkdir()
        projects = target_home / "projects"
        if layout == "undeclared-parent-link":
            other = target_home / "other"
            other.mkdir()
            projects.symlink_to(other, target_is_directory=True)
        else:
            projects.mkdir()
        (projects / "keep.txt").write_text("unrelated target file")
        if layout == "occupied-data":
            Path(data_dir_for(str(restored_address))).mkdir()
    if layout in {"generated-environment", "undeclared-parent-link", "occupied-data"}:
        with pytest.raises(ValueError, match="Undeclared application path already exists"):
            import_account(archive, home=target_home)
        if layout != "generated-environment":
            assert (target_home / "projects" / "keep.txt").read_text() == "unrelated target file"
        assert not restored_address.exists()
        assert not (target_home / Path(config.db_path).relative_to(source_home)).exists()
        return

    restored = import_account(archive, home=target_home)
    copied = Database(restored.db_path)
    await copied.initialize()
    try:
        row = await copied.get_application("removed")
        assert row is not None and row["deletion"] == deletion
        assert row["app_dir"] == str(restored_address)
        if layout == "retained-link":
            assert (target_home / "jump").resolve(strict=True) == target_home / "project files" / "parent" / "nested"
        else:
            assert list(restored_address.parent.iterdir()) == [restored_address.parent / "keep.txt"]
            assert (restored_address.parent / "keep.txt").read_text() == "unrelated target file"
        assert not restored_address.exists()
        assert not Path(data_dir_for(str(restored_address))).exists()
        assert not Path(runtime_dir_for(str(restored_address))).exists()
    finally:
        await copied.close()


async def test_account_export_refuses_runtime_used_as_a_traversal_directory(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="runtime exclusion .* traversal directory"):
        await _export_parent_link_account(tmp_path, monkeypatch, runtime_link=True)
    assert not (tmp_path / "account.zip").exists()


@pytest.mark.parametrize("outcome", ["delete", "retry", "keep"])
async def test_transferred_shared_link_chain_preserves_other_application_and_cleanup_retry(tmp_path, monkeypatch, outcome):
    home = tmp_path / "source"
    home.mkdir()
    config, _ = installation_settings(home)
    for name, value in config.model_dump().items():
        monkeypatch.setattr(settings, name, value)
    database = Database(config.db_path)
    await database.initialize()
    owner, _ = await create_account(database)
    root = Path(config.users_root) / owner["id"] / "applications"
    actual = root / "actual"
    actual.mkdir(parents=True)
    inner, outer = root / "inner", root / "outer"
    inner.symlink_to(actual, target_is_directory=True)
    outer.symlink_to(inner, target_is_directory=True)
    for name in ("selected", "other"):
        stored = outer / name
        for suffix in ("", ".data", ".runtime"):
            directory = Path(str(stored) + suffix)
            directory.mkdir()
            (directory / "original.txt").write_text(name + suffix)
        await database.save_application(app_id=name, name=name, app_dir=str(stored), status="ready",
                                        created_at="time", updated_at="time", user_id=owner["id"])
    await database.close()
    archive = tmp_path / "account.zip"
    export_account(config, owner["id"], archive, home=home)
    target = tmp_path / "restored"
    restored = import_account(archive, home=target)
    imported_root = target / root.relative_to(home)
    imported_inner, imported_outer = imported_root / "inner", imported_root / "outer"
    # Import may flatten aliases. Reintroduce the equivalent chain to exercise
    # effect-time interpretation rather than relying on an export-time plan.
    imported_outer.unlink()
    imported_outer.symlink_to(imported_inner, target_is_directory=True)
    for name, value in restored.model_dump().items():
        monkeypatch.setattr(settings, name, value)
    copied = Database(restored.db_path)
    await copied.initialize()
    manager = SessionManager()
    await manager.initialize(copied)
    applications = ApplicationManager()
    applications.bind(manager, copied)
    selected = imported_outer / "selected"
    data = Path(data_dir_for(str(selected))).resolve()
    try:
        if outcome == "retry":
            remove = shutil.rmtree

            def fail_data(path, *args, **kwargs):
                if Path(path) == data:
                    raise PermissionError("selected data is locked")
                return remove(path, *args, **kwargs)

            with monkeypatch.context() as faults:
                faults.setattr(shutil, "rmtree", fail_data)
                with pytest.raises(ApplicationError, match="locked"):
                    await applications.delete_application("selected")
            assert (await copied.get_application("selected"))["deletion"]["phase"] == "files"
            assert not selected.exists() and (data / "original.txt").read_text() == "selected.data"
        await applications.delete_application("selected", keep_files=outcome == "keep")
        assert await copied.get_application("selected") is None
        assert imported_inner.is_symlink() and imported_outer.is_symlink()
        assert (imported_outer / "other" / "original.txt").read_text() == "other"
        assert (Path(str(imported_outer / "other") + ".data") / "original.txt").read_text() == "other.data"
        assert not Path(str(imported_outer / "other") + ".runtime").exists()
        assert selected.exists() is (outcome == "keep")
        assert data.exists() is (outcome == "keep")
    finally:
        applications.shutdown()
        await manager.close_session_owners()
        await copied.close()
