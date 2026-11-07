"""The real-CLI gates decide whether the `*_real.py` suites run at all, so a
gate that answers wrongly is worse than a failing test: it turns real tests
into skips and the suite still reads green.

CLAUDE.md's rule is that a skip means a lapsed login, not a passing suite.
These tests pin the distinction the gate has to draw — absent/logged-out is a
skip, "we couldn't tell" is not.

The second half of the file pins the *marker* invariants. The gates are
applied by markers (`real`, `real_claude`, `real_codex`, `claude_bin`,
`codex_bin`) resolved in `conftest.pytest_runtest_setup`, and that scheme has
the same silent-failure mode as a wrong gate: a `*_real.py` file that forgets
`pytest.mark.real` lands its live-model tests in the hermetic tier, where
`-m "not real"` will happily run them against a real CLI and nobody notices
until CI-less local runs start making network calls.
"""

import ast
import pathlib
import re
import subprocess

import pytest

from tests.cli_gate import CliProbeTimeout, _probe

_REAL_SUITES = sorted(pathlib.Path(__file__).parent.glob("test_*_real.py"))


def test_probe_returns_true_when_the_cli_answers():
    assert _probe(["true"], timeout=10) is True


def test_probe_returns_false_when_the_cli_fails():
    """A logged-out CLI exits non-zero — that IS evidence, so dependent tests
    legitimately skip."""
    assert _probe(["false"], timeout=10) is False


def test_probe_returns_false_when_the_binary_is_missing():
    assert _probe(["/nonexistent-binary-for-gate-test"], timeout=10) is False


def test_probe_retries_once_before_giving_up(monkeypatch):
    """A loaded box can push a trivial call past its limit; one retry absorbs
    that rather than declaring the CLI logged out."""
    calls: list[float] = []
    real_run = subprocess.run

    def flaky(argv, **kwargs):
        calls.append(kwargs["timeout"])
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return real_run(["true"], **{k: v for k, v in kwargs.items() if k != "timeout"})

    monkeypatch.setattr(subprocess, "run", flaky)
    assert _probe(["true"], timeout=5) is True
    # Second attempt gets a longer budget than the first.
    assert calls == [5, 10]


def test_two_timeouts_raise_instead_of_skipping(monkeypatch):
    """The important one. Two timeouts mean we could not tell whether the CLI
    works — which must never be reported as "logged out", because that silently
    hollows out the suite."""
    def always_timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", always_timeout)
    with pytest.raises(CliProbeTimeout, match="NOT a lapsed login"):
        _probe(["whatever"], timeout=1)


@pytest.fixture
def codex_probe(tmp_path, monkeypatch):
    from tests import cli_gate

    home = tmp_path / "provider"
    home.mkdir()
    (home / "auth.json").write_text('{"test_credential": "first"}')
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr(cli_gate, "_resolve_cli", lambda _: "/test/bin/codex")
    monkeypatch.setattr(cli_gate, "_codex_probe_results", {}, raising=False)
    calls, answers = [], []

    def answer(argv, **kwargs):
        probe_home = pathlib.Path(kwargs["env"]["CODEX_HOME"])
        calls.append({
            "home": probe_home, "auth": (probe_home / "auth.json").resolve(),
            "config": (probe_home / "config.toml").resolve() if (probe_home / "config.toml").exists() else None,
            "timeout": kwargs["timeout"], "argv": argv,
        })
        response = answers.pop(0) if answers else 0
        if response == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return subprocess.CompletedProcess(argv, response)

    monkeypatch.setattr(subprocess, "run", answer)
    return home, calls, answers


@pytest.mark.parametrize("configured", [False, True])
def test_codex_gate_reuses_the_same_credentials_without_writing_provider_output(codex_probe, configured):
    from tests import cli_gate

    home, calls, _ = codex_probe
    if configured:
        (home / "config.toml").write_text('model_reasoning_effort = "low"\n')
    alias = home.parent / "credential-alias"
    alias.mkdir()
    for name in ("auth.json", "config.toml"):
        if (home / name).exists():
            (alias / name).symlink_to(home / name)
    assert cli_gate.codex_cli_works()
    assert cli_gate.codex_home_works(alias)
    assert cli_gate.codex_cli_works()
    assert len(calls) == 1
    assert calls[0]["home"] not in {home, alias}
    assert calls[0]["auth"] == home / "auth.json"
    assert calls[0]["config"] == (home / "config.toml" if configured else None)
    assert not calls[0]["home"].exists()
    assert 'model_reasoning_effort="low"' in calls[0]["argv"]
    assert set(path.name for path in home.iterdir()) == ({"auth.json", "config.toml"} if configured else {"auth.json"})


def test_codex_gate_keeps_different_credential_results_separate(codex_probe):
    from tests import cli_gate

    home, calls, answers = codex_probe
    other = home.parent / "other"
    other.mkdir()
    (other / "auth.json").write_text('{"test_credential": "second"}')
    answers.extend([0, 1])
    assert cli_gate.codex_cli_works()
    assert not cli_gate.codex_home_works(other)
    assert not cli_gate.codex_home_works(other)
    assert cli_gate.codex_cli_works()
    assert [call["auth"] for call in calls] == [home / "auth.json", other / "auth.json"]


@pytest.mark.parametrize("changed", ["auth.json", "config.toml", "executable"])
def test_codex_gate_rechecks_changed_credentials_or_configuration(codex_probe, monkeypatch, changed):
    from tests import cli_gate

    home, calls, answers = codex_probe
    answers.extend([0, 1])
    assert cli_gate.codex_cli_works()
    if changed == "executable":
        monkeypatch.setattr(cli_gate, "_resolve_cli", lambda _: "/test/bin/new-codex")
    else:
        (home / changed).write_text("changed test input")
    assert not cli_gate.codex_cli_works()
    assert len(calls) == 2


def test_codex_gate_propagates_timeouts_and_allows_a_later_probe(codex_probe):
    from tests import cli_gate

    _, calls, answers = codex_probe
    answers.extend(["timeout", "timeout", 0])
    with pytest.raises(CliProbeTimeout):
        cli_gate.codex_cli_works()
    assert cli_gate.codex_cli_works()
    assert [call["timeout"] for call in calls] == [90, 180, 90]
    assert all(not call["home"].exists() for call in calls)


def test_native_gate_uses_configured_binary_when_path_codex_is_absent(codex_probe, monkeypatch):
    from types import SimpleNamespace

    from server.config import settings
    from tests import cli_gate, conftest

    _, calls, _ = codex_probe
    configured = "/selected/native-codex"
    monkeypatch.setattr(settings, "codex_app_server_binary", configured)
    resolved = []

    def resolve(binary):
        resolved.append(binary)
        return configured if binary == configured else None

    monkeypatch.setattr(cli_gate, "_resolve_cli", resolve)
    item = SimpleNamespace(get_closest_marker=lambda marker: object() if marker in {
        "real", "real_codex", "native_codex",
    } else None)
    conftest.pytest_runtest_setup(item)
    assert resolved == [configured]
    assert len(calls) == 1 and calls[0]["argv"][0] == configured
    assert calls[0]["argv"][1] == "exec"


def test_native_gate_missing_configured_binary_does_not_probe_another_login(codex_probe, monkeypatch):
    from server.config import settings
    from tests import cli_gate

    _, calls, _ = codex_probe
    monkeypatch.setattr(settings, "codex_app_server_binary", "/missing/configured-codex")
    monkeypatch.setattr(cli_gate, "_resolve_cli", lambda _: None)
    assert not cli_gate.codex_app_server_works()
    assert calls == []


@pytest.mark.parametrize("timed_out", [False, True])
def test_research_credential_discovery_uses_the_shared_gate(tmp_path, monkeypatch, timed_out):
    from server.config import settings
    from tests import test_research_real as research

    for name in ("first", "second"):
        home = tmp_path / name
        home.mkdir()
        (home / "auth.json").write_text('{"test_credential": "configured"}')
    monkeypatch.setattr(settings, "codex_home_dir", str(tmp_path))
    calls = []

    def probe(home):
        calls.append(pathlib.Path(home).name)
        if timed_out:
            raise CliProbeTimeout("could not verify login")
        return pathlib.Path(home).name == "second"

    monkeypatch.setattr(research, "codex_home_works", probe, raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1))
    research._usable_codex_credential.cache_clear()
    try:
        if timed_out:
            with pytest.raises(CliProbeTimeout):
                research._usable_codex_credential()
            assert calls == ["first"]
        else:
            assert research._usable_codex_credential() == "second"
            assert calls == ["first", "second"]
    finally:
        research._usable_codex_credential.cache_clear()


@pytest.mark.parametrize("markers,expected", [
    ({"real", "real_codex"}, "low"),
    ({"real", "codex_bin"}, "low"),
    ({"real", "real_codex", "knowledge_eval"}, "high"),
    ({"real", "real_claude"}, "high"),
    (set(), "high"),
])
async def test_codex_smoke_effort_is_scoped_and_preserves_callers(monkeypatch, markers, expected):
    from types import SimpleNamespace

    from server.harness import OneShotContext, RunConfig
    from tests import conftest

    class Harness:
        def create_run(self, config=None):
            return config or RunConfig()

        async def run_oneshot(self, context, *, timeout):
            return context, timeout

    harness = Harness()
    monkeypatch.setattr("server.harness.get_harness", lambda backend: harness)
    request = SimpleNamespace(node=SimpleNamespace(get_closest_marker=lambda name: name in markers))
    conftest._codex_smoke_uses_low_effort.__wrapped__(request, monkeypatch, None)

    config = RunConfig(reasoning_effort="high", service_tier="fast", mcp_servers=[])
    context = OneShotContext(prompt="A protocol probe", reasoning_effort="high", service_tier="fast")
    configured = harness.create_run(config)
    oneshot, timeout = await harness.run_oneshot(context, timeout=17)
    assert configured.reasoning_effort == oneshot.reasoning_effort == expected
    assert config.reasoning_effort == context.reasoning_effort == "high"
    assert configured.mcp_servers == [] and oneshot.prompt == context.prompt
    assert configured.service_tier == oneshot.service_tier == "fast" and timeout == 17
    assert harness.create_run().reasoning_effort == ("low" if expected == "low" else None)


# --------------------------------------------------------------- marker invariants


def test_every_real_suite_is_discovered():
    """Guard the guard: if this glob ever returns nothing the two tests below
    pass vacuously and stop protecting anything."""
    assert len(_REAL_SUITES) >= 13, [p.name for p in _REAL_SUITES]


@pytest.mark.parametrize("path", _REAL_SUITES, ids=lambda p: p.name)
def test_real_suite_carries_the_real_marker(path):
    """Every `*_real.py` must opt into the `real` umbrella at module level.

    `-m "not real"` is the hermetic tier, so a file without this marker is not
    merely mis-labelled — its live-model tests join the tier that is supposed
    to be safe to run anywhere.
    """
    src = path.read_text()
    match = re.search(r"^pytestmark\s*=\s*(.+?)(?=\n\n|\n@|\nclass |\ndef )",
                      src, re.MULTILINE | re.DOTALL)
    assert match, f"{path.name} has no module-level pytestmark"
    assert "mark.real" in match.group(1), (
        f"{path.name} has a pytestmark but not pytest.mark.real: {match.group(1)!r}"
    )


@pytest.mark.parametrize("path", _REAL_SUITES, ids=lambda p: p.name)
def test_real_suite_does_not_probe_at_import(path):
    """No `*_real.py` may call a gate probe at module scope.

    `claude_cli_works()` shells out to a real `claude --print`, so evaluating
    it in a module-level `pytestmark = pytest.mark.skipif(...)` or a
    `HAS_CLAUDE = ...` constant made plain `pytest --collect-only` invoke the
    model — 16.44 s against 0.80 s, for a command that is meant to be free.
    The markers exist precisely to move that probe into setup; this test stops
    the old pattern coming back.
    """
    src = path.read_text()
    gates = {"claude_cli_works", "codex_cli_works", "codex_home_works", "codex_app_server_works"}
    offenders = []

    class CallsAtImport(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            # Defaults and decorators run at import; fixture bodies do not.
            for expression in [*node.decorator_list, *node.args.defaults, *node.args.kw_defaults]:
                if expression is not None:
                    self.visit(expression)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Lambda(self, node):
            for expression in [*node.args.defaults, *node.args.kw_defaults]:
                if expression is not None:
                    self.visit(expression)

        def visit_Call(self, node):
            if ((isinstance(node.func, ast.Name) and node.func.id in gates)
                    or (isinstance(node.func, ast.Attribute) and node.func.attr in gates)):
                offenders.append(ast.unparse(node))
            self.generic_visit(node)

    CallsAtImport().visit(ast.parse(src))
    assert not offenders, (
        f"{path.name} evaluates a gate probe at import; use a marker instead: "
        f"{offenders}"
    )
