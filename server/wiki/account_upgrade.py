"""Recoverable first-account adoption of the existing Wiki state."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from collections.abc import Iterable
from pathlib import Path

from ..aio import complete_cleanup
from ..config import settings
from ..service_installation import transfer_vault
from .history import atomic
from .registry import registry
from .storage import connect, file_effect
from .store import WikiError


def journal_path() -> Path:
    return Path(settings.users_root).expanduser() / ".first-account-upgrade.json"


def pending_upgrade(*, include_complete=False) -> dict | None:
    path = journal_path()
    if not path.exists():
        return None
    try:
        journal = json.loads(path.read_text())
        if not isinstance(journal, dict) or journal.get("version") != 1:
            raise ValueError("unsupported migration journal")
        if journal.get("phase") not in {"preparing", "prepared", "account_created", "publishing", "published", "reopening", "activating", "complete"}:
            raise ValueError("unknown migration phase")
        if journal["phase"] == "complete" and not include_complete:
            return None
        identifier = journal.get("upgrade_id")
        if not isinstance(identifier, str) or uuid.UUID(identifier).hex != identifier:
            raise ValueError("invalid migration identity")
        expected_stage = path.parent / f".wiki-adoption-{identifier}"
        if Path(journal.get("stage", "")).resolve() != expected_stage.resolve():
            raise ValueError("staging path does not match migration identity")
        if Path(journal.get("source", "")).resolve() != registry.state(None).resolve():
            raise ValueError("legacy state path differs from migration record")
        if not isinstance(journal.get("username"), str) or not journal["username"]:
            raise ValueError("migration username is missing")
        if not isinstance(journal.get("adopt"), bool):
            raise ValueError("migration adoption choice is missing")
        if journal.get("user_id") is not None and not isinstance(journal["user_id"], str):
            raise ValueError("invalid migration account binding")
        config = journal.get("config")
        if not isinstance(config, dict) or not isinstance(config.get("root"), str) or not Path(config["root"]).is_absolute():
            raise ValueError("invalid recorded vault root")
        uuid.UUID(config["vault_id"])
        if not isinstance(config.get("curator_agent_id"), str) or not config["curator_agent_id"]:
            raise ValueError("missing recorded curator binding")
        return journal
    except (ValueError, OSError, TypeError, KeyError, AttributeError) as exc:
        raise WikiError("Account migration journal needs operator verification", 503) from exc


def _sync_directories(directories: Iterable[Path]) -> None:
    with file_effect(registry.health):
        for directory in directories:
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


def save(journal: dict) -> None:
    path = journal_path()
    with file_effect(registry.health):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic(path, json.dumps(journal, ensure_ascii=False, sort_keys=True).encode())
        path.chmod(0o600)
    parent = path.parent.resolve()
    _sync_directories((parent, *parent.parents))


def subscriber_digest(subscriber):
    """Original immutable coordinates, shared by adoption and receive replay."""
    coordinates = [subscriber[key] for key in ("job_id", "owner", "kind", "target", "request_id", "client_id")]
    return hashlib.sha256(json.dumps(coordinates, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def adopts_notice(user_id, vault_id, delivery):
    """Only the recorded first-account transfer can interpret a legacy owner.

    This does not rewrite subscriber/event identities or authorize a target.
    The receiver still verifies the original session/app's current ownership.
    """
    if not user_id or delivery["owner"] != "legacy":
        return False
    journal = pending_upgrade(include_complete=True)
    if (journal is None or not journal["adopt"] or journal["user_id"] != user_id
            or journal["phase"] not in {"activating", "complete"} or journal["config"]["vault_id"] != vault_id
            or journal.get("notice_subscribers", {}).get(delivery["subscriber_id"]) != subscriber_digest(delivery)):
        return False
    marker = registry.state(user_id) / ".upgrade-owner.json"
    try:
        return (not marker.is_symlink() and json.loads(marker.read_text())["upgrade_id"] == journal["upgrade_id"]
                and registry.config(user_id)["vault_id"] == vault_id)
    except (OSError, ValueError, KeyError):
        return False


def _durable_digest(path: Path) -> dict:
    if not path.is_file():
        return {}
    from .maintenance_inputs import TABLES as maintenance_derived

    derived = {"documents", "search_documents", "search_sections", "search_fts", "raw_materials", "index_state", "watch_directories"} | maintenance_derived.keys()
    with connect(path, registry.health, write=False) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            registry.health.fail(RuntimeError("Wiki state integrity verification failed"))
        tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        result = {}
        for table in tables:
            if table in derived or table.startswith(("search_fts_", "sqlite_")):
                continue
            quoted = '"' + table.replace('"', '""') + '"'
            rows = sorted(repr(tuple(row)) for row in db.execute(f"SELECT * FROM {quoted}"))  # noqa: S608
            result[table] = {"rows": len(rows), "sha256": hashlib.sha256("\n".join(rows).encode()).hexdigest()}
        return result


def _manifest(state: Path) -> dict[str, str]:
    ignored = {"index.sqlite3", "index.sqlite3-wal", "index.sqlite3-shm", "index.sqlite3-journal",
               "config.lock", "vault.lock", ".upgrade-owner.json"}
    return {str(path.relative_to(state)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(state.rglob("*")) if path.is_file() and path.name not in ignored}


def prepare(journal: dict) -> None:
    source = Path(journal["source"])
    stage = Path(journal["stage"])
    if stage.exists():
        marker = stage / ".upgrade-owner.json"
        if not marker.is_file() or json.loads(marker.read_text()).get("upgrade_id") != journal["upgrade_id"]:
            raise WikiError("Migration staging directory is not owned by this upgrade; retained", 409)
        with file_effect(registry.health):
            shutil.rmtree(stage)
    total = 0
    for path in source.rglob("*"):
        if path.is_symlink():
            raise WikiError("Wiki state contains symlinks; migration requires verification", 409)
        if path.is_file():
            total += path.stat().st_size
    if shutil.disk_usage(stage.parent).free < total * 2 + 1024 * 1024:
        raise WikiError("Insufficient space to prepare a verified Wiki migration", 507)
    with file_effect(registry.health):
        stage.mkdir(mode=0o700)
        atomic(stage / ".upgrade-owner.json", json.dumps({"upgrade_id": journal["upgrade_id"]}).encode())
        shutil.copytree(source, stage, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("index.sqlite3", "index.sqlite3-wal", "index.sqlite3-shm", "index.sqlite3-journal"))
    original = source / "index.sqlite3"
    if original.is_file():
        with connect(original, registry.health, write=False) as before:
            with connect(stage / "index.sqlite3", registry.health, create=True) as after:
                before.backup(after)
    journal["durable"] = _durable_digest(stage / "index.sqlite3")
    if _durable_digest(original) != journal["durable"]:
        raise WikiError("Wiki state changed during migration preparation", 503)
    journal["files"] = _manifest(source)
    if _manifest(stage) != journal["files"]:
        raise WikiError("Wiki history and snapshot file verification failed", 503)
    journal["notice_subscribers"] = {}
    with connect(stage / "index.sqlite3", registry.health, write=False) as db:
        db.row_factory = sqlite3.Row
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='wiki_job_subscribers'").fetchone():
            from . import job_events

            job_events.require(db)
            journal["notice_subscribers"] = {row["id"]: subscriber_digest(row) for row in
                                              db.execute("SELECT * FROM wiki_job_subscribers WHERE owner='legacy'")}
    staged_paths = [stage, *stage.rglob("*")]
    with file_effect(registry.health):
        for path in staged_paths:
            path.chmod(0o700 if path.is_dir() else 0o600)
            if path.is_file():
                with path.open("rb") as handle:
                    os.fsync(handle.fileno())
    directories = sorted((path for path in staged_paths if path.is_dir()), key=lambda path: len(path.parts), reverse=True)
    _sync_directories(directories)
    journal["phase"] = "prepared"
    save(journal)


def publish(journal: dict) -> None:
    destination = registry.state(journal["user_id"])
    stage = Path(journal["stage"])
    if destination.exists():
        if journal["phase"] not in {"publishing", "published", "reopening", "activating"}:
            raise WikiError("Target account already has Wiki state; migration will not overwrite it", 409)
        config = json.loads((destination / "config.json").read_text())
        durable = _durable_digest(destination / "index.sqlite3")
        if config != journal["config"] or (journal["phase"] != "activating" and durable != journal["durable"]):
            raise WikiError("Published Wiki state differs from the migration record", 409)
        if journal["phase"] != "activating" and _manifest(destination) != journal["files"]:
            raise WikiError("Published Wiki history and snapshot files differ from the migration record", 409)
    else:
        journal["phase"] = "publishing"
        save(journal)
        with file_effect(registry.health):
            os.replace(stage, destination)
    # A previous rename can be visible without its directory-sync receipt.
    # Re-entry must confirm the same namespace before publishing ownership.
    _sync_directories((destination.parent, *destination.parent.parents))
    journal["phase"] = "published"
    save(journal)
    source = Path(journal["source"])
    transfer_vault(settings, Path(journal["config"]["root"]), source, destination, health=registry.health)
    old_config = source / "config.json"
    if old_config.exists():
        with file_effect(registry.health):
            os.replace(old_config, source / "config.legacy-adopted.json")
    _sync_directories((source,))


async def _io(function, *args):
    return await complete_cleanup(asyncio.to_thread(function, *args))


async def upgrade_with_wiki(db, *, username, password, adopt, manager, user=None):
    from ..app_backends import backend_supervisor
    from ..bootstrap import BootstrapError, _bootstrap_account
    from ..users import validate_password, validate_username
    from . import app_bridge
    from .jobs import worker

    journal = pending_upgrade()
    if user is not None:
        if journal is not None and journal.get("user_id") is None and journal.get("username") == user["username"]:
            journal["user_id"] = user["id"]
            await _io(save, journal)
        if journal is None or journal.get("user_id") != user["id"]:
            raise BootstrapError("No pending upgrade belongs to this account", status_code=404)
        adopt = journal["adopt"]
    else:
        try:
            username = validate_username(username)
            validate_password(password, username=username)
        except Exception as exc:
            raise BootstrapError(str(exc)) from exc
        config = registry.config(None)
        if await db.get_agent(config["curator_agent_id"]) is None:
            raise BootstrapError("Wiki curator binding is missing", status_code=409)
        if config.get("app_id") and await db.get_application(config["app_id"]) is None:
            raise BootstrapError("Wiki application binding is missing", status_code=409)
        if journal is not None and journal.get("user_id"):
            raise BootstrapError("Sign in to the migration owner and resume the pending upgrade", status_code=409)
        root = journal_path().parent
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if journal is not None:
            if (journal["username"], journal["adopt"], journal["config"]) != (username, adopt, config):
                raise BootstrapError("A different account upgrade is pending; retry its original request", status_code=409)
        else:
            upgrade_id = uuid.uuid4().hex
            journal = {"version": 1, "upgrade_id": upgrade_id, "phase": "preparing", "user_id": None, "username": username,
                       "adopt": adopt, "source": str(registry.state(None).resolve()),
                       "stage": str(root / f".wiki-adoption-{upgrade_id}"), "config": config}
        save(journal)

    config = journal["config"]
    owner = journal.get("user_id")
    running_worker = worker.manager is not None
    was_registry_running = registry._task is not None
    app_id = config.get("app_id")
    if manager is not None:
        manager._legacy_transition = True
    registry.begin_transition(None)
    if owner:
        registry.begin_transition(owner)
    if app_id:
        backend_supervisor._blocked_apps.add(app_id)
    drained = False
    try:
        await worker.stop()
        if manager is not None:
            await manager.retire_ownerless_runs()
        if app_id:
            await backend_supervisor.stop(app_id)
        await app_bridge.shutdown(user_id=None)
        await registry.stop()
        await _io(registry.block, None)
        drained = True
        if journal["phase"] == "preparing" and adopt:
            await _io(prepare, journal)

        async def created(created_user):
            journal["user_id"] = created_user["id"]
            registry.begin_transition(created_user["id"])
            # Preserve publication phase on a recovery after a lost response.
            if journal["phase"] in {"prepared", "preparing"}:
                journal["phase"] = "account_created"
            await _io(save, journal)

        summary = await _bootstrap_account(
            db, username=username, password=password, adopt=adopt, session_manager=manager,
            existing_user=user, on_created=created, wiki_root=config["root"] if adopt else None,
        )
        if adopt:
            await _io(publish, journal)
            journal["phase"] = "reopening"
            await _io(save, journal)
            await _io(registry.block, journal["user_id"])
            await _io(registry.reopen_for_upgrade, journal["user_id"], journal)
            summary["wiki"] = {"adopted": True, "vault_id": config["vault_id"], "durable": journal["durable"]}
        else:
            summary["wiki"] = {"adopted": False}
        journal["phase"] = "activating"
        journal["summary"] = summary
        await _io(save, journal)
        registry.legacy_enabled = False
        if not adopt:
            await _io(registry.block, journal["user_id"])
        registry.unblock(journal["user_id"])
        registry.resume_access()
        if manager is not None:
            manager._legacy_transition = False
        if app_id:
            backend_supervisor._blocked_apps.discard(app_id)
        app_bridge.reopen(user_id=None)
        if was_registry_running:
            await registry.start()
        if running_worker and manager is not None:
            await worker.start(manager)
        journal["phase"] = "complete"
        await _io(save, journal)
        return summary
    except BaseException as exc:
        retained = user or await db.get_user_by_username(journal["username"])
        if retained:
            journal["user_id"] = retained["id"]
            await _io(save, journal)
            registry.legacy_enabled = False
            registry.begin_transition(retained["id"])
            if manager is not None:
                manager._legacy_transition = True
            if app_id:
                backend_supervisor._blocked_apps.add(app_id)
            raise BootstrapError("Account upgrade is incomplete; sign in to your new account and open Account to resume. "
                                 f"Phase: {journal['phase']}. State retained.", status_code=503) from exc
        if not drained:
            raise BootstrapError("Account transition cleanup is unconfirmed; state retained. Retry the upgrade after its writers finish.",
                                 status_code=503) from exc
        if journal["phase"] != "preparing":
            # prepared may already be visible even when save's final fsync
            # failed. Reopening writers would invalidate the retained staging
            # that the same command will reuse on retry.
            raise BootstrapError("Account preparation is retained; Wiki writes remain stopped. "
                                 "Retry the original bootstrap request to confirm preparation and continue.",
                                 status_code=503) from exc
        registry.unblock(None)
        registry.resume_access()
        if manager is not None:
            manager._legacy_transition = False
        if app_id:
            backend_supervisor._blocked_apps.discard(app_id)
        app_bridge.reopen(user_id=None)
        if was_registry_running:
            await registry.start()
        if running_worker and manager is not None:
            await worker.start(manager)
        raise


async def resume_upgrade(db, user, manager):
    from ..bootstrap import BootstrapError
    async with db._bootstrap_lock:
        if pending_upgrade() is None:
            raise BootstrapError("No pending account upgrade", status_code=404)
        return await complete_cleanup(upgrade_with_wiki(
            db, username=user["username"], password="", adopt=True, manager=manager, user=user,
        ))
