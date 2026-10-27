"""Test-only process grace periods, shared with fresh-process failure probes."""

import asyncio


def shorten_harness_shutdown(monkeypatch):
    """Keep real signals and joins; shorten only Harness process-exit waits."""
    from server.harness import harness, run

    wait_group_exit = run.wait_group_exit

    async def short_group_exit(pgid, *, timeout, after_kill=False):
        return await wait_group_exit(
            pgid, timeout=min(timeout, 0.5 if after_kill else 0.1), after_kill=after_kill,
        )

    class ShortShutdownAsyncio:
        def __getattr__(self, name):
            # Later spawn and clock hooks must remain visible to the Harness.
            return getattr(asyncio, name)

        async def wait_for(self, future, timeout):
            if timeout == 2.0 and getattr(future, "cr_code", None) is asyncio.subprocess.Process.wait.__code__:
                timeout = 0.05
            return await asyncio.wait_for(future, timeout)

        async def wait(self, futures, *, timeout=None, return_when=asyncio.ALL_COMPLETED):
            if timeout == 2.0 and all(
                isinstance(task, asyncio.Task) and task.get_name() == "one-shot-process-retirement"
                for task in futures
            ):
                timeout = 0.05
            return await asyncio.wait(futures, timeout=timeout, return_when=return_when)

    clock = ShortShutdownAsyncio()
    monkeypatch.setattr(run, "asyncio", clock)
    monkeypatch.setattr(harness, "asyncio", clock)
    monkeypatch.setattr(run, "wait_group_exit", short_group_exit)
