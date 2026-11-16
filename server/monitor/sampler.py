"""Periodic gauges: the numbers that only make sense sampled.

Event hooks answer "what happened". These answer "what does it look like right
now" — process counts, memory, WAL growth — which is the half that shows a
slow leak, and the half that proves whether §4 B1 actually removed the sidecar
cost rather than moving it.

PSS (`/proc/<pid>/smaps_rollup`) rather than RSS: shared pages counted once per
process make several Python interpreters look far heavier than they are, and
that mistake is exactly what the B1 measurement had to correct for.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
from pathlib import Path

from ..config import settings
from ..harness.run import _which_with_fallback
from . import Sample, sample

logger = logging.getLogger(__name__)

SAMPLE_INTERVAL_SECONDS = 60.0
_PROC = Path("/proc")


def _pss_kb(pid: str) -> int | None:
    try:
        for line in (_PROC / pid / "smaps_rollup").read_text().splitlines():
            if line.startswith("Pss:"):
                return int(line.split()[1])
    except (OSError, PermissionError, ValueError, IndexError):
        return None
    return None


def _argv(pid: str) -> list[str]:
    try:
        raw = (_PROC / pid / "cmdline").read_bytes()
    except (OSError, PermissionError):
        return []
    return [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]


def _ppid(pid: str) -> str | None:
    """Parent pid from /proc/<pid>/stat.

    Field 4, and the comm field before it can contain spaces and parentheses —
    so split after the LAST ')' rather than on whitespace from the start.
    """
    try:
        stat = (_PROC / pid / "stat").read_text()
    except (OSError, PermissionError):
        return None
    try:
        after = stat[stat.rindex(")") + 1 :].split()
        return after[1]  # ppid, counting state as after[0]
    except (ValueError, IndexError):
        return None


def _is_descendant(pid: str, ancestor: str, *, max_depth: int = 12) -> bool:
    """Whether `pid` descends from `ancestor`.

    Needed because counting every `server.mcp_servers.*` process on the host
    attributes another Octopus instance's children to this one. A browser E2E
    run caught exactly that: the isolated instance under test spawned nothing,
    and the gauge still read 12 — the processes belonged to the production
    server running beside it. Walking the parent chain answers the question the
    gauge is actually asking, "how many did *I* start".

    Bounded depth so a pid-reuse cycle cannot spin.
    """
    seen = pid
    for _ in range(max_depth):
        parent = _ppid(seen)
        if parent is None or parent == "0":
            return False
        if parent == ancestor:
            return True
        seen = parent
    return False


def _memory_sample(metric: str, pss_kb: int | None) -> Sample:
    # The metrics table requires a value. The label makes this placeholder
    # explicitly unavailable, so queries cannot reuse an earlier reading.
    return (Sample(metric, 0.0, labels={"available": False}) if pss_kb is None
            else Sample(metric, round(pss_kb / 1024, 1)))


def collect() -> list[Sample]:
    """One reading of every gauge. Pure apart from reading /proc and stat()."""
    out: list[Sample] = []
    own = str(os.getpid())
    out.append(_memory_sample("server_pss_mb", _pss_kb(own)))

    # Engine CLI subprocesses, and any MCP sidecars still around. After B1 the
    # sidecar count should be zero; sampling it is how that stays true rather
    # than being true once.
    engines = sidecars = 0
    engine_pss: int | None = 0
    sidecar_pss: int | None = 0
    configured = settings.codex_app_server_binary
    configured_path = os.path.realpath(_which_with_fallback(configured) or configured)
    codex_path = _which_with_fallback("codex")
    codex_paths = {configured_path, *([os.path.realpath(codex_path)] if codex_path else [])}
    claude_path = _which_with_fallback("claude")
    claude_path = os.path.realpath(claude_path) if claude_path else None
    for entry in _PROC.iterdir():
        if not entry.name.isdigit():
            continue
        argv = _argv(entry.name)
        if not argv:
            continue
        # argv-structural, never a substring search: an engine process carries
        # the whole --mcp-config JSON on its command line, and that JSON names
        # server.mcp_servers, so a naive match counts engines as sidecars.
        if len(argv) >= 3 and argv[1] == "-m" and argv[2].startswith("server.mcp_servers"):
            if _is_descendant(entry.name, own):
                sidecars += 1
                pss = _pss_kb(entry.name)
                sidecar_pss = sidecar_pss + pss if sidecar_pss is not None and pss is not None else None
            continue
        base = argv[0].rsplit("/", 1)[-1]
        # A shebang launch exposes the interpreter followed by the resolved
        # script entry. Only that position can name a CLI; config/prompt text
        # elsewhere is never executable identity.
        script = len(argv) >= 3 and (base in {"node", "nodejs", "sh", "bash", "dash"}
                                    or re.fullmatch(r"python(?:\d+(?:\.\d+)?)?", base) is not None)
        cli_entry = os.path.realpath(argv[1] if script else argv[0])
        command_index = 2 if script else 1
        command = argv[command_index] if len(argv) > command_index else None
        codex = cli_entry in codex_paths or not script and base == "codex"
        claude = cli_entry == claude_path or not script and base == "claude"
        if (codex and command in ("app-server", "exec")) or (claude and command == "--print"):
            if _is_descendant(entry.name, own):
                engines += 1
                pss = _pss_kb(entry.name)
                engine_pss = engine_pss + pss if engine_pss is not None and pss is not None else None

    out.append(Sample("engine_process_count", float(engines)))
    out.append(_memory_sample("engine_pss_mb", engine_pss))
    out.append(Sample("mcp_sidecar_count", float(sidecars)))
    out.append(_memory_sample("mcp_sidecar_pss_mb", sidecar_pss))
    return out


def collect_db(db_path: str) -> list[Sample]:
    """Measure the database and its committed pages awaiting WAL checkpoint."""
    out: list[Sample] = []
    for suffix, metric in (("", "db_size_mb"), ("-wal", "db_wal_mb")):
        p = Path(db_path + suffix)
        with contextlib.suppress(OSError):
            out.append(Sample(metric, round(p.stat().st_size / 1e6, 2)))
    return out


async def run(db_path: str, held_process_count=None) -> None:
    """Sample forever. Cancelled by the caller on shutdown."""
    while True:
        try:
            await asyncio.sleep(SAMPLE_INTERVAL_SECONDS)
            for s in (*collect(), *collect_db(db_path)):
                sample(s)
            if held_process_count is not None:
                sample(Sample("held_process_count", float(held_process_count())))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("metrics sampler iteration failed", exc_info=True)
