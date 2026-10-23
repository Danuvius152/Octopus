"""Test-only process grace periods, shared with fresh-process failure probes."""

import asyncio


def shorten_harness_shutdown(monkeypatch):
    """Keep real signals and joins; shorten only Harness process-exit waits."""
    from server.harness import run

    class ShortShutdownAsyncio:
        def __getattr__(self, name):
            # Later spawn and clock hooks must remain visible to the Harness.
            return getattr(asyncio, name)

        async def wait_for(self, future, timeout):
            if timeout == 2.0 and getattr(future, "cr_code", None) is asyncio.subprocess.Process.wait.__code__:
                timeout = 0.05
            return await asyncio.wait_for(future, timeout)

    monkeypatch.setattr(run, "asyncio", ShortShutdownAsyncio())
