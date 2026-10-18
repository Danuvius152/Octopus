"""Short-lived task grants. Content and resumed history never grant access."""

from __future__ import annotations

import secrets
import shlex
from dataclasses import dataclass, field, replace

from ..mcp_identity import McpScope
from .store import WikiError


@dataclass(frozen=True)
class WikiTask:
    session_id: str
    user_id: str | None
    agent_id: str | None
    mode: str
    request: str = ""
    job_id: str | None = None
    document_ids: tuple[str, ...] = ()
    reservation_ids: tuple[str, ...] = ()
    registration: dict | None = field(default=None, compare=False)
    call_scope: McpScope | None = field(default=None, compare=False)
    active_task: WikiTask | None = field(default=None, compare=False, repr=False)
    reads: dict = field(default_factory=dict, compare=False)
    read_sizes: dict = field(default_factory=dict, compare=False)
    raw_reads: dict = field(default_factory=dict, compare=False)
    raw_read_sizes: dict = field(default_factory=dict, compare=False)
    request_id: str = ""
    origin: str = "user"
    app_id: str | None = None
    assigned_request_id: str | None = None
    assigned_attempt: int | None = None


class WikiTasks:
    def __init__(self):
        self.active: dict[str, WikiTask] = {}
        self.assignments: dict[str, dict] = {}

    def assign(self, session_id, job_id, items, *, registration=None, request_id=None, attempt=None):
        """Capture the host-selected batch, independently of later path moves."""
        items = tuple(items)
        self.assignments[session_id] = {
            "job_id": job_id,
            "document_ids": tuple(dict.fromkeys(item["doc"] for item in items if item.get("doc"))),
            "reservation_ids": tuple(dict.fromkeys(item["reservation_id"] for item in items if item.get("reservation_id"))),
            "registration": dict(registration) if registration is not None else None,
            "assigned_request_id": request_id,
            "assigned_attempt": attempt,
        }

    def unassign(self, session_id):
        self.assignments.pop(session_id, None)

    def mode_for(self, session, requested: str | None, *, request_id=None) -> str | None:
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
        assignment = self.assignments.get(session.id)
        assigned = assignment and assignment.get("assigned_request_id") in {None, request_id}
        return "maintain" if assigned else "auto"

    def open(self, session, mode: str, request: str = "", *, request_id: str = "") -> str:
        if mode not in {"auto", "read", "maintain"}:
            raise WikiError("Unknown wiki task mode")
        token = secrets.token_hex(24)
        assignment = self.assignments.get(session.id, {})
        if assignment.get("assigned_request_id") not in {None, request_id}:
            assignment = {}
        self.active[token] = WikiTask(
            session.id,
            session.user_id,
            session.agent_id,
            mode,
            request,
            request_id=request_id,
            origin=getattr(session, "origin", "user"),
            app_id=getattr(session, "app_id", None),
            **assignment,
        )
        return token

    def close(self, token: str | None) -> None:
        if token:
            self.active.pop(token, None)

    def is_curator_worker(self, task):
        if task is None or task.mode != "maintain" or not task.job_id:
            return False
        try:
            self.current_grant(task, strict=True)
        except WikiError:
            return False
        return True

    def current_grant(self, task, *, store=None, strict=False, include_selection=False):
        if task is None or not task.job_id:
            return None
        from . import grants
        from .registry import registry

        try:
            store = store or registry.store(task.user_id)
            with store.connect() as db:
                db.execute("BEGIN")
                grants.active(db, task, store, require_scope=task.call_scope is not None)
                grant = grants.view(db, task.job_id, document_ids=task.document_ids, reservation_ids=task.reservation_ids)
                if grant is not None and include_selection:
                    selected = [row[0] for row in db.execute(
                        "SELECT DISTINCT selection_id FROM job_selection_evidence WHERE job_id=? ORDER BY selection_id LIMIT 9", (task.job_id,))]
                    grant |= {"selection_ids": selected[:8], "selection_ids_truncated": len(selected) > 8}
                return grant
        except WikiError:
            if strict:
                raise
            return None

    @staticmethod
    def call(task, scope):
        return replace(task, call_scope=scope, active_task=task) if task.job_id else task

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
        if (write or publish) and task.registration is not None:
            from .registry import registry
            if task.registration != registry.config(task.user_id):
                raise WikiError("This maintenance assignment's registration has changed", 403)
        return task

    def require_path(self, task, store, path):
        if task is None:
            return
        if not task.job_id:
            raise WikiError("Enqueue an explicitly scoped maintenance job first", 403)
        from . import grants
        from .formats import require_markdown_maintenance

        target = store.path(path)
        require_markdown_maintenance(path)
        exists = target.is_file()
        if exists:
            store.read(path, length=1)
        with store.connect() as db:
            db.execute("BEGIN")
            grants.authorize(db, task, store, path, create=not exists)

    def require_selection(self, task, paths, store=None, *, checked=False, db=None):
        if task is None or not paths:
            return
        if task.job_id:
            raise WikiError("A running worker cannot expand its assigned files", 403)
        if task.mode == "auto":
            return  # The agent declares exact paths in the maintenance request.
        if checked and daily_command(task.request):
            from .formats import is_markdown_maintenance

            if all(path.startswith("Library/") and is_markdown_maintenance(path) for path in paths):
                return  # Eligibility and the observation are checked under the admission lock.
            raise WikiError("Daily maintenance is limited to current Library Markdown changes", 403)
        # Only the trusted request's first line selects existing files. Article
        # body, retrieved notes and model-supplied scope descriptions cannot.
        first_line = next(iter(task.request.splitlines()), "")
        _, selection = wiki_command(first_line)
        try:
            words = shlex.split(selection)
        except ValueError:
            words = []
        if all(path in words for path in paths):
            return
        if store is not None:
            from .maintenance_state import selected_document

            def permitted(db):
                whole = selected_document(db, selection.strip()) if selection.strip() else None
                selectors = [selection.strip()] if whole is not None else words
                selected = set()
                for selector in selectors:
                    if (row := selected_document(db, selector)) is not None:
                        selected.add(row["id"])
                if all((row := selected_document(db, path)) is not None and row["id"] in selected for path in paths):
                    return True
                return False

            if db is not None:
                if permitted(db):
                    return
            else:
                with store.connect() as connection:
                    if permitted(connection):
                        return
        raise WikiError("Select an unambiguous current file explicitly, or use a checked daily maintenance observation", 403)

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


def wiki_command(message: str) -> tuple[str | None, str]:
    """Parse only a leading human command; preserve its remaining text."""
    text = message.strip()
    words = text.split(maxsplit=2)
    if words and words[0] == "/wiki维护":
        return "maintain", text.split(maxsplit=1)[1] if len(words) > 1 else ""
    if words and words[0] == "/wiki":
        if len(words) > 1 and words[1] in {"maintain", "维护"}:
            return "maintain", words[2] if len(words) > 2 else ""
        return "read", text.split(maxsplit=1)[1] if len(words) > 1 else ""
    return None, message


def daily_command(message: str) -> bool:
    mode, body = wiki_command(message)
    return mode == "maintain" and not body.strip()


def command_prompt(message: str, mode: str | None) -> str:
    command, body = wiki_command(message)
    if command is None or mode not in {"auto", "read", "maintain"}:
        return message
    if body:
        return body
    return "执行日常 Wiki 增量维护。" if command == mode == "maintain" else "说明当前 Wiki 查询范围。"


def explicit_mode(message: str, selected: str | None = None) -> str | None:
    """Only call at trusted input boundaries, never on app context/reports."""
    # Cancelling maintenance must win over command text. Shared lookup is
    # always available; old clients' off value now means read-only.
    if selected in {"off", "read"}:
        return "read"
    mode, _ = wiki_command(message)
    if mode:
        return mode
    return selected if selected in {"read", "maintain"} else None


tasks = WikiTasks()
