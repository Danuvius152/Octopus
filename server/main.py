import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

# Clear this so the `claude` CLI subprocess doesn't think it's nested
# inside another Claude Code session (which would change its behavior).
os.environ.pop("CLAUDECODE", None)

import uvicorn
from fastapi import Depends, FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette._utils import get_route_path
from starlette.datastructures import MutableHeaders
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import deps
from . import monitor as monitor_pkg
from .account_baseline import RetainedLegacyInstallation, require_account_installation
from .agent_manager import AgentManager
from .aio import complete_cleanup
from .app_agent import app_agent_manager
from .app_backends import backend_supervisor
from .applications import application_manager
from .auth import verify_token
from .bg_tasks import bg_task_manager
from .config import settings
from .connector_manager import ConnectorManager
from .crypto import master_key
from .database import Database
from .delegations import delegation_manager
from .harness.registry import all_harnesses
from .installation_admission import InstallationAdmission
from .mcp_http import MOUNT_PREFIX
from .mcp_http import lifespan as mcp_lifespan
from .mcp_http import mount_all as mount_mcp
from .monitor import Event as _MonEvent
from .monitor import MetricsStore
from .monitor import record as _mon_record
from .monitor import sampler as metrics_sampler
from .notifiers import notifier_manager
from .research import research_manager
from .routers import (
    agents,
    attachments,
    backends,
    connectors,
    credentials,
    files,
    notifiers,
    questions,
    schedules,
    sessions,
    ws,
)
from .routers import applications as applications_router
from .routers import auth as auth_router
from .routers import bg_tasks as bg_tasks_router
from .routers import delegations as delegations_router
from .routers import monitor as monitor_router
from .routers import research as research_router
from .routers import wiki as wiki_router
from .scheduler import ScheduleRunner
from .service_health import ServiceHealth
from .service_installation import InstallationOwner, read_service_profile
from .session_manager import session_manager
from .sessions.owner import SessionOwnerUnavailable
from .submission_models import InputAdmissionNotReady
from .tunnel import CloudflareTunnel
from .users import UserManager
from .wiki import WikiError
from .wiki.app_bridge import shutdown as shutdown_wiki_app_writers
from .workspace import WorkspaceError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    service_health = ServiceHealth()
    installation = InstallationOwner(settings)
    try:
        installation.acquire()
    except RetainedLegacyInstallation as error:
        service_health.fail(error)
    app.state.installation_owner = installation
    app.state.service_health = service_health
    db = Database(settings.db_path, health=service_health)
    started: set[str] = set()
    schedule_runner = metrics = sampler_task = tunnel = None
    mcp_owner = None
    mcp_stop = asyncio.Event()
    original: BaseException | None = None
    retired = False
    try:
        from .wiki.registry import registry as wiki_registry
        wiki_registry.bind_health(service_health)
        try:
            await db.inspect()
            await asyncio.to_thread(require_account_installation, settings)
            master_key()
        except BaseException as error:
            service_health.fail(error)
        await db.initialize()
        for harness in all_harnesses():
            harness.open_admission()
        from .wiki.app_bridge import reopen as reopen_wiki_app_writers
        reopen_wiki_app_writers()
        started.add("sessions")
        # acquire() verified this installation's exclusive managed cgroup.
        # Its prior writers are gone; recovery still preserves their unknown
        # outcomes. Standalone startup has no corresponding retirement proof.
        await session_manager.initialize(db, previous_writers_retired=installation.profile is not None)
        # Restore round receipts before any background producer can start work.
        started.add("delegations")
        delegation_manager.bind(session_mgr=session_manager, db=db)
        await delegation_manager.restore()

        # Initialize scheduler
        schedule_runner = ScheduleRunner(session_manager, db)
        started.add("scheduler")
        await schedule_runner.initialize(paused=True)
        app.state.schedule_runner = schedule_runner
        session_manager.set_schedule_runner(schedule_runner)
        schedules._db = db
        schedules._runner = schedule_runner
        agents.set_manager(AgentManager(db))
        connectors.set_manager(ConnectorManager(db))
        applications_router.set_manager(application_manager)
        # Accounts. Bound like the other managers; `deps.current_user` resolves
        # every request's bearer through it (multi-tenancy.md §5.1).
        deps.set_user_manager(UserManager(db))
        credentials.set_db(db)
        notifiers.set_db(db)
        notifier_manager.set_db(db)
        session_manager.set_notifier_manager(notifier_manager)
        await notifier_manager.load()

        # Bg task worker — lives in this FastAPI process so spawned
        # subprocesses survive any per-turn `claude --print` lifetime. The
        # deliver callback synthesizes a user message into the session; the
        # broadcast callback pushes status events to all WS clients.
        bg_task_manager.bind(
            db=db,
            deliver_cb=session_manager.deliver_bg_result,
            broadcast_cb=session_manager._broadcast,
        )
        started.add("background")
        await bg_task_manager.start()

        # Applications (applications.md). Subscribes to the session broadcast bus
        # so a build session's turns drive each application's building/ready/failed
        # status.
        started.add("applications")
        application_manager.bind(session_mgr=session_manager, db=db)
        from .wiki.jobs import worker as wiki_worker
        # Pick up icons for applications that already exist. Discovery otherwise
        # only runs after a build, so an app that already ships a logo would keep
        # showing the generic fallback until someone rebuilt it.
        await application_manager.refresh_icons()
        # Applications with a backend (application-backends.md): stop the ones
        # nobody is using, and never leave one running past our own exit.
        started.add("backends")
        # Applications talking to agents (app-agent-access.md). Also a bus
        # subscriber: it turns a conversation session's events into the small
        # vocabulary an app consumes.
        started.add("app_agent")
        app_agent_manager.bind(
            session_mgr=session_manager, db=db, app_mgr=application_manager
        )

        # Native deep research (native-deep-research.md). Tracks research jobs as
        # async tasks; injects the final report back into the session.
        started.add("research")
        research_manager.bind(session_mgr=session_manager, db=db)
        await research_manager.recover_interrupted()

        # The MCP tool namespaces are served from this app rather than spawned per
        # session (polish-2026-09.md §4 B1). Each one's session manager has to be
        # running before it will answer, and mounting does not start it, so they run
        # for the lifetime of the app.
        metrics = MetricsStore(settings.metrics_db_path, settings.metrics_retention_days)
        await metrics.initialize()
        monitor_pkg.set_store(metrics)
        await metrics.prune()  # honour retention on every boot, not only on a timer

        ready = asyncio.get_running_loop().create_future()
        mcp_owner = asyncio.create_task(_serve_mcp(app, ready, mcp_stop), name="mcp-lifespan")
        await asyncio.shield(ready)
        started.add("wiki_registry")
        await wiki_registry.start(paused=True, previous_writers_retired=installation.profile is not None)
        from .wiki.cleanup import sweep as sweep_wiki_history
        await sweep_wiki_history()
        started.add("wiki_worker")
        await wiki_worker.start(session_manager, paused=True)

        # Recovery is complete. No producer above can dispatch new work.
        service_health.open_admission()
        schedule_runner.start()
        wiki_registry.start_reconciler()
        wiki_worker.start_producing()
        backend_supervisor.start_reaper()
        session_manager.start_reaper()
        metrics.start()
        sampler_task = asyncio.create_task(
            metrics_sampler.run(
                settings.db_path,
                held_process_count=lambda: len(getattr(session_manager, "_held", {}) or {}),
            ), name="metrics-sampler",
        )
        if settings.enable_tunnel:
            tunnel = CloudflareTunnel(port=settings.port)
            url = await tunnel.start()
            if url:
                logger.info("Cloudflare Tunnel active: %s", url)
        logger.info("MCP namespaces served in-process: %s", ", ".join(_mcp_mounts))
        yield
    except BaseException as exc:
        original = exc
        raise
    finally:
        service_health.begin_shutdown()
        cancelled_mcp_startup = mcp_owner is not None and not ready.done()
        if cancelled_mcp_startup:
            mcp_owner.cancel("service-startup-aborted")
        try:
            await complete_cleanup(_shutdown_service(db, started, schedule_runner, metrics, sampler_task, tunnel))
            retired = True
        except BaseException:
            if original is None:
                raise
            logger.exception("Service startup cleanup unconfirmed; database retained")
        finally:
            try:
                if mcp_owner is not None:
                    mcp_stop.set()
                    await complete_cleanup(_join_mcp(mcp_owner, cancelled_startup=cancelled_mcp_startup))
            except BaseException:
                retired = False
                if original is None:
                    raise
                logger.exception("MCP startup compensation failed")
            finally:
                if retired:
                    installation.close()


async def _serve_mcp(app, ready, stop):
    try:
        async with mcp_lifespan(app):
            ready.set_result(None)
            await stop.wait()
    except BaseException as exc:
        if not ready.done():
            ready.set_exception(exc)
            # The startup waiter may already have been cancelled. Preserve
            # the cause for an active waiter and consume it for an absent one.
            ready.exception()
        raise


async def _join_mcp(owner, *, cancelled_startup=False):
    try:
        await owner
    except asyncio.CancelledError:
        if not cancelled_startup:
            raise


async def _shutdown_service(db, started, schedule_runner, metrics, sampler_task, tunnel):
    from .codex_login import codex_login_manager
    from .oauth_login import oauth_login_manager
    from .wiki.jobs import worker as wiki_worker
    from .wiki.registry import registry as wiki_registry
    from .ws_delivery import shutdown_connections

    session_manager.close_admission()
    for harness in all_harnesses():
        harness.begin_shutdown()
    wiki_worker.begin_shutdown()
    if schedule_runner is not None:
        schedule_runner.begin_shutdown()
    research_manager.begin_shutdown()
    bg_task_manager.begin_shutdown()
    backend_supervisor.begin_shutdown()
    codex_login_manager.begin_shutdown()
    if started <= {"sessions", "delegations"}:
        # Close hooks join restored recorders before the session drain budget.
        await asyncio.gather(*(harness.shutdown() for harness in all_harnesses()))
        await db.close()
        if "delegations" in started:
            delegation_manager.shutdown()
        return
    owners = []
    if "sessions" in started:
        owners.extend((("session reaper", session_manager.stop_reaper()),
                       ("session execution", session_manager.drain_turns())))
    for flag, name, stop in (
        ("wiki_worker", "Wiki curator", wiki_worker.stop),
        ("scheduler", "scheduler", schedule_runner.shutdown if schedule_runner else None),
        ("research", "research", research_manager.shutdown),
        ("background", "background tasks", bg_task_manager.shutdown),
        ("backends", "application processes", backend_supervisor.shutdown),
    ):
        if flag in started and stop is not None:
            owners.append((name, stop()))
    owners.append(("Codex login", codex_login_manager.shutdown()))
    owners.extend((f"{harness.backend} one-shot resources", harness.shutdown()) for harness in all_harnesses())
    results = await asyncio.gather(*(stop for _, stop in owners), return_exceptions=True)
    failures = [(name, result) for (name, _), result in zip(owners, results, strict=True)
                if isinstance(result, BaseException)]
    writer_failures = list(failures)

    async def attempt(name, operation, *, writer=False):
        try:
            await operation
        except BaseException as exc:
            failures.append((name, exc))
            if writer:
                writer_failures.append((name, exc))

    # Independent resources can retire even when a writer remains unconfirmed.
    if tunnel is not None:
        await attempt("tunnel", tunnel.stop())
    await attempt("OAuth login", oauth_login_manager.shutdown())
    await attempt("WebSockets", shutdown_connections())
    if sampler_task is not None:
        sampler_task.cancel()
        try:
            await sampler_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            failures.append(("metrics sampler", exc))
    if metrics is not None:
        await attempt("metrics", metrics.stop())
    monitor_pkg.set_store(None)

    await attempt("held processes", session_manager.stop_all_held_processes(), writer=True)
    if "delegations" in started:
        await attempt("delegation recording", delegation_manager.confirm_recording(), writer=True)
    if not any(name == "application processes" for name, _ in writer_failures):
        await attempt("Wiki application writers", shutdown_wiki_app_writers(), writer=True)
    if not writer_failures and "wiki_registry" in started:
        await attempt("Wiki registry", wiki_registry.stop(), writer=True)
    if writer_failures:
        names = ", ".join(name for name, _ in failures)
        logger.error("Service shutdown cleanup unconfirmed: %s; database kept open", names)
        raise RuntimeError(f"Service shutdown cleanup unconfirmed: {names}") from failures[0][1]
    if "app_agent" in started:
        app_agent_manager.shutdown()
    if "applications" in started:
        application_manager.shutdown()
    if "delegations" in started:
        delegation_manager.shutdown()
    await db.close()
    if failures:
        names = ", ".join(name for name, _ in failures)
        raise RuntimeError(f"Independent service resource cleanup failed: {names}") from failures[0][1]


class _HTTPCompression(GZipMiddleware):
    """MCP streams must send headers before their first event.

    GZip buffers response.start even for excluded SSE content. An idle MCP
    GET can therefore disconnect before the outer middleware sees a response.
    Match the same root-relative path as Starlette's mounted namespaces.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and get_route_path(scope).startswith(MOUNT_PREFIX + "/"):
            await self.app(scope, receive, send)
        else:
            await super().__call__(scope, receive, send)


app = FastAPI(title="Octopus", version="0.1.0", lifespan=lifespan)
app.add_middleware(_HTTPCompression, minimum_size=1024, compresslevel=5)


@app.exception_handler(SessionOwnerUnavailable)
async def _session_owner_unavailable(_request: Request, exc: SessionOwnerUnavailable) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.exception_handler(InputAdmissionNotReady)
async def _input_not_ready(_request: Request, exc: InputAdmissionNotReady) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc), "code": exc.code})




@app.exception_handler(WikiError)
async def _wiki_error(_request: Request, exc: WikiError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"detail": str(exc), **getattr(exc, "response", {})})

@app.exception_handler(WorkspaceError)
async def _workspace_error(_request: Request, exc: WorkspaceError) -> JSONResponse:
    """A path outside what this account may reach is a bad request, not a crash.

    `confine()` is raised from inside the session manager, several frames below
    any route, and nothing caught it: naming a working directory outside your
    workspace answered 500 with a traceback in the log (multi-tenancy.md §6).
    One handler rather than a `try` per route, because every path that resolves
    a working directory goes through the same check.
    """
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST, content={"detail": exc.message}
    )


# Mounted before the routers so the /mcp/* prefix is claimed explicitly, and at
# import time because `lifespan` needs the same server instances (module import
# is cached, so both see one object per namespace).
_mcp_mounts = mount_mcp(app)


class _HTTPResponses:
    """Apply metrics and cache policy without buffering streaming headers.

    BaseHTTPMiddleware queues can lose an SSE response.start when its task is
    cancelled during shutdown. Forward ASGI messages in the original task.
    Route templates keep per-session paths out of metric dimensions; human
    long-poll 408s are tagged and are not failures. A 404 must override cache
    policy so a CDN cannot retain a missing asset after it is published.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()

        async def response_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_code = message["status"]
                if status_code == 404:
                    MutableHeaders(scope=message)["Cache-Control"] = "no-store"
                template = getattr(scope.get("route"), "path", None) or scope["path"]
                long_poll = scope.get("state", {}).get("long_poll", False)
                detail = {"route": template, "method": scope["method"]}
                if long_poll:
                    detail["long_poll"] = True
                failed = status_code >= 400 and not (long_poll and status_code == 408)
                _mon_record(_MonEvent(
                    kind="http", duration_ms=(time.perf_counter() - started) * 1000,
                    ok=status_code < 500, error_code=str(status_code) if failed else None,
                    detail=detail,
                ))
            await send(message)

        await self.app(scope, receive, response_send)


app.add_middleware(_HTTPResponses)


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(agents.router)
app.include_router(monitor_router.router)
app.include_router(applications_router.router)
# /apps/{id}/… — the application itself. Registered before the SPA catch-all
# mount so it wins the path (applications.md §3).
app.include_router(wiki_router.app_router)
app.include_router(applications_router.static_router)
app.include_router(sessions.router)
app.include_router(attachments.router)
app.include_router(files.router)
app.include_router(bg_tasks_router.router)
app.include_router(delegations_router.router)
app.include_router(research_router.router)
app.include_router(wiki_router.router)
app.include_router(questions.router)
app.include_router(schedules.router)
app.include_router(schedules.session_router)
app.include_router(credentials.router)
app.include_router(auth_router.router)
app.include_router(connectors.router)
app.include_router(connectors.agent_router)
app.include_router(backends.router)
app.include_router(notifiers.router)
app.include_router(ws.router)


def _installation_blockers(request: Request) -> tuple[str, ...]:
    """Each domain explains its own live execution and delivery obligations."""
    from .codex_login import codex_login_manager
    from .oauth_login import oauth_login_manager
    from .wiki import app_bridge, job_notifications
    from .wiki.jobs import worker
    from .wiki.registry import registry

    gate = getattr(request.app.state, "installation_admission", None)
    runner = getattr(request.app.state, "schedule_runner", None)
    db = session_manager.db
    facts = (
        ("requests", () if gate is not None and gate.idle else ("HTTP response or WebSocket input",)),
        ("sessions", session_manager.drain_blockers()),
        ("background", bg_task_manager.drain_blockers()),
        ("research", research_manager.drain_blockers()),
        ("delegations", delegation_manager.drain_blockers()),
        ("wiki worker", worker.drain_blockers()),
        ("wiki notifications", job_notifications.drain_blockers()),
        ("wiki registry", registry.drain_blockers()),
        ("wiki app bridge", app_bridge.drain_blockers()),
        ("app agent", app_agent_manager.drain_blockers()),
        ("app backends", backend_supervisor.drain_blockers()),
        ("login", codex_login_manager.drain_blockers()),
        ("oauth login", oauth_login_manager.drain_blockers()),
        ("scheduler", runner.drain_blockers() if runner is not None else ()),
        ("database", db.drain_blockers() if db is not None else ("unavailable",)),
        *((f"harness {harness.backend}", harness.drain_blockers()) for harness in all_harnesses()),
    )
    return tuple(f"{domain}: {reason}" for domain, reasons in facts for reason in reasons)


@app.post("/api/installation/quiesce")
async def quiesce_installation(request: Request, user: deps.CurrentUser):
    """Seal an idle managed installation; the external owner then stops it."""
    from uuid import UUID

    if not user["is_admin"]:
        return JSONResponse({"detail": "Installation maintenance requires an administrator"}, status_code=403)
    owner = request.app.state.installation_owner
    health = request.app.state.service_health
    if owner.profile is None:
        return JSONResponse({"detail": "Use the standalone process owner to stop this installation"}, status_code=409)
    try:
        request_id = str(UUID((await request.json())["request_id"]))
    except (ValueError, KeyError, TypeError, AttributeError):
        return JSONResponse({"detail": "A valid request_id is required"}, status_code=400)
    if owner.offline_request is not None:
        if owner.offline_request == request_id:
            return {"status": "stopping", "request_id": request_id}
        return JSONResponse({"detail": "Installation handoff belongs to another request"}, status_code=409)
    if health.state != "ready":
        return JSONResponse({"detail": "Installation is unavailable; handoff was not confirmed"}, status_code=503)
    from .codex_login import codex_login_manager
    from .wiki import app_bridge
    from .wiki.jobs import worker
    from .wiki.registry import registry
    from .wiki.store import WikiError

    try:
        # No await through observation, producer sealing and receipt publication.
        # Registry/Store guards also serialize actual cross-thread admission.
        with registry.handoff_guard():
            if blockers := _installation_blockers(request):
                return JSONResponse({"detail": "Installation still has work; wait for natural completion", "blockers": blockers}, status_code=409)
            health.begin_shutdown()
            for harness in all_harnesses():
                harness.begin_shutdown()
            session_manager.close_admission()
            worker.begin_shutdown()
            registry.begin_shutdown()
            app_bridge.begin_shutdown()
            app_agent_manager.begin_shutdown()
            delegation_manager.begin_shutdown()
            runner = getattr(request.app.state, "schedule_runner", None)
            if runner is not None:
                runner.begin_shutdown()
            research_manager.begin_shutdown()
            bg_task_manager.begin_shutdown()
            backend_supervisor.begin_shutdown()
            codex_login_manager.begin_shutdown()
            # This is a successful handoff receipt, never a sealing intent.
            owner.offline_request = request_id
    except WikiError as error:
        if health.state != "ready":
            raise  # Partial sealing remains closed and has no success receipt.
        return JSONResponse({"detail": "Installation still has work; wait for natural completion", "blockers": [str(error)]}, status_code=409)
    return {"status": "stopping", "request_id": request_id}


@app.get("/api/backends", response_model=backends.BackendAvailability)
async def list_backends(_: str = Depends(verify_token)):
    """Which AI backends are usable on this host (codex-backend.md §6.1).
    A harness kind appears only when its CLI resolves on PATH. `claude-code`
    is always listed (the default) even if not yet installed, matching the
    historical contract."""
    from .harness import available_backends, get_harness

    available = available_backends()
    if "claude-code" not in available:
        available = ["claude-code", *available]
    # Per-backend model shortlists, for `/model`'s picker. A shortlist, not a
    # whitelist — see `RuntimeProfile.models`.
    models: dict[str, list[str]] = {}
    for kind in available:
        try:
            models[kind] = list(get_harness(kind).profile.models)
        except Exception:
            models[kind] = []
    return {
        "available": available,
        "models": models,
        "default": settings.default_backend,
        "tool_policy": {kind: get_harness(kind).profile.tool_policy for kind in available},
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def readiness(request: Request):
    health = getattr(request.app.state, "service_health", None)
    state = health.state if health is not None else "starting"
    result = {"status": state}
    owner = getattr(request.app.state, "installation_owner", None)
    if owner is not None and owner.offline_request:
        result["offline_request"] = owner.offline_request
    return JSONResponse(result, status_code=200 if state == "ready" else 503)


# Serve built frontend as static files (SPA catch-all).
# Mounted after API routes so /api/*, /ws, /health take priority.
_dist_dir = Path(__file__).resolve().parent.parent / "web" / "dist"
app.add_middleware(InstallationAdmission)
if _dist_dir.is_dir():
    app.mount("/", StaticFiles(directory=str(_dist_dir), html=True), name="spa")


def run():
    # Reject managed reload/worker configuration before Uvicorn can create
    # another process. Direct ASGI entry also checks in lifespan.
    read_service_profile(settings)
    uvicorn.run(
        "server.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )


if __name__ == "__main__":
    run()
