"""Read complete byte frames from an append-only, possibly still active file."""

import json
from pathlib import Path


def read_frames(path: Path, *, finished: bool = False) -> list[dict]:
    if not path.exists():
        return []
    # A snapshot EOF is not a frame boundary. The file retains the suffix for
    # the next snapshot, including UTF-8 code points split across writes.
    *lines, suffix = path.read_bytes().split(b"\n")
    frames = [json.loads(line.decode("utf-8")) for line in lines]
    if finished and suffix:
        raise ValueError(f"Incomplete JSONL frame in {path}: {suffix!r}")
    return frames
