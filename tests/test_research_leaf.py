"""Leaf success requires a provider terminal, even when partial JSON parses."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from dataclasses import replace

import pytest

from server.harness import Harness, get_harness
from server.research.leaf import LeafRetirementError, run_web_leaf


@pytest.mark.parametrize("ending", ["zero_exit", "error_exit", "success"])
async def test_leaf_eof_keeps_partial_but_requires_success_terminal(tmp_path, ending):
    partial = '[{"claim":"partial claim","url":"https://example.test"}]'
    records = [{"type": "assistant", "message": {"content": [{"type": "text", "text": partial}]}}]
    if ending == "success":
        records.append({"type": "result", "subtype": "success", "result": partial})
    script = "import sys; " + "; ".join(f"print({json.dumps(json.dumps(record))}, flush=True)" for record in records)
    if ending == "error_exit":
        script += "; sys.exit(7)"
    profile = replace(
        get_harness("claude-code").profile, binary=sys.executable,
        build_turn_argv=lambda ctx: ([sys.executable, "-c", script], {"cwd": ctx.working_dir}),
        activity_observer=None,
    )
    result = await run_web_leaf(
        Harness(profile), prompt="Read the synthetic provider", working_dir=str(tmp_path), credential=None, model=None,
    )
    assert "partial claim" in result.text
    assert (result.error is None) is (ending == "success")


async def test_successful_leaf_retains_original_run_when_retirement_fails(tmp_path, monkeypatch):
    processes = []
    spawn = asyncio.create_subprocess_exec
    killpg = os.killpg

    async def capture_spawn(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        processes.append(process)
        return process

    def refuse_signal(pgid, sig):
        raise OSError("original process retirement refused")

    records = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "original result"}]}},
        {"type": "result", "subtype": "success", "result": "original result"},
    ]
    script = "import time; " + "; ".join(f"print({json.dumps(json.dumps(record))}, flush=True)" for record in records)
    script += "; time.sleep(60)"
    profile = replace(
        get_harness("claude-code").profile, binary=sys.executable, activity_observer=None,
        build_turn_argv=lambda ctx: ([sys.executable, "-c", script], {"cwd": ctx.working_dir}),
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture_spawn)
    monkeypatch.setattr(os, "killpg", refuse_signal)
    try:
        with pytest.raises(LeafRetirementError) as caught:
            await run_web_leaf(Harness(profile), prompt="q", working_dir=str(tmp_path), credential=None, model=None)
        assert caught.value.result.text == "original result"
        assert caught.value.result.error is None
        assert caught.value.run.run_lease_id
        assert "original process retirement refused" in str(caught.value.__cause__)
    finally:
        for process in processes:
            if process.returncode is None:
                killpg(process.pid, signal.SIGKILL)
            await asyncio.wait_for(process.wait(), 3)
