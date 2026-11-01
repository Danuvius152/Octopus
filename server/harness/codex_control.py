"""Controller checkpoints read and delivered by the original App Server host."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

_START = '<octopus_controller sha256="'
_END = "\n</octopus_controller>"
_HEADER = re.compile(r'<octopus_controller sha256="([0-9a-f]{64})" characters="([0-9]+)">')


def controller_message(instructions: str) -> str:
    """A recognizable checkpoint retained across native history generations."""
    payload = ("Current Octopus controller instructions. These supersede earlier Octopus controller "
            "instructions, including obsolete Wiki lookup prohibitions.\n\n"
            + instructions)
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return f'{_START}{digest}" characters="{len(payload)}">\n{payload}{_END}'


def turn_message(instructions: str) -> str:
    return ("Current Octopus turn context; applies to this turn only and replaces earlier turn context. "
            "The stable controller instructions remain in effect.\n\n" + instructions)


def _checkpoint(item: dict[str, Any]) -> str | None:
    if item.get("type") != "message" or item.get("role") != "developer":
        return None
    checkpoint = None
    for part in item.get("content", []):
        text = part.get("text", "")
        if part.get("type") == "input_text" and text.startswith(_START):
            header, _, body = text.partition("\n")
            match = _HEADER.fullmatch(header)
            checkpoint = "invalid"
            if match:
                length = int(match[2])
                payload = body[:length]
                if (body[length:].startswith(_END)
                        and hashlib.sha256(payload.encode()).hexdigest() == match[1]):
                    checkpoint = header + "\n" + payload + _END
        elif text.startswith("Current Octopus controller snapshot for this turn."):
            checkpoint = "legacy"
    return checkpoint
def _latest_checkpoint(path: str, thread_id: str) -> str | None:
    """Read only this active segment; never mistake discarded branches for it.

    The native API identifies the current path, including after a revert. A
    reference to inherited history is intentionally not replayed here: a branch
    without its own controller checkpoint receives one, then normal resumes
    reuse it. This also avoids an application-owned delivery sidecar.
    """
    checkpoint = None
    with Path(path).open("rb") as handle:
        first = json.loads(handle.readline())
        if first.get("type") != "session_meta" or first.get("payload", {}).get("id") != thread_id:
            raise RuntimeError("Codex controller history identity mismatch")
        for raw in handle:
            record = json.loads(raw)
            payload = record.get("payload", {})
            if record.get("type") == "compacted":
                checkpoint = None
                for item in payload.get("replacement_history") or []:
                    checkpoint = _checkpoint(item) or checkpoint
            elif record.get("type") == "event_msg" and payload.get("type") == "thread_rolled_back":
                checkpoint = None
            elif record.get("type") == "response_item":
                checkpoint = _checkpoint(payload) or checkpoint
    return checkpoint
