"""The export consumer trusts only the original HTTP/ready success receipt."""

import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from server import portability_lifecycle as lifecycle
from server.config import Settings
from server.service_health import ServiceHealth
from tests.test_installation_drain import installation as installation


@pytest.mark.parametrize("scenario", ["success", "response_lost", "partial_seal", "missing_status", "foreign_id"])
async def test_managed_source_uses_the_actual_handoff_and_ready_contract(installation, monkeypatch, scenario):
    running = installation
    loop = asyncio.get_running_loop()
    controls = []
    entered = []
    monkeypatch.setattr(lifecycle, "read_service_profile", lambda _config: SimpleNamespace(manager="user", unit="isolated-source.service", port=19999))

    def control(argv, **kwargs):
        controls.append(argv)
        if "restart" in argv:
            restarted = ServiceHealth()
            restarted.open_admission()
            running.app.state.service_health = restarted
        return subprocess.CompletedProcess(argv, 0, stdout="ActiveState=active\nMainPID=123\nJob=\n")

    monkeypatch.setattr(lifecycle.subprocess, "run", control)
    if scenario == "partial_seal":
        def fail_gate():
            raise RuntimeError("Producer sealing failed")
        monkeypatch.setattr(running.worker, "begin_shutdown", fail_gate)
    elif scenario == "foreign_id":
        running.app.state.installation_owner.offline_request = "foreign-request"

    def request(url, *, token="", payload=None):
        if payload is not None:
            if scenario == "missing_status":
                return 200, {"request_id": payload["request_id"]}
            future = asyncio.run_coroutine_threadsafe(running.client.post("/api/installation/quiesce", json=payload), loop)
            try:
                response = future.result(timeout=3)
            except RuntimeError:
                return 500, {"detail": "Producer sealing failed"}
            if scenario == "response_lost":
                raise OSError("Response was lost after the actual service sealed")
        else:
            response = asyncio.run_coroutine_threadsafe(running.client.get("/ready"), loop).result(timeout=3)
        return response.status_code, response.json()

    monkeypatch.setattr(lifecycle, "_request", request)

    def export():
        with lifecycle.managed_source(Settings(), token="isolated-admin", timeout=.1):
            entered.append("export")

    if scenario in {"success", "response_lost"}:
        await asyncio.to_thread(export)
        assert entered == ["export"]
        assert [argv[2] for argv in controls] == ["show", "stop", "restart"]
    else:
        with pytest.raises(RuntimeError, match="unknown"):
            await asyncio.to_thread(export)
        assert entered == []
        assert len(controls) == 1
