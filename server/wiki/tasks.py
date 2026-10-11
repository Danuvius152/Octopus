"""Short-lived task grants. Content and resumed history never grant access."""

import secrets
import shlex
from dataclasses import dataclass, field

from .store import WikiError


@dataclass(frozen=True)
class WikiTask:
    session_id: str
    user_id: str | None
    agent_id: str | None
    mode: str
    request: str = ""
    job_id: str | None = None
    writable: tuple[str, ...] = ()
    new_pages: bool = False
    reads: dict = field(default_factory=dict, compare=False)
    read_sizes: dict = field(default_factory=dict, compare=False)


class WikiTasks:
    def __init__(self):
        self.active: dict[str, WikiTask] = {}
        self.assignments: dict[str, dict] = {}

    def assign(self, session_id, job_id, paths):
        self.assignments[session_id] = {
            "job_id": job_id,
            "writable": tuple(paths),
            "new_pages": True,
        }

    def unassign(self, session_id):
        self.assignments.pop(session_id, None)

    def open(self, session, mode: str, request: str = "") -> str:
        if mode not in {"read", "maintain"}:
            raise WikiError("Unknown wiki task mode")
        token = secrets.token_hex(24)
        self.active[token] = WikiTask(
            session.id,
            session.user_id,
            session.agent_id,
            mode,
            request,
            **self.assignments.get(session.id, {}),
        )
        return token

    def close(self, token: str | None) -> None:
        if token:
            self.active.pop(token, None)

    def require(self, scope, *, write: bool = False, publish: bool = False) -> WikiTask:
        task = self.active.get(scope.wiki_task_id) if scope else None
        if task is None or (task.session_id, task.user_id) != (
            scope.session_id,
            scope.user_id,
        ):
            raise WikiError("An active explicit /wiki task is required", 403)
        if write and task.mode != "maintain":
            raise WikiError("Use /wiki maintain to ingest or organize materials", 403)
        if publish and task.mode != "maintain":
            raise WikiError("An explicit maintenance task is required", 403)
        return task

    def require_path(self, task, store, path):
        if task is None:
            return
        if not task.job_id:
            raise WikiError("Enqueue an explicitly scoped maintenance job first", 403)
        from .jobs import WikiJobs

        job = WikiJobs(task.user_id).get(task.job_id)
        if job["status"] != "running" or job["session_id"] != task.session_id:
            raise WikiError("This maintenance assignment is no longer active", 403)
        target = store.path(path)
        if path in task.writable:
            return
        from .store import asset_reason

        if (
            task.new_pages
            and path.startswith("Library/")
            and not target.exists()
            and not asset_reason(path)
        ):
            return
        raise WikiError(
            "This file is outside the maintenance write scope; related old notes are read-only",
            403,
        )

    def require_selection(self, task, paths):
        if task is None or not paths:
            return
        if task.job_id:
            raise WikiError("A running worker cannot expand its assigned files", 403)
        # Only the trusted request's first line selects existing files. Article
        # body, retrieved notes and model-supplied scope descriptions cannot.
        try:
            words = shlex.split(task.request.splitlines()[0])
        except (ValueError, IndexError):
            words = []
        if any(path not in words for path in paths):
            raise WikiError(
                "Name existing file paths explicitly in /wiki maintain, or select them in the app",
                403,
            )

    def record_read(self, task, document):
        if task is None or document.get("page"):
            return
        key = (document["path"], document["revision"])
        task.reads.setdefault(key, []).append((document["start"], document["end"]))
        task.read_sizes[key] = document["characters"]

    def require_read(self, task, store, path, expected_revision):
        if task is None:
            return
        document = store.read(path, length=1)
        key = (document["path"], expected_revision)
        covered = 0
        for start, end in sorted(task.reads.get(key, [])):
            if start > covered:
                break
            covered = max(covered, end)
        if key not in task.read_sizes or covered < task.read_sizes[key]:
            raise WikiError("Read the full current note before organizing it", 422)


def explicit_mode(message: str, selected: str | None = None) -> str | None:
    """Only call at trusted input boundaries, never on app context/reports."""
    words = message.strip().split(maxsplit=2)
    if words and words[0] == "/wiki":
        return words[1] if len(words) > 1 and words[1] == "maintain" else "read"
    return selected if selected in {"read", "maintain"} else None


tasks = WikiTasks()
