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

from . import deps
from . import monitor as monitor_pkg
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
from .installation_keys import InstallationKeyRetired
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
    installation = InstallationOwner(settings)
    installation.acquire()
    app.state.installation_owner = installation
    app.state.service_health = service_health = ServiceHealth()
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
            master_key()
        except (OSError, ValueError) as error:
            service_health.fail(error)
        await db.initialize()
        from .token_rotation import recover_auth_token
        await recover_auth_token(db)
        from .memory_handoff import handoff_agent_memory
        async with db._bootstrap_lock:
            await handoff_agent_memory(db)
        from .wiki.app_bridge import reopen as reopen_wiki_app_writers
        reopen_wiki_app_writers()
        from .wiki.account_upgrade import pending_upgrade
        if pending := pending_upgrade():
            session_manager._legacy_transition = True
            if pending["config"].get("app_id"):
                backend_supervisor._blocked_apps.add(pending["config"]["app_id"])
            from .wiki.app_bridge import _blocked_owners
            _blocked_owners.add(None)
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
        auth_router.set_db(db)
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
        wiki_registry.legacy_enabled = not bool(await db.count_users())
        from .wiki.account_upgrade import pending_upgrade
        if pending := pending_upgrade():
            session_manager._legacy_transition = True
            wiki_registry.begin_transition(None)
            if pending.get("user_id"):
                wiki_registry.begin_transition(pending["user_id"])
        started.add("wiki_registry")
        await wiki_registry.start(paused=True)
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
    wiki_worker.begin_shutdown()
    if schedule_runner is not None:
        schedule_runner.begin_shutdown()
    research_manager.begin_shutdown()
    bg_task_manager.begin_shutdown()
    backend_supervisor.begin_shutdown()
    codex_login_manager.begin_shutdown()
    if started <= {"sessions", "delegations"}:
        # Close hooks join restored recorders before the session drain budget.
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


app = FastAPI(title="Octopus", version="0.1.0", lifespan=lifespan)


@app.exception_handler(SessionOwnerUnavailable)
async def _session_owner_unavailable(_request: Request, exc: SessionOwnerUnavailable) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.exception_handler(InstallationKeyRetired)
async def _installation_key_retired(_request: Request, exc: InstallationKeyRetired) -> JSONResponse:
    return JSONResponse(status_code=401, content={"detail": str(exc)})


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


@app.middleware("http")
async def _record_request(request, call_next):
    """Time every request, labelled by route template.

    The template matters: labelling by raw path would mint a new dimension per
    session id and make the table useless within a day (§9 G1).

    A long-poll (a handler that sets `request.state.long_poll`) is tagged so
    latency questions can leave it out: it lasts as long as the user takes to
    answer, and its 408 is the designed "nothing yet", not an error.
    """
    started = time.perf_counter()
    response = await call_next(request)
    route = request.scope.get("route")
    template = getattr(route, "path", None) or request.url.path
    long_poll = getattr(request.state, "long_poll", False)
    detail = {"route": template, "method": request.method}
    if long_poll:
        detail["long_poll"] = True
    failed = response.status_code >= 400 and not (long_poll and response.status_code == 408)
    _mon_record(_MonEvent(
        kind="http",
        duration_ms=(time.perf_counter() - started) * 1000,
        ok=response.status_code < 500,
        error_code=str(response.status_code) if failed else None,
        detail=detail,
    ))
    return response


@app.middleware("http")
async def _no_store_on_missing(request, call_next):
    """Never let a 404 be cached.

    We send no `Cache-Control` on a 404, so a CDN in front of us is free to
    invent one — Cloudflare caches error responses for static-looking paths for
    four hours. The consequence is nasty and non-obvious: the FIRST request for
    an asset that doesn't exist yet poisons that URL long after the file lands,
    so a newly added icon or script is invisible to everyone behind the edge
    while the origin serves it perfectly. Cost real debugging time once; this
    makes it structurally impossible.
    """
    response = await call_next(request)
    if response.status_code == 404:
        response.headers["Cache-Control"] = "no-store"
    return response


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


@app.get("/api/backends")
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
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def readiness(request: Request):
    health = getattr(request.app.state, "service_health", None)
    state = health.state if health is not None else "starting"
    return JSONResponse({"status": state}, status_code=200 if state == "ready" else 503)


# Serve built frontend as static files (SPA catch-all).
# Mounted after API routes so /api/*, /ws, /health take priority.
_dist_dir = Path(__file__).resolve().parent.parent / "web" / "dist"
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
