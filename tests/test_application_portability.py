"""Complete account transfer preserves unfinished application cleanup safely."""

import shutil
from pathlib import Path

import pytest

from server.applications import (
    ApplicationError,
    ApplicationManager,
    data_dir_for,
    runtime_dir_for,
)
from server.config import settings
from server.database import Database
from server.portability import export_account, import_account
from server.sessions import SessionManager
from tests.accounts import create_account
from tests.test_service_runtime import installation_settings


@pytest.mark.parametrize("phase", ["retiring", "files"])
async def test_export_import_preserves_remaining_application_files_and_explicit_retry(tmp_path, monkeypatch, phase):
    home = tmp_path / "source"
    home.mkdir()
    config, _ = installation_settings(home)
    for name, value in config.model_dump().items():
        monkeypatch.setattr(settings, name, value)
    database = Database(config.db_path)
    await database.initialize()
    owner, _ = await create_account(database)
    code = Path(config.users_root) / owner["id"] / "applications" / "partial"
    data, runtime = Path(data_dir_for(str(code))), Path(runtime_dir_for(str(code)))
    for directory in (code, data, runtime):
        directory.mkdir(parents=True)
        (directory / "original.txt").write_text(directory.name)
    await database.save_application(app_id="partial", name="Partial", app_dir=str(code),
                                    created_at="time", updated_at="time", user_id=owner["id"])
    manager = SessionManager()
    await manager.initialize(database)
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
        with pytest.raises(ApplicationError, match="delet"):
            await retry.request_build("partial", "must not restart a writer")
        await retry.delete_application("partial")
        assert await copied.get_application("partial") is None
        assert not restored_code.exists() and not Path(data_dir_for(str(restored_code))).exists()
    finally:
        retry.shutdown()
        await copied.close()
