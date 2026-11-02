"""Global test configuration — ensure tests use known default settings."""

import atexit
import os
import shutil
import signal
import tempfile
import time
from dataclasses import replace
from pathlib import Path

import pytest

pytest_plugins = ["tests.wiki_cost_probe"]

# Override env vars before any module imports Settings, so tests
# don't pick up values from the user's .env file.
os.environ["OCTOPUS_AUTH_TOKEN"] = "changeme"
os.environ["OCTOPUS_DEFAULT_BACKEND"] = "claude-code"

_PROC = Path("/proc")


@pytest.fixture
def short_process_shutdown(monkeypatch):
    """Exercise real retirement without spending the production grace periods.

    Only request this in tests of cancellation, ownership or recovery. Tests of
    the default shutdown timing keep the ordinary Harness budgets.
    """
    from tests.process_shutdown import shorten_harness_shutdown

    shorten_harness_shutdown(monkeypatch)


@pytest.fixture(autouse=True)
def _harness_installation(monkeypatch):
    """Each temporary installation has its own one-shot resource owners."""
    from server.harness import Harness
    from server.harness.registry import _REGISTRY

    for backend, harness in tuple(_REGISTRY.items()):
        monkeypatch.setitem(_REGISTRY, backend, Harness(harness.profile))


@pytest.fixture(autouse=True)
def _codex_smoke_uses_low_effort(request, monkeypatch, _harness_installation):
    """Pin live protocol checks without changing the caller or host configuration."""
    marker = request.node.get_closest_marker
    if not marker("real") or marker("knowledge_eval") or not (marker("real_codex") or marker("codex_bin")):
        return
    from server.harness import RunConfig, get_harness

    harness = get_harness("codex")
    create_run, run_oneshot = harness.create_run, harness.run_oneshot

    def low_run(config=None):
        return create_run(replace(config or RunConfig(), reasoning_effort="low"))

    async def low_oneshot(context, **kwargs):
        return await run_oneshot(replace(context, reasoning_effort="low"), **kwargs)

    monkeypatch.setattr(harness, "create_run", low_run)
    monkeypatch.setattr(harness, "run_oneshot", low_oneshot)


@pytest.fixture(autouse=True)
def _hermetic_execution_boundary(request, monkeypatch):
    if request.node.get_closest_marker("real") is not None:
        yield
        return
    from tests.hermetic_guard import install

    violations = install(monkeypatch)
    yield violations
    # A manager may catch a failed build. That must still fail this test.
    if violations:
        pytest.fail("; ".join(violations))

# Isolate per-agent state (the canonical memory dir each agent gets) under a
# throwaway temp root, so creating agents in tests never litters the
# developer's real ~/.octopus/agents (docs/implemented/memory.md). Set before any
# import of Settings; cleaned at process exit. Per-test fixtures may still
# point agents_dir at their own tmp_path — that just overrides this default.
_TEST_AGENTS_DIR = tempfile.mkdtemp(prefix="octopus-test-agents-")
os.environ["OCTOPUS_AGENTS_DIR"] = _TEST_AGENTS_DIR
atexit.register(lambda: shutil.rmtree(_TEST_AGENTS_DIR, ignore_errors=True))

# A wider MCP startup budget for the real-CLI tier than production needs.
# Those tests run the callback server, the turn engine and two or three live
# CLIs inside one process and one event loop, and each CLI opens a handshake per
# namespace on startup. Measured in isolation a handshake takes ~0.1 s, but
# under that pressure the CLI's default budget is occasionally missed, and the
# CLI then reports the namespace as `CONNECT_TIMEOUT` and drops its tools — a
# test failure that says "the model ignored the instruction" when the tool was
# never offered. Production serves from its own process; this is test pressure,
# so it is corrected here rather than in the product.
os.environ.setdefault("MCP_TIMEOUT", "30000")

# Real-CLI availability gates live in tests/cli_gate.py (imported by the
# *_real.py suites as `from tests.cli_gate import …`); they're not here because
# `import conftest` isn't reliably resolvable under pytest collection.


def _ppid(pid: int) -> int | None:
    """Parent pid from /proc/<pid>/stat — same trick the monitor sampler uses.

    The comm field can contain spaces and parentheses, so everything up to the
    last ')' is skipped rather than split on.
    """
    try:
        raw = (_PROC / str(pid) / "stat").read_text()
    except OSError:
        return None
    try:
        return int(raw[raw.rindex(")") + 1 :].split()[1])
    except (ValueError, IndexError):
        return None


def _surviving_cli_pids() -> list[int]:
    """Live `claude` / `codex` processes descended from this pytest run.

    Ancestry is checked, not just the name: this box runs a production Octopus
    with CLIs of its own, and a test suite has no business killing those.
    """
    me = os.getpid()
    found: list[int] = []
    for entry in _PROC.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == me:
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
        if "claude" not in cmdline and "codex" not in cmdline:
            continue
        cur: int | None = pid
        for _ in range(8):  # a CLI sits a couple of hops below us at most
            cur = _ppid(cur) if cur else None
            if cur is None or cur == 1:
                break
            if cur == me:
                found.append(pid)
                break
    return found


def _seed_cli_home(source, destination, files):
    for name in files:
        if (source / name).is_file():
            if name == 'models_cache.json':
                # Preserve the selected catalog before spawn-time validation;
                # subsequent CLI refreshes belong only to this test home.
                shutil.copy2(source / name, destination / name)
            else:
                (destination / name).symlink_to(source / name)


@pytest.fixture(autouse=True)
def _account_state_stays_in_tmp(request, tmp_path_factory, monkeypatch):
    """No test may write into a real install's state directory.

    Three settings default into `~/.octopus`: `master_key_file`, which
    `crypto.master_key()` *creates* when it is missing; `users_root`, under
    which `paths_for(user_id).ensure()` makes an account's directories; and
    `research_dir`, where a research job writes its scratch cwd and report.
    All three are reached by ordinary hermetic tests, so a plain `pytest` run
    left a 0600 key file, a `users/` tree and hundreds of research directories
    in the state directory of whatever install happened to be deployed on the
    box — found exactly that way.

    Pointed at a per-session tmp dir for every test. A test that cares about
    the value still sets its own afterwards; this only decides where the
    default lands.
    """
    from server.config import settings

    root = tmp_path_factory.mktemp("octopus-state")
    real = request.node.get_closest_marker("real") is not None
    credential_root = Path(settings.codex_home_dir).expanduser()
    # Installation discovery includes databases, cwd and default provider homes,
    # not only account files. Child processes must inherit the same isolation.
    for name, relative in {
        "db_path": "octopus.db", "metrics_db_path": "metrics.db",
        "master_key_file": "master.key", "users_root": "users",
        "default_working_dir": "workspace", "agents_dir": "agents",
        "attachments_dir": "attachments", "large_prompts_dir": "large-prompts",
        "codex_home_dir": "codex", "applications_dir": "applications",
        "research_dir": "research", "wiki_dir": "wiki",
    }.items():
        value = str(root / relative)
        monkeypatch.setattr(settings, name, value)
        monkeypatch.setenv("OCTOPUS_" + name.upper(), value)
    Path(settings.default_working_dir).mkdir()
    for variable, default, files in (
        ("CODEX_HOME", ".codex", ("auth.json", "config.toml", "models_cache.json")),
        ("CLAUDE_CONFIG_DIR", ".claude", (".credentials.json", "settings.json", ".claude.json")),
    ):
        source = Path(os.environ.get(variable) or Path.home() / default).expanduser()
        destination = root / default
        destination.mkdir()
        if real:
            # Keep live authentication at its existing owner; isolate all CLI
            # output and exclude the managed installation's ownership marker.
            _seed_cli_home(source, destination, files)
        monkeypatch.setenv(variable, str(destination))
    if real and (request.node.get_closest_marker("real_codex") or request.node.get_closest_marker("codex_bin")):
        # Codex installs its helper aliases under CODEX_HOME and rejects homes
        # below its temporary directory. Keep both under this fixture's owner,
        # in sibling directories; hermetic tests retain their existing setup.
        cli_temporary = root / "cli-tmp"
        cli_temporary.mkdir()
        monkeypatch.setenv("TMPDIR", str(cli_temporary))
    if real and credential_root.is_dir():
        credentials = Path(settings.codex_home_dir)
        credentials.mkdir()
        for source in credential_root.iterdir():
            if source.is_dir() and (source / "auth.json").is_file():
                destination = credentials / source.name
                destination.mkdir()
                _seed_cli_home(source, destination, ('auth.json', 'config.toml', 'models_cache.json'))
    # Each temporary installation starts with its own healthy storage boundary.
    # Keep resource-owner collections intact so teardown leaks remain visible.
    from server.service_health import ServiceHealth
    from server.wiki import app_bridge
    from server.wiki.registry import registry
    # This fixture owns a complete temporary installation. Confirm that the
    # preceding owner retired before opening this installation's health.
    registry.bind_health(ServiceHealth())
    monkeypatch.setattr(registry, "_blocked", set())
    # Lifespan tests stop the shared registry. A fresh test installation must
    # reopen through the real drain checks, without clearing resource owners.
    if registry._stopped:
        registry.resume_access()
    monkeypatch.setattr(app_bridge, "_stopping", False)
    monkeypatch.setattr(app_bridge, "_blocked_owners", set())
    try:
        yield
    finally:
        # Use actual resource retirement, never clear the owner collections.
        registry.close()


@pytest.fixture(autouse=True)
def _accounts_start_empty():
    """A test cannot inherit an account service bound to a closed database."""
    from server import deps

    deps._user_manager = None
    yield
    deps._user_manager = None


@pytest.fixture(autouse=True)
def _no_cli_left_behind(request):
    """Release every CLI a real test started, whether or not it remembered to.

    A held `claude` is ~250 MB and lives until its manager is told to stop.
    Several of the `*_real.py` suites build their own `SessionManager` and never
    tell it, so the tier accumulated idle CLIs as it ran — which is how a suite
    where every test passes alone starts failing in a group: the box gets slow
    enough that a CLI's own MCP handshake times out, and the model then reports
    the tool as unavailable. (inline-steering.md §7 records the OOM this caused
    the first time.)

    Hermetic tests spawn nothing, so this only runs for the `real` marker.
    """
    yield
    if request.node.get_closest_marker("real") is None:
        return
    survivors = _surviving_cli_pids()
    for pid in survivors:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    if not survivors:
        return
    time.sleep(0.3)
    for pid in _surviving_cli_pids():
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def pytest_addoption(parser):
    integration = parser.getgroup("live integration")
    integration.addoption(
        "--run-real", action="store_true", default=False,
        help="Enable explicitly selected real-provider and supervisor tests",
    )
    performance = parser.getgroup("Wiki performance")
    performance.addoption(
        "--run-wiki-perf", action="store_true", default=False,
        help="Run optional Wiki scale and performance measurements (not release gates)",
    )
    performance.addoption(
        "--wiki-perf-output", metavar="FILE",
        help="Retain the expanded Wiki efficiency matrix, raw samples and source fingerprint as JSON",
    )
    performance.addoption(
        "--wiki-baseline-reference", metavar="FILE",
        help="Compare frozen phase-one pages with a previously retained performance report",
    )
    group = parser.getgroup("knowledge evaluation")
    group.addoption(
        "--run-knowledge-evals", action="store_true", default=False,
        help="Run optional knowledge-answer and maintenance-intent evaluations (not release gates)",
    )
    group.addoption(
        "--knowledge-eval-output", metavar="DIR",
        help="Retain optional evaluation reports under DIR; otherwise use each test's temporary directory",
    )
    group.addoption(
        "--wiki-chat-registration", metavar="FILE",
        help="Prospectively registered source root, credential id and at most four natural chat questions",
    )
    group.addoption(
        "--wiki-query-registration", metavar="FILE",
        help="Prospectively registered source root, explicit profile and dedicated query questions",
    )


def pytest_collection_modifyitems(config, items):
    """Opt in before setup, including the signed-in CLI availability probe."""
    disabled = [marker for marker, option in (
        ("real", "run_real"),
        ("knowledge_eval", "run_knowledge_evals"),
        ("wiki_perf", "run_wiki_perf"),
    ) if not config.getoption(option)]
    selected, deselected = [], []
    for item in items:
        if any(item.get_closest_marker(marker) for marker in disabled):
            deselected.append(item)
        else:
            selected.append(item)
    if deselected:
        items[:] = selected
        config.hook.pytest_deselected(items=deselected)


def pytest_runtest_setup(item):
    """Apply the real-CLI markers — at setup, deliberately not at import.

    The `*_real.py` suites used to compute their skip condition at module
    level (`pytestmark = pytest.mark.skipif(not claude_cli_works(), …)`, or
    a `HAS_CLAUDE = claude_cli_works()` constant). Because `claude_cli_works`
    probes by making a real `claude --print` call, that turned plain
    collection into something that invoked the model: `--collect-only` took
    16.44 s against 0.80 s for the hermetic tier, and `pytest --collect-only`
    is supposed to be free.

    Running the probe here instead means it fires only when a test carrying
    the marker is genuinely about to run — and never at all under
    `-m "not real"`, which deselects before setup.

    `cli_gate` is imported inside the function for the same reason: keep
    importing conftest side-effect-free. The gates cache each login check;
    Codex shares a result across temporary aliases of unchanged auth/config
    files while keeping different credentials separate.
    """
    if item.get_closest_marker("knowledge_eval"):
        # The evidence-producing request verifies provider availability itself.
        # A separate CLI probe spends tokens outside that request's ledger and
        # may use a different login/model. Preserve actual execution failures.
        return

    from server.harness.run import _which_with_fallback
    from tests.cli_gate import claude_cli_works, codex_app_server_works, codex_cli_works

    native = bool(item.get_closest_marker("native_codex"))

    checks = (
        # marker         predicate                                    skip reason
        ("real_claude", claude_cli_works, "claude CLI unavailable or not signed in"),
        ("real_codex", codex_app_server_works if native else codex_cli_works,
         "selected native Codex unavailable or not signed in" if native else "codex CLI unavailable or not signed in"),
        ("claude_bin", lambda: _which_with_fallback("claude") is not None,
         "claude CLI not installed"),
        ("codex_bin", lambda: _which_with_fallback("codex") is not None,
         "codex CLI not installed"),
    )
    for marker, is_available, reason in checks:
        if item.get_closest_marker(marker) and not is_available():
            import pytest

            pytest.skip(reason)


@pytest.fixture
def mcp_scope(monkeypatch):
    """Put a tool call in a session's scope, the way a real HTTP request does.

    The MCP namespaces are served in-process now rather than spawned per session
    (docs/2026-09-25-polish-pass.md), so a tool's session no longer arrives
    as `OCTOPUS_SESSION_ID` in a process environment — one process serves every
    session, and the session comes from the verified scope of the request. Tests
    that used to `monkeypatch.setenv` establish a scope instead.

    `api_base` is redirected too, so a test can keep asserting against a stable
    fake host rather than whatever port `settings` happens to hold.
    """
    from server.mcp_identity import McpScope, reset_current_scope, set_current_scope
    from server.mcp_servers import _host

    state = {"session_id": "s1", "installation_id": None, "api_base": "http://x"}
    monkeypatch.setattr(_host, "api_base", lambda: state["api_base"])

    def _enter():
        return set_current_scope(
            McpScope(state["session_id"], state["installation_id"])
        )

    token = _enter()

    class Control:
        """Lets a test move the scope mid-test (e.g. to assert isolation)."""

        def set(self, session_id=None, installation_id=None, api_base=None):
            nonlocal token
            if session_id is not None:
                state["session_id"] = session_id
            if installation_id is not None:
                state["installation_id"] = installation_id
            if api_base is not None:
                state["api_base"] = api_base
            reset_current_scope(token)
            token = _enter()

        def clear(self):
            nonlocal token
            reset_current_scope(token)
            token = set_current_scope(None)

    yield Control()
    reset_current_scope(token)
