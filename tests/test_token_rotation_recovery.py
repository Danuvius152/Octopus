"""A restart settles token files against the database's durable commit fact."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from dotenv import dotenv_values

from server import main
from server.config import settings
from server.crypto import decrypt, encrypt
from server.database import Database
from tests.test_service_shutdown import service as service

OLD = "old-rotation-key-20261020"
NEW = "new-rotation-key-20261020"


@pytest.mark.parametrize("explicit_source", [False, True])
def test_package_env_is_a_restart_source_only_when_settings_selects_it(tmp_path, explicit_source):
    source, working = tmp_path / "source", tmp_path / "working"
    shutil.copytree(Path(__file__).resolve().parents[1] / "server", source / "server",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    working.mkdir()
    (working / ".env").write_text("OCTOPUS_PORT=43211\n")
    package_env = source / ".env"
    package_env.write_text("OCTOPUS_AUTH_TOKEN=changeme\n")
    environment = {name: value for name, value in os.environ.items() if not name.lower().startswith("octopus_")}
    environment["PYTHONPATH"] = str(source)
    if explicit_source:
        environment["OCTOPUS_ENV_FILE"] = str(package_env)
    database_path = tmp_path / "rotation.db"
    program = """
import asyncio,json,sys
from server.config import settings
from server.crypto import encrypt
from server.database import Database
from server.token_rotation import TokenRotationError,rotate_auth_token
assert settings.auth_token=='changeme'
settings.master_key='test-restart-source-master'
async def main():
    database=Database(sys.argv[1]); await database.initialize()
    await database.save_credential('probe','codex','Probe','api_key',encrypt('retained secret',settings.auth_token),'2026-10-20')
    try:
        await rotate_auth_token(database,sys.argv[2])
    except TokenRotationError as error:
        status=error.status_code
    else:
        status=200
    await database.close()
    print(json.dumps({'status':status,'live_token':settings.auth_token}))
asyncio.run(main())
"""
    rotated = subprocess.run([sys.executable, "-c", program, str(database_path), NEW],
                             cwd=working, env=environment, capture_output=True, text=True, timeout=10)
    assert rotated.returncode == 0, rotated.stderr
    result = json.loads(rotated.stdout)
    loaded = subprocess.run([sys.executable, "-c", "from server.config import settings; print(settings.auth_token)"],
                            cwd=working, env=environment, capture_output=True, text=True, timeout=5)
    assert loaded.returncode == 0, loaded.stderr
    assert result["status"] == (200 if explicit_source else 409), "a package mirror cannot prove Settings will load it"
    expected = NEW if explicit_source else "changeme"
    assert loaded.stdout.strip() == result["live_token"] == expected
    assert dotenv_values(package_env)["OCTOPUS_AUTH_TOKEN"] == expected
    with sqlite3.connect(database_path) as retained:
        assert retained.execute("SELECT id FROM token_rotation_intent").fetchall() == []
        ciphertext = retained.execute("SELECT secret_encrypted FROM credential_secrets WHERE credential_id='probe'").fetchone()[0]
        assert decrypt(ciphertext, expected) == "retained secret"


@pytest.mark.parametrize("crash_after", ["intent", "files", "before_commit", "database", "commit_error", "commit_cancel", "sql", "cleanup", "ancestor", "intent_owner_cancel", "database_owner_cancel"])
@pytest.mark.parametrize("has_secret", [False, True])
async def test_startup_reconciles_a_crashed_rotation_without_replaying_secret_changes(
    service, tmp_path, monkeypatch, crash_after, has_secret,
):
    environment = tmp_path / "active.env"
    environment.write_text(f"OCTOPUS_AUTH_TOKEN={OLD}\nOCTOPUS_PORT=43211\n")
    program = """
import asyncio,os,sys
from pathlib import Path
os.environ['OCTOPUS_ENV_FILE']=sys.argv[2]
os.environ.pop('OCTOPUS_AUTH_TOKEN',None)
from server.config import settings
from server.crypto import encrypt,decrypt
from server.database import Database
from server import token_rotation
from types import SimpleNamespace
old,new=sys.argv[4:6]
settings.db_path=sys.argv[1]
settings.master_key_file=sys.argv[3]
settings.auth_token=old

async def main():
    database=Database(sys.argv[1])
    await database.initialize()
    agent=await database.get_system_agent()
    await database.save_session('rotation-session','Transcript retained',str(Path(sys.argv[1]).parent),'2026-10-20',agent_id=agent['id'])
    await database.append_message('rotation-session',0,'assistant','text','Before rotation')
    if sys.argv[7]=='yes':
        await database.save_credential('rotation-probe','codex','probe','api_key',encrypt('retained secret',old),'2026-10-20')
    commit=database.conn.commit
    execute=database.conn.execute
    intent_written=c1_started=owner_cancelled=False
    async def observe(sql,parameters=()):
        nonlocal intent_written,c1_started
        cursor=await execute(sql,parameters)
        if sql.startswith('INSERT INTO token_rotation_intent'): intent_written=True
        if sql.startswith('DELETE FROM token_rotation_intent'): c1_started=True
        if sys.argv[6]=='sql' and (sql.startswith('UPDATE credential_secrets ') or (sys.argv[7]=='no' and c1_started)):
            raise OSError('SQL receipt lost')
        return cursor
    async def stop_at_commit():
        nonlocal owner_cancelled
        if c1_started and sys.argv[6]=='before_commit': raise OSError('commit did not run')
        await commit()
        if intent_written and not c1_started and sys.argv[6]=='intent': os._exit(71)
        owner_cancel=(sys.argv[6]=='intent_owner_cancel' and intent_written and not c1_started) or (sys.argv[6]=='database_owner_cancel' and c1_started)
        if owner_cancel and not owner_cancelled:
            owner_cancelled=True
            Path(sys.argv[1]+'.owner-cancelled').write_text('normal commit receipt')
            database._write_owner.cancel('command owner cancelled after actual commit')
        if c1_started:
            if sys.argv[6]=='database': os._exit(71)
            if sys.argv[6]=='commit_error': raise OSError('commit receipt lost')
            if sys.argv[6]=='commit_cancel': raise asyncio.CancelledError('commit driver cancelled')
    database.conn.execute=observe
    database.conn.commit=stop_at_commit
    fsync=os.fsync
    def fail_ancestor(descriptor):
        if intent_written and sys.argv[6]=='ancestor' and Path(os.readlink('/proc/self/fd/'+str(descriptor)))==Path(sys.argv[2]).parent.parent:
            raise OSError('token ancestor directory sync failed')
        return fsync(descriptor)
    os.fsync=fail_ancestor
    if sys.argv[6]=='files':
        replace=os.replace
        def stop_after_files(source,destination):
            replace(source,destination)
            if Path(destination)==Path(sys.argv[2]): os._exit(71)
        os.replace=stop_after_files
    async def stop_cleanup(): raise RuntimeError('old CLI retirement unconfirmed')
    manager=SimpleNamespace(stop_all_held_processes=stop_cleanup) if sys.argv[6]=='cleanup' else None
    try:
        await token_rotation.rotate_auth_token(database,new,session_mgr=manager)
    except asyncio.CancelledError:
        from server.installation_keys import secret_key_scope
        async with secret_key_scope(database,None) as key:
            assert key==old
            Path(sys.argv[1]+'.admitted-after-cancel').write_text('old key scope reopened')
        await database.close()
        return
    await database.close()
    raise AssertionError('the injected crash was not reached')

asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", program, settings.db_path, str(environment), settings.master_key_file,
         OLD, NEW, crash_after, "yes" if has_secret else "no"],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10,
    )
    if crash_after.endswith("owner_cancel"):
        assert Path(settings.db_path + ".owner-cancelled").read_text() == "normal commit receipt"
        assert not Path(settings.db_path + ".admitted-after-cancel").exists(), "cancelling the command reopened the old key scope"
    assert result.returncode == (71 if crash_after in {"intent", "files", "database"} else 70), result.stderr
    with sqlite3.connect(Path(settings.db_path).as_uri() + "?mode=ro", uri=True) as before:
        stored = before.execute("SELECT secret_encrypted FROM credential_secrets WHERE credential_id='rotation-probe'").fetchone()
    assert dotenv_values(environment, interpolate=False)["OCTOPUS_AUTH_TOKEN"] == (OLD if crash_after in {"intent", "intent_owner_cancel"} else NEW)
    # A later configuration edit is independent of the token protocol.
    environment.write_text(environment.read_text().replace("43211", "43212") + "OCTOPUS_USER_LABEL=later-edit\n")
    monkeypatch.setenv("OCTOPUS_ENV_FILE", str(environment))
    monkeypatch.delenv("OCTOPUS_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(settings, "auth_token", NEW)
    expected = NEW if crash_after in {"database", "commit_error", "commit_cancel", "cleanup", "database_owner_cancel"} else OLD
    async with main.app.router.lifespan_context(main.app):
        assert settings.auth_token == expected
        assert dotenv_values(environment, interpolate=False)["OCTOPUS_AUTH_TOKEN"] == expected
        assert "OCTOPUS_PORT=43212" in environment.read_text()
        assert "OCTOPUS_USER_LABEL=later-edit" in environment.read_text()
        credential = await service.db.get_credential("rotation-probe")
        if has_secret:
            assert decrypt(credential["secret_encrypted"], expected) == "retained secret"
            assert credential["secret_encrypted"] == stored[0], "startup must never re-encrypt the secret again"
        else:
            assert credential is None and stored is None
        await service.db.append_message("rotation-session", 1, "assistant", "text", "After restart")
        assert [row["content"] for row in await service.db.load_messages("rotation-session")] == ["Before rotation", "After restart"]


@pytest.mark.parametrize("fault", ["version", "database", "third_token", "source"])
async def test_unprovable_prepared_rotation_preserves_evidence_and_never_admits_work(
    service, tmp_path, fault,
):
    environment = tmp_path / "active.env"
    environment.write_text(f"OCTOPUS_AUTH_TOKEN={OLD}\n")
    prepare = """
import asyncio,os,sys
from pathlib import Path
os.environ.pop('OCTOPUS_AUTH_TOKEN',None)
os.environ['OCTOPUS_ENV_FILE']=sys.argv[2]
from server.config import settings
settings.db_path=sys.argv[1]
settings.master_key_file=sys.argv[3]
from server.database import Database
from server.crypto import encrypt
from server.token_rotation import rotate_auth_token
async def main():
    database=Database(sys.argv[1]); await database.initialize()
    await database.save_credential('probe','claude-code','Probe','api_key',encrypt('original secret',settings.auth_token),'2026-10-20')
    replace=os.replace
    def crash(source,destination):
        replace(source,destination)
        if Path(destination)==Path(sys.argv[2]): os._exit(71)
    os.replace=crash
    await rotate_auth_token(database,sys.argv[4])
asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", prepare, settings.db_path, str(environment), settings.master_key_file, NEW],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 71, result.stderr
    database = Database(settings.db_path)
    await database.initialize()
    try:
        async with database.transaction() as tx:
            payload = (await tx.fetch_one("SELECT encrypted_payload FROM token_rotation_intent WHERE id=1"))[0]
            if fault in {"version", "database"}:
                import json

                key = Path(settings.master_key_file).read_text().strip()
                document = json.loads(decrypt(payload, key))
                document["version" if fault == "version" else "database"] = 99 if fault == "version" else str(tmp_path / "another.db")
                payload = encrypt(json.dumps(document), key)
                await tx.execute("UPDATE token_rotation_intent SET encrypted_payload=? WHERE id=1", (payload,))
        original_secret = (await database.get_credential("probe"))["secret_encrypted"]
    finally:
        await database.close()
    startup_environment = environment
    if fault == "third_token":
        environment.write_text("OCTOPUS_AUTH_TOKEN=unrelated-third-token\n")
    elif fault == "source":
        startup_environment = tmp_path / "different-startup.env"
        startup_environment.write_text(f"OCTOPUS_AUTH_TOKEN={NEW}\n")
    retained_file = environment.read_bytes()
    startup = """
import asyncio,os,sys
from pathlib import Path
os.environ.pop('OCTOPUS_AUTH_TOKEN',None)
os.environ['OCTOPUS_ENV_FILE']=sys.argv[2]
from server.config import settings
root=Path(sys.argv[1]).parent
for name in ('agents_dir','applications_dir','users_root','wiki_dir','research_dir','large_prompts_dir','attachments_dir','default_working_dir'):
    setattr(settings,name,str(root/name))
settings.db_path=sys.argv[1]
settings.metrics_db_path=str(root/'metrics.db')
settings.master_key_file=sys.argv[3]
settings.enable_tunnel=False
from server.main import app
async def main():
    async with app.router.lifespan_context(app): (root/'admitted').touch()
asyncio.run(main())
"""
    restarted = subprocess.run(
        [sys.executable, "-c", startup, settings.db_path, str(startup_environment), settings.master_key_file],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10,
    )
    assert restarted.returncode == 70, restarted.stderr
    assert not (tmp_path / "admitted").exists()
    assert environment.read_bytes() == retained_file
    with sqlite3.connect(Path(settings.db_path).as_uri() + "?mode=ro", uri=True) as retained:
        assert retained.execute("SELECT encrypted_payload FROM token_rotation_intent WHERE id=1").fetchone()[0] == payload
        assert retained.execute("SELECT secret_encrypted FROM credential_secrets WHERE credential_id='probe'").fetchone()[0] == original_secret


@pytest.mark.parametrize("crash", ["first_file", "second_file", "during_recovery"])
async def test_partial_multi_file_rotation_and_interrupted_recovery_share_one_original_key(
    service, tmp_path, crash,
):
    # Two real fallback sources, both under the test root. Copy the package so
    # its fallback can never point at the developer's repository environment.
    source = tmp_path / "source"
    shutil.copytree(Path(__file__).resolve().parents[1] / "server", source / "server",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    working = tmp_path / "working"
    working.mkdir()
    files = (working / ".env", source / ".env")
    for path in files:
        path.write_text(f"OCTOPUS_AUTH_TOKEN={OLD}\n")
    environment = dict(os.environ, PYTHONPATH=str(source))
    common = """
import asyncio,os,sys
from pathlib import Path
os.environ.pop('OCTOPUS_AUTH_TOKEN',None)
os.environ.pop('OCTOPUS_ENV_FILE',None)
from server.config import settings
root=Path(sys.argv[1]).parent
settings.db_path=sys.argv[1]
settings.master_key_file=sys.argv[2]
settings.metrics_db_path=str(root/'metrics.db')
settings.enable_tunnel=False
for name in ('agents_dir','applications_dir','users_root','wiki_dir','research_dir','large_prompts_dir','attachments_dir','default_working_dir'):
    setattr(settings,name,str(root/name))
"""
    prepare = common + """
from server.database import Database
from server.crypto import encrypt
from server.token_rotation import rotate_auth_token
async def main():
    database=Database(settings.db_path); await database.initialize()
    await database.save_credential('probe','claude-code','Probe','api_key',encrypt('retained secret',settings.auth_token),'2026-10-20')
    replace=os.replace
    count=0
    def crash(source,destination):
        nonlocal count
        replace(source,destination)
        if Path(destination).name=='.env':
            count+=1
            if count==int(sys.argv[4]): os._exit(71)
    os.replace=crash
    await rotate_auth_token(database,sys.argv[3])
    await database.close()
    raise AssertionError('file interruption did not run')
asyncio.run(main())
"""
    arguments = [settings.db_path, settings.master_key_file, NEW, "1" if crash == "first_file" else "2"]
    result = subprocess.run([sys.executable, "-c", prepare, *arguments], cwd=working, env=environment,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 71, result.stderr
    assert [dotenv_values(path)["OCTOPUS_AUTH_TOKEN"] for path in files] == [NEW, OLD if crash == "first_file" else NEW]
    for path in files:
        path.write_text(path.read_text() + "OCTOPUS_USER_LABEL=independent-edit\n")
    restart = common + """
if sys.argv[3]=='interrupt':
    replace=os.replace
    def crash(source,destination):
        replace(source,destination)
        if Path(destination).name=='.env': os._exit(72)
    os.replace=crash
from server.main import app
async def main():
    async with app.router.lifespan_context(app): (root/'admitted').write_text(settings.auth_token)
asyncio.run(main())
"""
    if crash == "during_recovery":
        interrupted = subprocess.run([sys.executable, "-c", restart, settings.db_path, settings.master_key_file, "interrupt"],
                                     cwd=working, env=environment, capture_output=True, text=True, timeout=10)
        assert interrupted.returncode == 72, interrupted.stderr
        assert not (tmp_path / "admitted").exists()
    restored = subprocess.run([sys.executable, "-c", restart, settings.db_path, settings.master_key_file, "finish"],
                              cwd=working, env=environment, capture_output=True, text=True, timeout=10)
    assert restored.returncode == 0, restored.stderr
    assert (tmp_path / "admitted").read_text() == OLD
    assert all(dotenv_values(path)["OCTOPUS_AUTH_TOKEN"] == OLD for path in files)
    assert all("OCTOPUS_USER_LABEL=independent-edit" in path.read_text() for path in files)
    with sqlite3.connect(Path(settings.db_path).as_uri() + "?mode=ro", uri=True) as retained:
        assert retained.execute("SELECT id FROM token_rotation_intent").fetchall() == []
        ciphertext = retained.execute("SELECT secret_encrypted FROM credential_secrets WHERE credential_id='probe'").fetchone()[0]
        assert decrypt(ciphertext, OLD) == "retained secret"
