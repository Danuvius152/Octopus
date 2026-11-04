"""Host setup for the offline browser test; cards use the product HTTP/DB path."""

import asyncio
import os
import uuid
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

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

    class ReadFault:
        """Hold actual HTTP headers/body once, released by the test owner."""

        def __init__(self, app: ASGIApp):
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send):
            phase = None
            if (scope["type"] == "http" and scope["method"] == "GET"
                    and scope["path"] == f"/api/sessions/{active.get('session_id')}/wiki-jobs"):
                active["read_requests"] += 1
                phase = active.get("read_fault")
                active["read_fault"] = None
            release = active.get("read_release")
            held = False

            async def fault_send(message: Message):
                nonlocal held
                boundary = "http.response.start" if phase == "headers" else "http.response.body"
                if phase and not held and message["type"] == boundary and (phase != "body" or message.get("body")):
                    held = True
                    if phase == "body":
                        # Vite's HTTP proxy forwards headers with its first
                        # body chunk. Send one incomplete byte so this is a
                        # browser body stall rather than another header stall.
                        body = message["body"]
                        await send({"type": "http.response.body", "body": body[:1], "more_body": True})
                        message = {**message, "body": body[1:]}
                    active["read_entered"] = phase
                    await release.wait()
                await send(message)

            await self.app(scope, receive, fault_send)

    app.add_middleware(ReadFault)

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
        active.update(session_id=session_id, user_id=user_id, task_id=task_id, registration=config,
                      read_fault=None, read_release=asyncio.Event(), read_entered=None, read_requests=0)
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

    @app.post("/api/_e2e/wiki-cards/{session_id}/read-fault", include_in_schema=False)
    async def read_fault(session_id: str, request: Request, user_id: ScopeUser = None, _: str = Depends(verify_token)):
        require(request, session_id, user_id)
        phase = (await request.json()).get("phase")
        if phase not in {None, "headers", "body"}:
            raise HTTPException(422, "Select headers, body or release")
        active["read_release"].set()
        active.update(read_release=asyncio.Event(), read_fault=phase, read_entered=None)
        return {"armed": phase}

    @app.get("/api/_e2e/wiki-cards/{session_id}/read-fault", include_in_schema=False)
    async def read_fault_status(session_id: str, request: Request, user_id: ScopeUser = None, _: str = Depends(verify_token)):
        require(request, session_id, user_id)
        return {"entered": active["read_entered"], "requests": active["read_requests"]}

    @app.delete("/api/_e2e/wiki-cards/{session_id}", include_in_schema=False)
    async def finish(
        session_id: str, request: Request, session_manager: SessionMgr,
        user_id: ScopeUser = None, _: str = Depends(verify_token),
    ):
        require(request, session_id, user_id)
        active["read_release"].set()
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
