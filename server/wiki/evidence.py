"""Capture authorized job metadata without returning it through shell arguments."""

import hashlib
import json
import os
import tempfile
import uuid
from pathlib import Path

from ..workspace import paths_for
from .registry import registry
from .store import WikiError
from .tasks import tasks

INLINE_JOB_BYTES = 48 * 1024


def capture_jobs(scope, result: dict) -> dict:
    task = tasks.require(scope)
    workspace = paths_for(task.user_id).workspace.resolve()
    directory = workspace / "wiki-evidence"
    if not directory.resolve().is_relative_to(workspace):
        raise WikiError("Wiki evidence directory escapes the account workspace", 403)
    vault = registry.store(task.user_id).root
    if directory.resolve().is_relative_to(vault):
        directory = registry.state(task.user_id).resolve() / "tool-evidence"
        if not directory.resolve().is_relative_to(directory.parent) or directory.resolve().is_relative_to(vault):
            raise WikiError("Wiki evidence must be stored outside the canonical vault", 409)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8")
    path = directory / f"jobs-{uuid.uuid4().hex}.json"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, mode="wb", delete=False) as handle:
            temporary = handle.name
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
    return {"path": str(path), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "format": "application/json"}
