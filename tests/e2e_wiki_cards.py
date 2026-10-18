"""Host setup for the offline browser test; cards use the product HTTP/DB path."""

import asyncio
import os
import uuid
from pathlib import Path

from fastapi import Depends, HTTPException, Request

from server.auth import verify_token
from server.config import settings
from server.deps import ScopeUser, SessionMgr
from server.mcp_identity import mint
from server.routers.wiki import access
from server.wiki import registry
from server.wiki.job_notifications import dispatch
from server.wiki.jobs import WikiJobs, worker
from server.wiki.tasks import tasks


def install(app):
    if os.environ.get("OCTOPUS_E2E_MODE") != "offline":
        raise RuntimeError("Wiki browser setup is only available in the guarded offline backend")
    active = {}

    def require(request, session_id, user_id):
        access(request, human_only=True)
        if active.get("session_id") != session_id or active.get("user_id") != user_id:
            raise HTTPException(404, "Offline Wiki fixture not found")

    @app.post("/api/_e2e/wiki-cards/{session_id}", include_in_schema=False)
    async def prepare(
        session_id: str, request: Request, session_manager: SessionMgr,
        user_id: ScopeUser = None, _: str = Depends(verify_token),
    ):
        access(request, human_only=True)
        session = session_manager.get_session(session_id, user_id)
        if session is None:
            raise HTTPException(404, "Session not found")
        if active:
            raise HTTPException(409, "Offline Wiki fixture is already active")
        if (registry.state(user_id) / "config.json").exists():
            raise HTTPException(409, "Offline Wiki fixture requires an unregistered test installation")
        # Pause automatic execution, retaining the actual manager for the
        # cancellation route. No model, fabricated job or card is seeded.
        await worker.stop()
        worker.manager = session_manager
        root = Path(settings.default_working_dir) / "wiki-card-browser"
        root.mkdir(parents=True, exist_ok=True)
        (root / "note.md").write_text("# Browser fixture\nPreserve these words.\n")
        config = await asyncio.to_thread(registry.configure, user_id, root, session.agent_id)
        request_id = f"browser-origin-{uuid.uuid4().hex}"
        task_id = tasks.open(session, "auto", request_id=request_id)
        active.update(session_id=session_id, user_id=user_id, task_id=task_id, registration=config)
        return {"token": mint(session_id, user_id=user_id, wiki_task_id=task_id),
                "request_id": request_id, "vault_id": config["vault_id"]}

    @app.post("/api/_e2e/wiki-cards/{session_id}/deliver", include_in_schema=False)
    async def deliver(
        session_id: str, request: Request, session_manager: SessionMgr,
        user_id: ScopeUser = None, _: str = Depends(verify_token),
    ):
        require(request, session_id, user_id)
        jobs = await asyncio.to_thread(WikiJobs, user_id)
        return {"delivered": await dispatch(jobs, session_manager)}

    @app.delete("/api/_e2e/wiki-cards/{session_id}", include_in_schema=False)
    async def finish(
        session_id: str, request: Request, session_manager: SessionMgr,
        user_id: ScopeUser = None, _: str = Depends(verify_token),
    ):
        require(request, session_id, user_id)
        jobs = await asyncio.to_thread(WikiJobs, user_id)
        rows = await asyncio.to_thread(jobs.list)
        if rows["next_offset"] is not None or any(
            row["status"] in {"queued", "running", "interrupted"} for row in rows["items"]
        ):
            raise HTTPException(409, "Cancel the browser's queued work before teardown")
        await worker.stop()
        await asyncio.to_thread(registry.block, user_id)
        if registry.config(user_id) != active["registration"]:
            raise HTTPException(409, "The browser fixture registration changed")
        (registry.state(user_id) / "config.json").unlink()
        registry.unblock(user_id)
        tasks.close(active["task_id"])
        active.clear()
        await worker.start(session_manager)
        # The owned registration and observers retire before global teardown
        # removes E2E_STATE_DIR; no receiver races deletion of its state.
        return {"closed": True}
