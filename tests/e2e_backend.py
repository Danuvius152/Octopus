"""Explicit offline-only ASGI entrypoint. Never imported by the real bucket."""

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from tests.e2e_boundary import OFFLINE_ARGV, install

if os.environ.get("OCTOPUS_E2E_MODE") != "offline":
    raise RuntimeError("The offline E2E backend requires OCTOPUS_E2E_MODE=offline")

record = install(Path(os.environ["OCTOPUS_E2E_AUDIT_FILE"]))

# The guard is active before the app, lifespan or harness registry is imported.
from server import fork_helpers
from server.harness import all_backends, get_harness
from server.harness.claude_code import CLAUDE_CODE
from server.harness.driver import ArgvDriver
from tests.harness_profiles import process_profile


def offline_argv(ctx):
    # Do not pass credentials, MCP configuration or prompt to the test program.
    return list(OFFLINE_ARGV), {"cwd": ctx.working_dir, "env": {"OCTOPUS_E2E_OFFLINE": "1"}}


async def offline_git(*args, **kwargs):
    # The UI fixture is a non-Git workspace. Do not execute repository hooks
    # or other child programs while collecting an optional turn/fork anchor.
    return -1, "", "Offline UI workspace has no Git repository"


fork_helpers._git = offline_git


for backend in all_backends():
    harness = get_harness(backend)
    harness.profile = process_profile(
        harness.profile,
        binary=OFFLINE_ARGV[0],
        build_turn_argv=offline_argv,
        build_oneshot_argv=offline_argv,
        parse_oneshot_stdout=lambda _stdout: "Offline UI",
        new_event_parser=CLAUDE_CODE.new_event_parser,
        new_driver=ArgvDriver,
        premature_exit_recovery=False,
        activity_observer=None,
        prepare_resume=None,
    )

from server.main import app as app
from tests.e2e_wiki_cards import install as install_wiki_cards

# Keep the test-only API ahead of the product SPA catch-all.
spa = next((route for route in app.router.routes if route.name == "spa"), None)
if spa is not None:
    app.router.routes.remove(spa)
install_wiki_cards(app)
if spa is not None:
    app.router.routes.append(spa)

product_lifespan = app.router.lifespan_context


@asynccontextmanager
async def offline_lifespan(application):
    if not isinstance(asyncio.get_running_loop(), asyncio.BaseEventLoop):
        record("violation", reason="unsupported_event_loop")
        raise RuntimeError("Offline E2E requires the guarded stdlib asyncio loop")
    async with product_lifespan(application) as state:
        yield state


app.router.lifespan_context = offline_lifespan
record("offline_ready")
