"""Notifier receipts retain the real authoritative-storage exit boundary."""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tests.installation_settings import installation_settings

PROGRAM = r'''
import asyncio, json, os, sqlite3, sys
from pathlib import Path
from server.config import settings
config = Path(sys.argv[1])
for name, value in json.loads(config.read_text()).items():
    setattr(settings, name, value)
settings.master_key = "notifier-storage-test-master"
os.environ["CODEX_HOME"] = str(config.parent / "default-codex")
os.environ["CLAUDE_CONFIG_DIR"] = str(config.parent / "default-claude")
from httpx import ASGITransport, AsyncClient
from server import deps
from server.database import Database
from server.main import app
from server.notifiers import NotifierEvent, NotifierManager, WebhookNotifier
from server.routers import notifiers
from server.users import UserManager
phase = sys.argv[2]

def authorize(action, first, second, database, source):
    denied = (phase == "commit" and action == sqlite3.SQLITE_TRANSACTION and first == "COMMIT"
              or phase == "read" and action == sqlite3.SQLITE_READ and first == "notifiers")
    if denied:
        Path(sys.argv[4]).write_text(first)
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK

async def main():
    db = Database(settings.db_path)
    await db.initialize()
    users = UserManager(db)
    deps.set_user_manager(users)
    owner = await users.create_user(username="notifier-owner", password="password1")
    token = await users.issue_token(owner["id"])
    await db.save_notifier("target", "webhook", "Original", {"url": "https://local.test/hook"},
                           "now", user_id=owner["id"])
    manager = NotifierManager()
    manager.set_db(db)
    notifiers.set_db(db)
    update = db.update_notifier

    async def update_with_fault(*args, **kwargs):
        await db.conn.set_authorizer(authorize)
        return await update(*args, **kwargs)

    async def record_delivery(self, event):
        Path(sys.argv[5]).write_text(self.id)

    if phase == "commit":
        db.update_notifier = update_with_fault
    WebhookNotifier.send = record_delivery
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.patch("/api/notifiers/target",
            headers={"Authorization": f"Bearer {token}"}, json={"label": "Saved"})
        Path(sys.argv[3]).write_text(json.dumps({"status": response.status_code, "body": response.json()}))
    if phase == "read":
        await db.conn.set_authorizer(authorize)
        await manager.fire(NotifierEvent(type="session_idle", title="Idle", message="Done"), owner["id"])
    await db.close()

asyncio.run(main())
'''


@pytest.mark.parametrize("phase", ["commit", "read"])
def test_storage_fault_exits_at_the_notifier_transaction_boundary(tmp_path, phase):
    config, config_file = installation_settings(tmp_path / "installation")
    response = tmp_path / "response.json"
    fault = tmp_path / "fault"
    delivery = tmp_path / "delivered"
    result = subprocess.run(
        [sys.executable, "-c", PROGRAM, str(config_file), phase, str(response), str(fault), str(delivery)],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 70, result.stderr
    assert fault.read_text() == ("COMMIT" if phase == "commit" else "notifiers")
    assert not delivery.exists()
    if phase == "commit":
        assert not response.exists()
    else:
        receipt = json.loads(response.read_text())
        assert receipt["status"] == 200
        assert receipt["body"]["id"] == "target" and receipt["body"]["label"] == "Saved"
    with sqlite3.connect(f"file:{config.db_path}?mode=ro", uri=True) as database:
        row = database.execute("SELECT label FROM notifiers WHERE id = 'target'").fetchone()
    assert row == ("Saved" if phase == "read" else "Original",)
