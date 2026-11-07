"""Shared real-CLI availability gates for the `tests/*_real.py` suites.

Real-LLM tests must skip not just when a backend's binary is absent, but also
when it's present yet NOT signed in — otherwise an expired/lapsed login turns
the whole suite red with confusing downstream errors ("one-shot exited 1",
AI-parse failures) that look like product regressions but are really just a
logged-out CLI. (That's the exact failure harness-credential-reauth.md exists
to surface in the app.)

These probe ACTUAL usability once per session:
  - claude: a tiny real `--print` call must exit 0 (there's no auth.json to stat).
  - codex: a tiny real `exec` call must exit 0 per credential/configuration;
    aliases of the same auth/config files share a result, not provider output.

Imported as `from tests.cli_gate import claude_cli_works, codex_cli_works`
(the repo root is on sys.path during the test run, so `tests` resolves as a
namespace package — `import conftest` is NOT reliable under pytest).
"""

from __future__ import annotations

import functools
import glob
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def _resolve_cli(binary: str) -> str | None:
    """Resolve a CLI honoring the same PATH fallback the harness uses (nvm /
    ~/.local/bin), so the gate matches how the backend actually launches it."""
    try:
        from server.harness.run import _which_with_fallback

        return _which_with_fallback(binary)
    except Exception:
        return shutil.which(binary)


class CliProbeTimeout(RuntimeError):
    """A gate probe timed out, twice.

    Deliberately an error rather than a `False`. `False` means "this CLI is
    absent or logged out", which turns dependent tests into skips — and a skip
    that really meant "the box was busy" is a silently hollow suite, the exact
    thing these gates exist to prevent. A timeout is not evidence about the
    login; it's evidence we couldn't tell, so the suite says so out loud.
    """


def _probe(argv: list[str], *, timeout: float, cwd: str | None = None,
           env: dict[str, str] | None = None) -> bool:
    """Run a gate probe, retrying once with a doubled timeout.

    A loaded machine (a parallel suite, several CLIs mid-turn) can push a
    trivial call past its limit; one retry absorbs that. Two timeouts in a row
    is not load, and is reported rather than swallowed.
    """
    last: Exception | None = None
    for _attempt, limit in enumerate((timeout, timeout * 2)):
        try:
            proc = subprocess.run(
                argv, stdin=subprocess.DEVNULL, capture_output=True,
                timeout=limit, cwd=cwd, env=env,
            )
            return proc.returncode == 0
        except subprocess.TimeoutExpired as exc:
            last = exc
            continue
        except OSError:
            return False
    raise CliProbeTimeout(
        f"{argv[0]} did not answer a trivial probe within "
        f"{timeout:.0f}s or {timeout * 2:.0f}s. This is NOT a lapsed login — "
        f"tests must not be skipped on it. Re-run on a less loaded machine, "
        f"or fix the CLI. (last: {last})"
    )


@functools.lru_cache(maxsize=1)
def claude_cli_works() -> bool:
    """True only if `claude` is installed AND authenticated. Probes once with a
    minimal real call; a logged-out CLI exits non-zero (the 401), so dependent
    tests skip rather than fail."""
    exe = _resolve_cli("claude")
    if exe is None:
        return False
    return _probe([exe, "--print", "--", "ok"], timeout=60)


_codex_probe_results: dict[tuple, bool] = {}


def _file_identity(path: Path) -> tuple | None:
    path = path.resolve()
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return str(path), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def codex_home_works(home: str | Path, *, executable: str | None = None) -> bool:
    """Probe the selected login once; keep every probe's output in a temp home.

    The cache belongs to this pytest process. Only the same executable and
    unchanged auth/config files share a result. A timeout never becomes a
    cached login failure. Missing auth stays scoped to its original home.
    """
    exe = executable or _resolve_cli("codex")
    if exe is None:
        return False
    root = Path(home).expanduser().resolve()
    auth = _file_identity(root / "auth.json")
    key = exe, auth if auth is not None else str(root), _file_identity(root / "config.toml")
    if key not in _codex_probe_results:
        with tempfile.TemporaryDirectory(prefix="octopus-codex-probe-") as probe:
            for name in ("auth.json", "config.toml"):
                if (root / name).is_file():
                    (Path(probe) / name).symlink_to((root / name).resolve())
            result = _probe(
                [
                    exe, "exec", "--json", "--skip-git-repo-check",
                    "--dangerously-bypass-approvals-and-sandbox",
                    "-c", 'model_reasoning_effort="low"', "--", "Reply with OK.",
                ],
                timeout=90, cwd=tempfile.gettempdir(),
                env={**os.environ, "CODEX_HOME": probe},
            )
        _codex_probe_results[key] = result
    return _codex_probe_results[key]


def codex_cli_works() -> bool:
    """Check actual usability of the ambient login, not auth.json presence."""
    return codex_home_works(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def codex_app_server_works() -> bool:
    """Probe the selected login using the product's configured executable.

    Native tests exercise protocol compatibility through the product handshake.
    An unrelated PATH codex cannot decide whether those tests run.
    """
    from server.config import settings

    exe = _resolve_cli(settings.codex_app_server_binary)
    if exe is None:
        return False
    return codex_home_works(os.environ.get("CODEX_HOME") or Path.home() / ".codex", executable=exe)


def codex_home_with_auth() -> str | None:
    """Honor the selected home; only discover another login when none was set."""
    selected = os.environ.get("CODEX_HOME")
    if selected:
        home = os.path.expanduser(selected)
        return home if os.path.exists(os.path.join(home, "auth.json")) else None
    host = os.path.expanduser("~/.codex")
    if os.path.exists(os.path.join(host, "auth.json")):
        return host
    for path in glob.glob(os.path.expanduser("~/.octopus/codex/*/auth.json")):
        return os.path.dirname(path)
    return None
