"""The public ASGI lifespan owns storage before recovering or producing work."""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from server import main
from server.config import settings
from server.database import Database
from server.knowledge import encode, for_account
from server.service_installation import InstallationError, InstallationOwner
from server.wiki.registry import registry
from tests import test_api as api
from tests.accounts import create_account
from tests.test_service_shutdown import service as service
from tests.test_service_shutdown import synthetic_cli

client = api.client


async def test_direct_asgi_refuses_a_second_owner_before_creating_the_database(service):
    owner = InstallationOwner(settings)
    owner.acquire()
    try:
        with pytest.raises(InstallationError, match="already owned"):
            async with main.app.router.lifespan_context(main.app):
                pass
        assert not Path(settings.db_path).exists()
    finally:
        owner.close()


async def test_readiness_exposes_recovery_and_shutdown_without_claiming_a_dead_process(client, monkeypatch):
    from server.service_health import ServiceHealth

    health = ServiceHealth()
    monkeypatch.setattr(api.app.state, "service_health", health, raising=False)
    assert (await client.get("/health")).status_code == 200
    pending = await client.get("/ready")
    assert pending.status_code == 503 and pending.json()["status"] == "starting"
    health.open_admission()
    ready = await client.get("/ready")
    assert ready.status_code == 200 and ready.json()["status"] == "ready"
    health.begin_shutdown()
    stopped = await client.get("/ready")
    assert stopped.status_code == 503 and stopped.json()["status"] == "stopping"
    assert (await client.get("/health")).status_code == 200


async def test_due_schedules_wait_for_the_service_recovery_gate(service, tmp_path, monkeypatch):
    starts = []
    synthetic_cli(monkeypatch, starts)
    db = Database(settings.db_path)
    await db.initialize()
    user, _ = await create_account(db)
    agent = await db.get_system_agent()
    await db.update_agent(agent["id"], backend="codex")
    now = datetime.now(UTC)
    context = for_account(session_id="schedule-entry", request_id="due-at-startup",
                          user_id=user["id"], prompt="Run after recovery")
    await db.save_schedule(
        schedule_id="due-at-startup", agent_id=agent["id"], name="Startup gate",
        prompt="Run after recovery", created_at=now.isoformat(),
        run_at=(now + timedelta(seconds=.3)).isoformat(), knowledge_context=encode(context),
    )
    await db.close()
    # Keep a real startup storage operation pending. The application must not
    # dispatch a due fire while any later initialization is still blocked.
    lock = sqlite3.connect(settings.metrics_db_path)
    lock.execute("BEGIN EXCLUSIVE")
    entered, stop = asyncio.Event(), asyncio.Event()

    async def serve():
        async with main.app.router.lifespan_context(main.app):
            entered.set()
            await stop.wait()

    owner = asyncio.create_task(serve())
    try:
        await asyncio.sleep(.7)
        assert not entered.is_set(), "the storage lock did not hold startup"
        assert starts == [], "a schedule dispatched before startup recovery completed"
        lock.rollback()
        await asyncio.wait_for(entered.wait(), 5)
        async with asyncio.timeout(5):
            while not starts:
                await asyncio.sleep(.01)
        assert len(starts) == 1
    finally:
        lock.close()
        stop.set()
        await owner


@pytest.mark.parametrize("config", ['{"invalid":', '{"root":"/unavailable-test-vault","curator_agent_id":"curator","vault_id":"00000000-0000-0000-0000-000000000001"}'])
async def test_invalid_wiki_registration_keeps_the_workbench_and_diagnostic_endpoint_available(service, config):
    db = Database(settings.db_path)
    await db.initialize()
    user, bearer = await create_account(db)
    await db.close()
    path = registry.state(user["id"]) / "config.json"
    path.parent.mkdir(parents=True)
    path.write_text(config)
    main.app.add_api_route("/ready", main.readiness)
    main.app.include_router(main.wiki_router.router)
    main.app.add_exception_handler(main.WikiError, main._wiki_error)
    async with main.app.router.lifespan_context(main.app):
        async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as client:
            assert (await client.get("/ready")).status_code == 200
            diagnostic = await client.get("/api/wiki/config", headers={"Authorization": f"Bearer {bearer}"})
            assert diagnostic.status_code == 503
            assert "registration is invalid" in diagnostic.json()["detail"]
            assert path.read_text() == config
