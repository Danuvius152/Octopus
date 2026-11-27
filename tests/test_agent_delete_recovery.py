"""Recover the original Agent deletion across real SQLite and account boundaries."""

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from contextlib import closing
from pathlib import Path

import pytest

from server import agent_memory
from server.account_baseline import require_account_installation
from server.agent_manager import AgentDeleteRetry, AgentManager
from server.config import settings
from server.database import Database
from server.db.schema import CURRENT_VERSION
from server.portability import export_account, import_account
from server.scheduler import ScheduleRunner
from server.sessions.runtime_types import SessionAdmissionClosed
from server.storage_verification import verify_storage
from server.users import UserManager
from tests.accounts import create_account
from tests.agent_delete_support import agent_http
from tests.installation_settings import installation_settings
from tests.schedule_support import save_schedule, schedule_job


@pytest.fixture
async def retired_database(tmp_path, monkeypatch):
    config, config_file = installation_settings(tmp_path / "original")
    for name, value in config.model_dump().items():
        monkeypatch.setattr(settings, name, value)
    db = Database(config.db_path)
    await db.initialize()
    try:
        owner, token = await create_account(db)
        agent = await AgentManager(db).create_agent(name="Original agent", user_id=owner["id"])
        memory = agent_memory.agent_memory_dir(agent["id"], owner["id"])
        (memory / "MEMORY.md").write_text("Original private memory\n")
        await save_schedule(db, schedule_id="original-schedule", agent_id=agent["id"], name="Original schedule",
                            prompt="Must not run during cleanup", created_at="time", interval_seconds=3600)
    finally:
        await db.close()
    return config, config_file, owner["id"], agent["id"], token, memory


@pytest.mark.parametrize("fault", [
    "retirement-planned", "retirement-move", "retirement-replaced", "retirement-replaced-before", "retirement-setgid",
    "retirement-verified", "retirement-unlinked-proof", "retirement-removed",
    "pre-final", "rollback", "commit-before", "commit-after",
])
async def test_delete_restarts_from_actual_final_commit_facts(retired_database, tmp_path, fault):
    config, config_file, owner, agent_id, token, memory = retired_database
    proof = tmp_path / "delete-probe.json"
    proof.write_text(json.dumps({"owner": owner, "agent_id": agent_id, "token": token}))
    replaced_before = fault == "retirement-replaced-before"
    has_replacement = fault.startswith("retirement-replaced")
    at_original_address = replaced_before or fault == "retirement-planned"
    if fault == "retirement-setgid":
        memory.parents[1].chmod(0o2750)
    child = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", """
import asyncio, sys
from pathlib import Path
from tests.agent_delete_support import agent_delete_probe
asyncio.run(agent_delete_probe(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]))
""", str(config_file), str(proof), fault],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=12,
    )
    exits = {"pre-final": 71, "rollback": 0, "commit-before": 70, "commit-after": 70}
    assert child.returncode == exits.get(fault, 72), child.stderr
    assert proof.with_suffix(".files-cleared").exists()
    assert proof.with_suffix(".lost-commit").exists() is fault.startswith("commit-")
    assert not proof.with_suffix(".event").exists()
    if fault == "rollback":
        assert proof.with_suffix(".response").read_text() == "409"
    else:
        assert not proof.with_suffix(".response").exists()

    reopened = Database(config.db_path)
    await reopened.initialize()
    url = f"/api/agents/{agent_id}"
    try:
        async with agent_http(reopened, token) as (client, sessions):
            runner = ScheduleRunner(sessions, reopened)
            await runner.initialize(paused=True)
            try:
                assert memory.parent.exists() is at_original_address
                slot = memory.parents[1] / agent_memory.retirement_name(agent_id)
                assert slot.exists() is fault.startswith("retirement-")
                row = await reopened.get_agent(agent_id, owner)
                assert schedule_job(runner, "original-schedule") is None
                if fault == "commit-after":
                    assert row is None
                    assert (await client.delete(url)).status_code == 404
                else:
                    assert row["deletion_pending"] and row["archived"] and row["user_id"] == owner
                    assert (await reopened.get_schedule_definition("original-schedule"))["enabled"]
                    assert (await client.post(f"{url}/unarchive")).status_code == 409
                    assert memory.parent.exists() is at_original_address
                    with pytest.raises(SessionAdmissionClosed):
                        await sessions.create_session(agent_id)
                    if has_replacement:
                        replace = memory.parent if replaced_before else slot / "directory"
                        inode = int(proof.with_suffix(".replacement").read_text())
                        assert replace.stat().st_ino == inode
                        assert (await client.delete(url)).status_code == 409
                        assert replace.stat().st_ino == inode
                        assert memory.parent.exists() is replaced_before
                        assert (await reopened.get_agent(agent_id, owner))["deletion_pending"]
                        assert (await client.post(f"{url}/unarchive")).status_code == 409
                        assert schedule_job(runner, "original-schedule") is None
                    else:
                        assert (await client.delete(url)).status_code == 204
                if not has_replacement:
                    assert await reopened.get_agent(agent_id, owner) is None
                    assert not slot.exists()
            finally:
                await runner.shutdown()
    finally:
        await reopened.close()
    if has_replacement:
        archive = tmp_path / "unverified-retirement.zip"
        with pytest.raises(OSError, match="original directory proof"):
            export_account(config, owner, archive, home=tmp_path / "original")
        assert not archive.exists()


@pytest.mark.parametrize("transfer", ["archive", "backup"])
@pytest.mark.parametrize("retained", ["absent", "partial", "staged", "verified"])
async def test_account_transfer_retains_unfinished_agent_deletion(retired_database, tmp_path, monkeypatch, retained, transfer):
    config, _, owner, agent_id, _, memory = retired_database
    source = Database(config.db_path)
    await source.initialize()
    shared = memory.parents[1] / "shared-memory"
    shared.mkdir(exist_ok=True)
    (shared / "original.md").write_text("Shared account memory\n")
    (memory / "first.md").write_text("Already cleared\n")
    try:
        with monkeypatch.context() as patch:
            def stop_after_claim(*args):
                if retained == "partial":
                    (memory / "first.md").unlink()
                else:
                    shutil.rmtree(memory.parent)
                raise PermissionError("File cleanup remains unfinished")

            if retained in {"staged", "verified"}:
                move = agent_memory.directory_cleanup.move_unoccupied

                def interrupt_move(*args):
                    move(*args)
                    staged = args[-1] == "directory"
                    if (retained == "staged" and staged) or (retained == "verified" and args[-1].startswith("verified-")):
                        raise PermissionError("Final move acknowledgment lost")

                patch.setattr(agent_memory.directory_cleanup, "move_unoccupied", interrupt_move)
            else:
                patch.setattr(agent_memory, "remove_agent_dir", stop_after_claim)
            with pytest.raises(AgentDeleteRetry):
                await AgentManager(source).delete_agent(agent_id, user_id=owner)
    finally:
        await source.close()

    target_home = tmp_path / "relocated"
    if transfer == "archive":
        archive = tmp_path / "account.zip"
        export_account(config, owner, archive, home=tmp_path / "original")
        if retained == "staged":
            edited = tmp_path / "without-original-proof.zip"
            with zipfile.ZipFile(archive) as package, zipfile.ZipFile(edited, "w") as update:
                manifest = json.loads(package.read("manifest.json"))
                proof = next(name for name in manifest["entries"] if Path(name).name.startswith(".agent-delete-origin-"))
                del manifest["entries"][proof]
                for name in package.namelist():
                    if name not in {"manifest.json", "files/" + proof}:
                        update.writestr(name, package.read(name))
                update.writestr("manifest.json", json.dumps(manifest))
            with pytest.raises(OSError, match="original directory proof"):
                import_account(edited, home=tmp_path / "invalid-restoration")
            assert not (tmp_path / "invalid-restoration" / "main.db").exists()
        restored = import_account(archive, home=target_home)
    else:
        restored, _ = installation_settings(target_home)
        with closing(sqlite3.connect(Path(config.db_path).as_uri() + "?mode=ro", uri=True)) as original:
            with closing(sqlite3.connect(restored.db_path)) as backup:
                original.backup(backup)
        shutil.copytree(config.users_root, restored.users_root)
        shutil.copy2(config.master_key_file, restored.master_key_file)
    for name, value in restored.model_dump().items():
        monkeypatch.setattr(settings, name, value)
    require_account_installation(restored)
    assert verify_storage(restored)["main_format_version"] == CURRENT_VERSION
    db = Database(restored.db_path)
    await db.initialize()
    try:
        row = await db.get_agent(agent_id, owner)
        assert row["deletion_pending"] and row["archived"] and row["user_id"] == owner
        restored_memory = agent_memory.agent_memory_dir(agent_id, owner)
        restored_slot = restored_memory.parents[1] / agent_memory.retirement_name(agent_id)
        assert restored_memory.parent.exists() is (retained == "partial")
        assert restored_slot.exists() is (retained in {"staged", "verified"})
        if retained == "partial":
            assert (restored_memory / "MEMORY.md").read_text() == "Original private memory\n"
            assert not (restored_memory / "first.md").exists()
        token = await UserManager(db).issue_token(owner)
        async with agent_http(db, token) as (client, sessions):
            with pytest.raises(SessionAdmissionClosed):
                await sessions.create_session(agent_id)
            assert (await client.post(f"/api/agents/{agent_id}/unarchive")).status_code == 409
            assert (await client.delete(f"/api/agents/{agent_id}")).status_code == 204
        assert not restored_memory.parent.exists()
        assert not restored_slot.exists()
        assert await db.get_agent(agent_id) is None
        assert (restored_memory.parents[1] / "shared-memory" / "original.md").read_text() == "Shared account memory\n"
    finally:
        await db.close()


@pytest.mark.parametrize("mount_target", ["memory", "MEMORY.md"])
async def test_export_will_not_copy_a_mount_below_pending_agent(retired_database, tmp_path, monkeypatch, mount_target):
    config, _, owner, agent_id, _, memory = retired_database
    db = Database(config.db_path)
    await db.initialize()
    try:
        await db.claim_agent_deletion(agent_id, user_id=owner)
    finally:
        await db.close()
    target = memory if mount_target == "memory" else memory / mount_target
    mounted = target.stat()
    mount_id = agent_memory.directory_cleanup.mount_id

    def replacement_mount(descriptor):
        return "unexpected-bind-mount" if os.path.samestat(os.fstat(descriptor), mounted) else mount_id(descriptor)

    monkeypatch.setattr(agent_memory.directory_cleanup, "mount_id", replacement_mount)
    archive = tmp_path / "refused.zip"
    with pytest.raises(OSError, match="crosses a mount"):
        export_account(config, owner, archive, home=tmp_path / "original")
    assert not archive.exists()
    assert (memory / "MEMORY.md").read_text() == "Original private memory\n"


async def test_disabled_owner_between_claim_and_delete_keeps_pending_identity(retired_database, monkeypatch):
    config, _, owner, agent_id, token, memory = retired_database
    db = Database(config.db_path)
    await db.initialize()
    delete = db.delete_agent
    url = f"/api/agents/{agent_id}"
    try:
        async def disable_before_final(identifier, *, user_id):
            assert not memory.parent.exists()
            assert await db.set_user_disabled(user_id, at="2026-11-27T00:00:00Z")
            await delete(identifier, user_id=user_id)

        async with agent_http(db, token) as (client, sessions):
            events = []

            async def observe(event):
                if event["type"] == "agent_deleted":
                    events.append(event["agent_id"])

            sessions.on_broadcast("disabled-pending", observe)
            with monkeypatch.context() as patch:
                patch.setattr(db, "delete_agent", disable_before_final)
                assert (await client.delete(url)).status_code == 404
            assert not events
        row = await db.get_agent(agent_id, owner)
        assert row["deletion_pending"] and row["archived"]
        assert await db.set_user_disabled(owner, at=None)
        token = await UserManager(db).issue_token(owner)
        async with agent_http(db, token) as (client, _):
            assert (await client.delete(url)).status_code == 204
    finally:
        await db.close()


async def test_account_transfer_rejects_link_for_pending_agent(retired_database, tmp_path):
    config, _, owner, agent_id, _, memory = retired_database
    home = tmp_path / "original"
    db = Database(config.db_path)
    await db.initialize()
    try:
        await db.claim_agent_deletion(agent_id, user_id=owner)
    finally:
        await db.close()

    shared = memory.parents[1] / "shared-memory"
    shared.mkdir(exist_ok=True)
    (shared / "keep.md").write_text("Keep shared memory\n")
    archive = tmp_path / "original.zip"
    export_account(config, owner, archive, home=home)
    address = memory.parent.relative_to(home).as_posix()
    replacement = {"kind": "symlink", "target": shared.relative_to(home).as_posix()}
    edited = tmp_path / "edited.zip"
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(edited, "w") as target:
        manifest = json.loads(source.read("manifest.json"))
        entries = manifest["entries"]
        for path in tuple(entries):
            if path.startswith(address + "/"):
                del entries[path]
        entries[address] = replacement
        for name in source.namelist():
            if name != "manifest.json" and not name.startswith("files/" + address + "/"):
                target.writestr(name, source.read(name))
        target.writestr("manifest.json", json.dumps(manifest))
    with pytest.raises(ValueError, match="Unfinished Agent deletion addresses a non-directory"):
        import_account(edited, home=tmp_path / "bad-target")
    assert not (tmp_path / "bad-target").exists()

    omitted = tmp_path / "omitted.zip"
    account = memory.parents[2].relative_to(home).as_posix()
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(omitted, "w") as target:
        manifest = json.loads(source.read("manifest.json"))
        manifest["roots"].remove(account)
        manifest["entries"] = {
            name: entry for name, entry in manifest["entries"].items()
            if not (Path(name).is_relative_to(account) or Path(account).is_relative_to(name))
        }
        for name in source.namelist():
            if name != "manifest.json" and not name.startswith("files/" + account + "/"):
                target.writestr(name, source.read(name))
        target.writestr("manifest.json", json.dumps(manifest))
    occupied_home = tmp_path / "occupied"
    decoy = occupied_home / address / "local.md"
    decoy.parent.mkdir(parents=True)
    decoy.write_text("Keep existing target memory\n")
    with pytest.raises(ValueError, match="requires the account's directory as a published resource"):
        import_account(omitted, home=occupied_home)
    assert decoy.read_text() == "Keep existing target memory\n"
    assert not (occupied_home / Path(config.db_path).relative_to(home)).exists()

    shutil.rmtree(memory.parent)
    memory.parent.symlink_to(shared, target_is_directory=True)
    refused = tmp_path / "refused.zip"
    with pytest.raises(ValueError, match="Unfinished Agent deletion addresses a non-directory"):
        export_account(config, owner, refused, home=home)
    assert not refused.exists()
    assert (shared / "keep.md").read_text() == "Keep shared memory\n"
