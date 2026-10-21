"""Conservative local evidence that a previous managed writer has exited."""

import os
from pathlib import Path


def boot_identity() -> str | None:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return None


def process_group_exited(group: int | None, boot: str | None) -> bool:
    current = boot_identity()
    if current and boot and current != boot:
        return True
    if group is None or group <= 1 or current is None or current != boot:
        return False
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return False
    return False
