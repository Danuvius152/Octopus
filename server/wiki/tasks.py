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
    raw_reads: dict = field(default_factory=dict, compare=False)
    raw_read_sizes: dict = field(default_factory=dict, compare=False)


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

    def mode_for(self, session, requested: str | None) -> str | None:
        """Default registered accounts to autonomous use; retain explicit restrictions."""
        if requested == "off":
            return "read"  # Legacy clients may disable maintenance, never shared lookup.
        if requested in {"read", "maintain"}:
            return requested
        if requested not in {None, "auto"}:
            raise WikiError("Unknown wiki task mode")
        from .registry import registry
        if (session.user_id in registry._blocked
                or (session.user_id is None and not registry.legacy_enabled)
                or not (registry.state(session.user_id) / "config.json").is_file()):
            return None
        return "maintain" if session.id in self.assignments else "auto"

    def open(self, session, mode: str, request: str = "") -> str:
        if mode not in {"auto", "read", "maintain"}:
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

    def is_curator_worker(self, task):
        if task is None or task.mode != "maintain" or not task.job_id:
            return False
        from .jobs import WikiJobs
        from .registry import registry

        try:
            config = registry.config(task.user_id)
            job = WikiJobs(task.user_id).get(task.job_id)
        except WikiError:
            return False
        return task.agent_id == config["curator_agent_id"] and job["status"] == "running" and job["session_id"] == task.session_id

    def require(self, scope, *, write: bool = False, publish: bool = False) -> WikiTask:
        task = self.active.get(scope.wiki_task_id) if scope else None
        if task is None or (task.session_id, task.user_id) != (
            scope.session_id,
            scope.user_id,
        ):
            raise WikiError("Wiki access is unavailable outside the active authorized turn", 403)
        if write and task.mode not in {"auto", "maintain"}:
            raise WikiError("This turn is limited to read-only Wiki access", 403)
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
        from .formats import require_markdown_maintenance

        target = store.path(path)
        require_markdown_maintenance(path)
        if path in task.writable:
            from .store import is_maintainable_text

            if not is_maintainable_text(path):
                raise WikiError("This is an excluded maintenance asset", 422)
            selected = next((item for item in job["items"] if item["path"] == path), None)
            paired = next((item for item in job["items"] if item["path"] + ".md" == path), None)
            expected_id = selected["doc"] if selected else paired.get("paired_doc") if paired else None
            if not selected and not paired:
                raise WikiError("This path has no current document assignment", 403)
            current_id = store.read(path, length=1)["id"] if target.is_file() else None
            if current_id != expected_id:
                raise WikiError("The assigned document identity was deleted or replaced", 403)
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
        if task.mode == "auto":
            return  # The agent declares exact paths in the maintenance request.
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
        key = (document["id"], document["path"], document["revision"])
        task.reads.setdefault(key, []).append((document["start"], document["end"]))
        task.read_sizes[key] = document["characters"]

    def require_read(self, task, store, path, expected_revision):
        if task is None:
            return
        document = store.read(path, length=1)
        key = (document["id"], document["path"], expected_revision)
        covered = 0
        for start, end in sorted(task.reads.get(key, [])):
            if start > covered:
                break
            covered = max(covered, end)
        raw_covered = self.coverage(task.raw_reads.get(key, []), 0)
        raw_complete = key in task.raw_read_sizes and raw_covered >= task.raw_read_sizes[key]
        if not raw_complete and (key not in task.read_sizes or covered < task.read_sizes[key]):
            raise WikiError("Read the full current note before organizing it", 422)

    @staticmethod
    def coverage(ranges, start):
        covered = start
        for first, last in sorted(ranges):
            if first > covered:
                break
            covered = max(covered, last)
        return covered

    def record_raw_read(self, task, document, start, end, size):
        if task is not None:
            key = (document["id"], document["path"], document["revision"])
            task.raw_reads.setdefault(key, []).append((start, end))
            task.raw_read_sizes[key] = size

    def require_raw_read(self, task, document, start, end):
        if task is None:
            return
        key = (document["id"], document["path"], document["revision"])
        if key not in task.raw_read_sizes or self.coverage(task.raw_reads.get(key, []), start) < end:
            raise WikiError("Read the current raw passage and context before revising it", 422)

    def require_source_read(self, task, store, source):
        from .links import source_span

        document = store.read(source["doc"], length=1, expected_revision=source["revision"])
        with store.connect() as db:
            text = db.execute("SELECT text FROM documents WHERE id=?", (document["id"],)).fetchone()[0]
        span = source_span(text, source["location"])
        if span is None:
            raise WikiError("A relation needs an existing line, heading or block locator", 422)
        if task is None:
            return
        covered = span[0]
        for start, end in sorted(task.reads.get((document["id"], document["path"], document["revision"]), [])):
            if start > covered:
                break
            covered = max(covered, end)
        if covered < span[1]:
            raise WikiError("Read the relation's cited current passage before adding its link", 422)


def explicit_mode(message: str, selected: str | None = None) -> str | None:
    """Only call at trusted input boundaries, never on app context/reports."""
    # Cancelling maintenance must win over command text. Shared lookup is
    # always available; old clients' off value now means read-only.
    if selected in {"off", "read"}:
        return "read"
    words = message.strip().split(maxsplit=2)
    if words and words[0] == "/wiki":
        return words[1] if len(words) > 1 and words[1] == "maintain" else "read"
    return selected if selected in {"read", "maintain"} else None


tasks = WikiTasks()
