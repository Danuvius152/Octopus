"""Exit evidence for process groups owned by this installation.

The spawning caller captures the group ID (the child PID with
start_new_session=True) and retains ownership until these waits succeed.
Only ESRCH proves absence while members can fork. After SIGKILL, procfs can
also distinguish inert orphan zombies from processes that can still write.
"""

import asyncio
import os
from pathlib import Path


def group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def group_has_live_members_after_kill(pgid: int) -> bool:
    if not group_exists(pgid):
        return False
    proc = Path("/proc")
    if not proc.is_dir():
        return True
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if os.getpgid(int(entry.name)) != pgid:
                continue
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            group, state, threads = int(fields[2]), fields[0], int(fields[17])
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (OSError, IndexError, ValueError):
            return True
        if group == pgid and (state not in {"Z", "X"} or threads != 1):
            return True
    return False


async def wait_group_exit(pgid: int, *, timeout: float, after_kill: bool = False) -> None:
    """Raise on unconfirmed retirement; a caller must retain its owner then."""
    exists = group_has_live_members_after_kill if after_kill else group_exists
    async with asyncio.timeout(timeout):
        while await asyncio.to_thread(exists, pgid):
            await asyncio.sleep(0.01)
