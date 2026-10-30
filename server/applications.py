"""ApplicationManager — agent-built web apps (docs/implemented/applications.md).

An **application** is a directory of static files that one of the user's
agents wrote, served back under ``/apps/{id}/`` and rendered in the main pane
like a browser tab. The row owns three things:

  * the directory (allocated under ``settings.applications_dir``),
  * the *build session* — a normal ``Session`` with ``origin='application'``
    whose turns write the files (the same "reuse the session concept" trick
    delegations use for children),
  * a ``building | ready | failed`` status, derived from whether the
    entrypoint exists when a build turn ends.

Status is observed from the original build request's committed completion.
Broadcasts report progress; a failed notification does not erase the build
result. Requests typed straight into the build session use the same
completion observer as requests from the application view.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any
from weakref import WeakValueDictionary

from .aio import complete_cleanup
from .config import settings
from .models import SessionStatus
from .sessions.submission import RequestCompletion
from .workspace import paths_for

if TYPE_CHECKING:
    from .database import Database
    from .session_manager import SessionManager

logger = logging.getLogger(__name__)

# Status values. `building` covers "a build turn is in flight"; the terminal
# pair is decided by whether the entrypoint file landed.
STATUS_BUILDING = "building"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

DEFAULT_ENTRYPOINT = "index.html"


class ApplicationError(Exception):
    """Surface-level error carrying an HTTP status for the REST layer."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def app_scope_token(app_id: str) -> str:
    """The credential an application's *backend* uses to reach Octopus.

    Derived, not stored: ``HMAC-SHA256(master_key, "app:<id>")``. It opens
    exactly one application's ``/apps/<id>/…`` surface and nothing else, which
    is what lets a backend hold a conversation with an agent without ever
    seeing a user's credential — the promise ``script_env`` makes by refusing
    to inherit the server's environment (application-backends.md §4).

    Keyed on the **master key** since accounts (multi-tenancy.md §7). It was
    keyed on `OCTOPUS_AUTH_TOKEN`, which is being retired — and more to the
    point, a token that belongs to a person is the wrong lifetime for a
    credential a *server process* holds: every running application backend
    would have been locked out the moment somebody changed their password.

    Still derived, so there is no new secret to store, back up or leak, and
    rotating the master key rotates every app's token with it.
    (app-agent-access.md §4)
    """
    from .crypto import master_key

    return hmac.new(
        master_key().encode("utf-8"),
        f"app:{app_id}".encode(),
        hashlib.sha256,
    ).hexdigest()


def is_app_scope_token(app_id: str, candidate: str | None) -> bool:
    """Constant-time check of a presented app token against `app_id`'s."""
    if not candidate:
        return False
    return hmac.compare_digest(app_scope_token(app_id), candidate)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def applications_root() -> str:
    """The managed root every application directory lives under. Expanded at
    call time (not import time) so tests and the e2e suite can repoint
    `OCTOPUS_APPLICATIONS_DIR` after Settings has been constructed."""
    return os.path.abspath(os.path.expanduser(settings.applications_dir))


def slugify(name: str) -> str:
    """Filesystem-safe, human-readable stem for an application directory."""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return slug[:48] or "app"


def allocate_app_dir(name: str, user_id: str | None = None) -> str:
    """Pick an unused directory for `name` under the managed root.

    The slug is the readable part; collisions get a `-2`, `-3`, … suffix so
    two applications never share a directory. The result is stored on the row,
    which is why renaming an application later never has to move files — and
    why applications that already exist keep working when this root moves: the
    row says where they are, not this function.

    Under the owner's root once there is one (multi-tenancy.md §6), which also
    means two accounts' apps cannot collide on a slug.
    """
    root = (
        str(paths_for(user_id).applications) if user_id else applications_root()
    )
    os.makedirs(root, exist_ok=True)
    stem = slugify(name)
    candidate = os.path.join(root, stem)
    n = 2
    while os.path.exists(candidate):
        candidate = os.path.join(root, f"{stem}-{n}")
        n += 1
    return candidate


def data_dir_for(app_dir: str) -> str:
    """The app's own state, as a SIBLING of the code directory.

    A sibling and not a subdirectory: a rebuild rewrites the code directory,
    and anything the running app owns — a cloned repo, a database file,
    uploads — must survive that (application-backends.md §3). Never served
    statically.
    """
    return app_dir.rstrip("/") + ".data"


def runtime_dir_for(app_dir: str) -> str:
    """Where `install.sh` puts dependencies.

    Separate from the data directory because a venv or node_modules is
    *derived*: this directory can be deleted to force a clean reinstall without
    risking anything the user cares about, and a backup of the data directory
    doesn't carry a few hundred megabytes of packages.
    """
    return app_dir.rstrip("/") + ".runtime"


def backend_script(app_dir: str, name: str) -> str | None:
    """Path to `install.sh` / `start.sh` if the app ships an executable one.

    Presence of `start.sh` IS the declaration that an application has a
    backend — there is no manifest (application-backends.md §2). A file that
    isn't executable is reported as missing rather than run, so a
    `chmod`-forgotten script fails as "no backend" instead of as a confusing
    exec error.
    """
    if name not in ("install.sh", "start.sh"):
        raise ValueError(f"not a backend script: {name}")
    path = resolve_within(app_dir, name)
    if path is None or not os.path.isfile(path):
        return None
    return path if os.access(path, os.X_OK) else None


def has_backend(app_dir: str) -> bool:
    return backend_script(app_dir, "start.sh") is not None


def is_inside_root(path: str, user_id: str | None = None) -> bool:
    """True iff `path` resolves inside the managed applications root. Guards
    the delete path — a hand-edited `app_dir` must not be able to make us
    `rmtree` something outside our own tree."""
    root = os.path.realpath(str(paths_for(user_id).applications) if user_id else applications_root())
    target = os.path.realpath(path)
    return target != root and os.path.commonpath([root, target]) == root


def is_safe_relative_path(rel_path: str) -> bool:
    """True iff `rel_path` is a relative path that can't climb out of its own
    directory. Used to validate a stored `entrypoint` before it ever reaches
    the filesystem."""
    if not rel_path or os.path.isabs(rel_path) or rel_path.startswith("/"):
        return False
    parts = PurePosixPath(rel_path).parts
    return bool(parts) and ".." not in parts


def resolve_within(app_dir: str, rel_path: str) -> str | None:
    """Resolve `rel_path` inside `app_dir`, or None if it escapes.

    Used by the static-file route. Returns None for absolute paths, `..`
    traversal, and symlinks pointing out of the directory — the caller turns
    that into a 404 rather than a 403, so probing can't confirm what exists
    outside the app.
    """
    base = os.path.realpath(app_dir)
    target = os.path.realpath(os.path.join(base, rel_path.lstrip("/")))
    if target != base and os.path.commonpath([base, target]) != base:
        return None
    return target


# Icon discovery (feature request: "let an Application supply its own icon").
#
# Two sources, because neither alone covers how apps actually ship a logo:
# a conventional file at the root (what `compose_build_prompt` now asks agents
# to write), and the `<link rel="icon">` the app already declares in its own
# entry point. Real apps overwhelmingly do the second — one points at a file in
# a subdirectory, another inlines a `data:` SVG — and a convention-only
# implementation would light up neither.
_ICON_CANDIDATES = (
    "icon.svg",
    "icon.png",
    "favicon.svg",
    "favicon.png",
    "apple-touch-icon.png",
    "favicon.ico",
)

# A file this big isn't a sidebar icon. Skipping it keeps a stray large asset
# from being fetched on every render.
_MAX_ICON_BYTES = 512 * 1024

# A `data:` icon is stored inline on the row and shipped in every API response,
# so it has to stay small. A real inline SVG favicon is well under a kilobyte.
_MAX_DATA_ICON_CHARS = 64 * 1024

_ICON_LINK_RE = re.compile(
    r"""<link\b[^>]*\brel\s*=\s*["'][^"']*\bicon\b[^"']*["'][^>]*>""",
    re.IGNORECASE,
)
# Capture the opening quote and stop only at the MATCHING one: a data: URI
# routinely contains the other quote character (`<svg xmlns='…'>` inside a
# double-quoted href), and a naive [^"']+ truncates it mid-payload.
_HREF_RE = re.compile(r"""\bhref\s*=\s*(["'])(.*?)\1""", re.IGNORECASE | re.DOTALL)


def _usable_icon_file(app_dir: str, rel_path: str) -> str | None:
    """`rel_path` if it's a sane icon file inside `app_dir`, else None.

    Goes through `resolve_within`, so `..` and symlinks pointing outside are
    rejected exactly as they are for the static route — an icon is just another
    file the app asked us to serve.
    """
    target = resolve_within(app_dir, rel_path)
    if target is None or not os.path.isfile(target):
        return None
    try:
        if os.path.getsize(target) > _MAX_ICON_BYTES:
            logger.info("icon candidate %s is too large; skipping", rel_path)
            return None
    except OSError:
        return None
    return rel_path.lstrip("/")


def discover_icon_src(app_dir: str, entrypoint: str) -> str | None:
    """What the UI should put in an `<img src>` for this app, or None.

    Returns either a path relative to the app directory (served through
    `/apps/{id}/…`) or a `data:` URI to use verbatim. Server-owned: callers
    never pass this in, so a rebuild can refresh it and a deleted icon clears
    it, without ever touching the emoji a user typed.
    """
    for candidate in _ICON_CANDIDATES:
        found = _usable_icon_file(app_dir, candidate)
        if found:
            return found

    # Fall back to whatever the page itself declares.
    entry = resolve_within(app_dir, entrypoint)
    if entry is None or not os.path.isfile(entry):
        return None
    try:
        # The link lives in <head>; no need to read a large document.
        with open(entry, encoding="utf-8", errors="replace") as fh:
            head = fh.read(64 * 1024)
    except OSError:
        return None

    for tag in _ICON_LINK_RE.findall(head):
        match = _HREF_RE.search(tag)
        if not match:
            continue
        href = match.group(2).strip()
        if not href:
            continue
        low = href.lower()
        if low.startswith("data:"):
            # Inline SVG/PNG favicons are common and are the app's real mark.
            # Rendered through <img src>, so scripts inside an SVG don't run.
            if not low.startswith("data:image/"):
                continue
            if len(href) > _MAX_DATA_ICON_CHARS:
                logger.info("inline icon for %s is too large; skipping", app_dir)
                continue
            return href
        if "://" in low or low.startswith("//"):
            continue  # remote icon: not ours to serve, and not always reachable
        found = _usable_icon_file(app_dir, href.split("?")[0].split("#")[0])
        if found:
            return found
    return None


class ApplicationManager:
    """App-lifetime singleton; bound in main.py's lifespan."""

    BROADCAST_KEY = "application-manager"

    def __init__(self) -> None:
        self.session_mgr: SessionManager | None = None
        self.db: Database | None = None
        # Only projection writers share this short-lived coordination. No
        # request result or application status is copied into this map.
        self._build_updates: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._deletions: dict[str, tuple[bool, asyncio.Task[None]]] = {}

    def bind(self, session_mgr: SessionManager, db: Database) -> None:
        self.session_mgr = session_mgr
        self.db = db
        db.add_close_hook(self._join_deletions)
        session_mgr.on_broadcast(self.BROADCAST_KEY, self._on_broadcast)
        session_mgr.on_request_complete(self.BROADCAST_KEY, self._on_completion)

    def shutdown(self) -> None:
        if self.session_mgr is not None:
            self.session_mgr.remove_broadcast(self.BROADCAST_KEY)
            self.session_mgr.remove_request_complete(self.BROADCAST_KEY)

    def _require_db(self) -> Database:
        if self.db is None:
            raise ApplicationError("ApplicationManager not bound", status_code=500)
        return self.db

    async def _join_deletions(self) -> None:
        tasks = [task for _, task in self._deletions.values() if not task.done()]
        if tasks:
            await asyncio.gather(*(asyncio.shield(task) for task in tasks), return_exceptions=True)

    @staticmethod
    def require_writable(row: dict[str, Any]) -> None:
        if row.get("deletion") is not None:
            raise ApplicationError("Application deletion is unfinished; retry deletion", status_code=409)

    # ------------------------------------------------------------------ reads

    async def list_applications(
        self,
        *,
        include_archived: bool = False,
        only_archived: bool = False,
        user_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return await self._require_db().load_applications(
            include_archived=include_archived,
            only_archived=only_archived,
            user_id=user_id,
        )

    async def get_application(
        self, app_id: str, user_id: str | None = None
    ) -> dict[str, Any]:
        """One application, or a 404 — which is also the answer for one that
        belongs to another account (multi-tenancy.md §5).

        Every write below opens with this call, so passing the caller's account
        here is what scopes them all; a method that forgot would be a method
        that does not read its own row.
        """
        row = await self._require_db().get_application(app_id, user_id)
        if row is None:
            raise ApplicationError("Application not found", status_code=404)
        return row

    # ----------------------------------------------------------------- create

    async def create_application(
        self,
        *,
        name: str,
        description: str = "",
        agent_id: str,
        icon: str | None = None,
        instructions: str = "",
        entrypoint: str = DEFAULT_ENTRYPOINT,
    ) -> dict[str, Any]:
        """Create the directory + row, open the build session under `agent_id`,
        and fire the build brief. Returns the row immediately — the app is
        `building` until the agent's turn ends (applications.md §4)."""
        db = self._require_db()
        if self.session_mgr is None:
            raise ApplicationError("ApplicationManager not bound", status_code=500)

        name = (name or "").strip()
        if not name:
            raise ApplicationError("Application name is required")
        # The agent is resolved first because the owner comes from it — whoever
        # owns the agent that builds the app owns the app (multi-tenancy.md §5)
        # — and the name check below is "taken for *this account*", not for the
        # whole box: two people must both be able to have an app called Notes.
        agent = await db.get_agent(agent_id) if agent_id else None
        if agent is None:
            raise ApplicationError("Agent not found", status_code=404)
        owner_id = agent.get("user_id")

        if await db.get_application_by_name(name, user_id=owner_id) is not None:
            raise ApplicationError(
                f"An application named {name!r} already exists", status_code=409
            )
        description = (description or "").strip()
        if not description:
            raise ApplicationError("Application description is required")

        entrypoint = (entrypoint or DEFAULT_ENTRYPOINT).strip()
        if not is_safe_relative_path(entrypoint):
            raise ApplicationError("entrypoint must be a path inside the app")

        app_id = uuid.uuid4().hex[:12]
        app_dir = allocate_app_dir(name, owner_id)
        os.makedirs(app_dir, exist_ok=True)
        now = _now()
        async with self._build_updates.setdefault(app_id, asyncio.Lock()):
            await db.save_application(
                app_id=app_id,
                name=name,
                description=description,
                icon=icon,
                agent_id=agent_id,
                session_id=None,
                app_dir=app_dir,
                entrypoint=entrypoint,
                status=STATUS_BUILDING,
                created_at=now,
                updated_at=now,
                user_id=owner_id,
            )

            session = await self.session_mgr.create_session(
                agent_id=agent_id,
                name=f"Build: {name}",
                working_dir=app_dir,
                origin="application",
                app_id=app_id,
                backend=(agent.get("backend") or "claude-code"),
            )
            await db.update_application(app_id, session_id=session.id, updated_at=_now())

        # Announce the row BEFORE the first turn starts, so the `created`
        # event can never land after an `updated` one produced by that turn
        # (clients upsert by id — a late `created` would re-apply the stale
        # building row over a finished build).
        row = await db.get_application(app_id)
        await self._broadcast_application("application_created", row)

        prompt = self.compose_build_prompt(
            name=name,
            description=description,
            app_dir=app_dir,
            entrypoint=entrypoint,
            instructions=instructions,
        )
        try:
            async with self._build_updates.setdefault(app_id, asyncio.Lock()):
                self.require_writable(await self.get_application(app_id))
                receipt = await self.session_mgr.start_message(session.id, prompt, source="application")
                if not receipt.accepted:
                    raise ApplicationError("Session admission is closed; the build did not start", status_code=409)
        except Exception as exc:
            logger.exception("Failed to start build session for application %s", app_id)
            await db.update_application(
                app_id,
                status=STATUS_FAILED,
                error=f"failed to start the build session: {exc}",
                updated_at=_now(),
            )
            row = await db.get_application(app_id)
            await self._broadcast_application("application_updated", row)
        if row is None:
            raise ApplicationError("Application was deleted during creation", status_code=409)
        return row

    # ------------------------------------------------------------------ build

    async def request_build(
        self, app_id: str, prompt: str, user_id: str | None = None
    ) -> dict[str, Any]:
        """Serialize build admission with this application's projection writers."""
        await self.get_application(app_id, user_id)
        if not (prompt or "").strip():
            raise ApplicationError("prompt must be a non-empty string")
        try:
            async with self._build_updates.setdefault(app_id, asyncio.Lock()):
                return await self._admit_build(app_id, prompt, user_id)
        finally:
            async def publish() -> None:
                row = await self._require_db().get_application(app_id, user_id)
                if row is not None:
                    await self._broadcast_application("application_updated", row)
            # A subscriber may request the next build. Publishing never holds
            # the application lock, including on refusal or caller cancellation.
            await complete_cleanup(publish())

    async def _admit_build(
        self, app_id: str, prompt: str, user_id: str | None,
    ) -> dict[str, Any]:
        """Run another build turn — "make the header sticky", "add a dark
        mode". Reuses the build session so the agent keeps its context; if that
        session is gone (deleted), a fresh one is opened under the same agent
        and given the full brief again."""
        db = self._require_db()
        if self.session_mgr is None:
            raise ApplicationError("ApplicationManager not bound", status_code=500)
        row = await self.get_application(app_id, user_id)

        prompt = (prompt or "").strip()
        self.require_writable(row)
        if not prompt:
            raise ApplicationError("prompt must be a non-empty string")

        session_id = row["session_id"]
        session = (
            self.session_mgr.get_session(session_id) if session_id else None
        )
        if session is None:
            agent_id = row["agent_id"]
            agent = await db.get_agent(agent_id, row["user_id"]) if agent_id else None
            if agent is None:
                raise ApplicationError(
                    "This application's agent is gone — pick a new one before "
                    "asking for changes",
                    status_code=409,
                )
            if session_id is not None:
                # A builder archived before attribution was retained still has
                # this exact relationship; save it before repointing the app.
                await db.retain_application_session_owner(session_id)
            session = await self.session_mgr.create_session(
                agent_id=agent_id,
                name=f"Build: {row['name']}",
                working_dir=row["app_dir"],
                origin="application",
                app_id=app_id,
                backend=(agent.get("backend") or "claude-code"),
            )
            await db.update_application(app_id, session_id=session.id)
            body = self.compose_build_prompt(
                name=row["name"],
                description=row["description"],
                app_dir=row["app_dir"],
                entrypoint=row["entrypoint"],
                instructions=prompt,
            )
        else:
            body = self.compose_change_prompt(
                request=prompt,
                app_dir=row["app_dir"],
                entrypoint=row["entrypoint"],
            )

        async def restore() -> None:
            await db.update_application(app_id, status=row["status"], error=row["error"], updated_at=_now())

        try:
            await db.update_application(app_id, status=STATUS_BUILDING, error=None, updated_at=_now())
            updated = await db.get_application(app_id)
        except asyncio.CancelledError:
            # No command has been submitted at this point.
            await complete_cleanup(restore())
            raise

        async def admit() -> None:
            try:
                receipt = await self.session_mgr.start_message(session.id, body, source="application")
            except ValueError as exc:
                await restore()
                raise ApplicationError(f"The build did not start: {exc}", status_code=409) from exc
            if not receipt.accepted:
                await restore()
                raise ApplicationError("Session admission is closed; the build did not start", status_code=409)

        # Owner admission does not await its publisher or completion consumers.
        await complete_cleanup(admit())
        assert updated is not None  # written immediately above
        return updated

    # ----------------------------------------------------------------- update

    async def update_application(
        self, app_id: str, user_id: str | None = None, **fields: Any
    ) -> dict[str, Any]:
        """Rename / re-icon / repoint the entrypoint. The directory never
        moves — `app_dir` is the identity of the files on disk."""
        db = self._require_db()
        row = await self.get_application(app_id, user_id)
        updates: dict[str, Any] = {}

        if "name" in fields and fields["name"] is not None:
            new_name = str(fields["name"]).strip()
            if not new_name:
                raise ApplicationError("Application name cannot be empty")
            clash = await db.get_application_by_name(
                new_name, user_id=row["user_id"]
            )
            if clash is not None and clash["id"] != app_id:
                raise ApplicationError(
                    f"An application named {new_name!r} already exists",
                    status_code=409,
                )
            updates["name"] = new_name
        if "description" in fields and fields["description"] is not None:
            updates["description"] = str(fields["description"]).strip()
        if "icon" in fields:
            updates["icon"] = fields["icon"]
        if "entrypoint" in fields and fields["entrypoint"] is not None:
            ep = str(fields["entrypoint"]).strip()
            if not is_safe_relative_path(ep):
                raise ApplicationError("entrypoint must be a path inside the app")
            updates["entrypoint"] = ep

        if not updates:
            assert row is not None  # just written above
            return row
        updates["updated_at"] = _now()
        await db.update_application(app_id, **updates)
        # An entrypoint change can flip a failed app to ready (and back), so
        # re-derive rather than trusting the stored status.
        if "entrypoint" in updates:
            await self._evaluate(app_id, broadcast=False)
        updated = await db.get_application(app_id)
        await self._broadcast_application("application_updated", updated)
        assert updated is not None  # written immediately above
        return updated

    # ---------------------------------------------------------------- archive

    async def set_archived(
        self, app_id: str, archived: bool, user_id: str | None = None
    ) -> dict[str, Any]:
        """Archive an application (it leaves the sidebar) or restore it.

        Archiving keeps the row AND the files, so restoring is instant and the
        app renders exactly as it did — that's the whole point of archiving
        rather than deleting. A restore refuses a name a live application has
        taken since, matching create's uniqueness rule.
        """
        db = self._require_db()
        row = await self.get_application(app_id, user_id)
        if bool(row["archived"]) == archived:
            assert row is not None  # just written above
            return row
        if not archived:
            clash = await db.get_application_by_name(
                row["name"], user_id=row["user_id"]
            )
            if clash is not None and clash["id"] != app_id:
                raise ApplicationError(
                    f"An application named {row['name']!r} already exists — "
                    f"rename it before restoring this one",
                    status_code=409,
                )
        await db.update_application(
            app_id, archived=1 if archived else 0, updated_at=_now()
        )
        if not archived:
            # Restoring is choosing to use it again: it comes back to the
            # sidebar (where it was, if it was pinned when it left).
            await db.set_application_pinned(app_id, True)
        updated = await db.get_application(app_id)
        await self._broadcast_application(
            "application_archived" if archived else "application_updated",
            updated,
        )
        assert updated is not None  # written immediately above
        return updated

    # ------------------------------------------------------------------ pins

    async def set_pinned(
        self, app_id: str, pinned: bool, user_id: str | None = None
    ) -> dict[str, Any]:
        """Pin an application to the sidebar or unpin it (sidebar-pins.md).

        Only where it is *shown* changes — an unpinned app still serves, its
        backend still runs, and the Applications page still lists it.
        """
        db = self._require_db()
        row = await self.get_application(app_id, user_id)
        if row["archived"]:
            raise ApplicationError("Restore the application before pinning it")
        await db.set_application_pinned(app_id, pinned)
        updated = await db.get_application(app_id)
        await self._broadcast_application("application_updated", updated)
        assert updated is not None  # written immediately above
        return updated

    async def reorder_pins(
        self, ordered_ids: list[str], user_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Set the sidebar order of the pinned applications; returns every
        live application. Every id must name a pinned, live one, once — and
        one of this account's, so an id from another account is "not a pinned
        application" rather than a row this reorders."""
        db = self._require_db()
        if len(set(ordered_ids)) != len(ordered_ids):
            raise ApplicationError("An application appears twice in the order")
        try:
            after, changed = await db.reorder_application_pins(ordered_ids, user_id=user_id)
        except ValueError as exc:
            raise ApplicationError(str(exc)) from exc
        for row in changed:
            await self._broadcast_application("application_updated", row)
        return after

    # ----------------------------------------------------------------- delete

    async def delete_application(
        self, app_id: str, *, keep_files: bool = False, user_id: str | None = None
    ) -> None:
        """Take cleanup ownership; cancellation only ends this HTTP wait."""
        async with self._build_updates.setdefault(app_id, asyncio.Lock()):
            row = await self.get_application(app_id, user_id)
            state = row.get("deletion")
            if state is not None and state["keep_files"] != keep_files:
                raise ApplicationError("Deletion already owns a different keep_files choice", status_code=409)
            active = self._deletions.get(app_id)
            if active is not None and not active[1].done() and active[0] != keep_files:
                raise ApplicationError("Deletion already owns a different keep_files choice", status_code=409)
            if active is None or active[1].done():
                state = state or {"phase": "retiring", "keep_files": keep_files, "error": None}
                # The operation owns this write too: an abandoned HTTP wait
                # cannot strand an acknowledged phase without its executor.
                task = asyncio.create_task(self._delete_owned(row, state), name=f"delete-application-{app_id}")
                self._deletions[app_id] = (keep_files, task)
                if self.session_mgr is not None:
                    self.session_mgr._own_task(task)
            else:
                task = active[1]
        await asyncio.shield(task)

    async def _delete_owned(self, row: dict[str, Any], state: dict[str, Any]) -> None:
        from .app_agent import app_agent_manager
        from .app_backends import backend_supervisor

        db = self._require_db()
        app_id = row["id"]
        state = dict(state, error=None)
        async with self._build_updates.setdefault(app_id, asyncio.Lock()):
            await db.set_application_deletion(app_id, state)
            backend_supervisor.close_application(app_id)
        session_ids = await db.application_session_ids(app_id)
        try:
            if state["phase"] == "retiring":
                if self.session_mgr is None:
                    raise ApplicationError("ApplicationManager not bound", status_code=500)
                # Close the original Sessions before independent-work drain;
                # no stopped BG/delegation return can wake a new execution.
                await self.session_mgr.retire_application_writers(session_ids)
                await backend_supervisor.stop(app_id)
                if app_agent_manager.session_mgr is self.session_mgr:
                    await app_agent_manager.delete_conversations_for_app(app_id)
                state = dict(state, phase="files")
                await db.set_application_deletion(app_id, state)
            if not state["keep_files"]:
                paths = (row["app_dir"], data_dir_for(row["app_dir"]), runtime_dir_for(row["app_dir"]))
                # Validate all targets before touching the first one.
                if any(not is_inside_root(path, row.get("user_id")) for path in paths):
                    raise ApplicationError("Application files are outside the managed account root", status_code=409)
                for path in paths:
                    try:
                        await asyncio.to_thread(shutil.rmtree, path)
                    except FileNotFoundError:
                        # Only an absent target is complete; a missing child
                        # inside a still-present directory remains an error.
                        if os.path.lexists(path):
                            raise
            await db.delete_application(app_id)
        except Exception as exc:
            db.health.require_healthy()
            state = dict(state, error=str(exc))
            await db.set_application_deletion(app_id, state)
            await self._broadcast_application("application_updated", await db.get_application(app_id))
            if isinstance(exc, ApplicationError):
                raise
            raise ApplicationError(f"Application deletion unfinished: {exc}", status_code=409) from exc
        # Only confirmed deletion releases the temporary prohibition. History
        # remains usable; a future human input may recreate its working dir.
        if self.session_mgr is not None:
            await self.session_mgr.release_application_writers(session_ids)
        self._deletions.pop(app_id, None)
        await self._broadcast_application("application_deleted", row)

    # ------------------------------------------------------------- build eval

    def entrypoint_path(self, row: dict[str, Any]) -> str | None:
        """Absolute path of the app's entry file, or None if it escapes."""
        return resolve_within(row["app_dir"], row["entrypoint"])

    def is_built(self, row: dict[str, Any]) -> bool:
        path = self.entrypoint_path(row)
        return path is not None and os.path.isfile(path)

    async def refresh_icons(self) -> int:
        """Re-discover every live application's own icon. Returns how many changed.

        Discovery otherwise only happens in `_evaluate`, i.e. after a build —
        which would leave every application that already exists showing the
        generic fallback until someone happened to rebuild it. Running this at
        startup means an app that already ships a logo picks it up with no user
        action, and one whose icon was deleted outside a build stops pointing at
        a file that isn't there.

        Touches only `icon_src`; a user's emoji and the app's status are left
        exactly as they are.
        """
        db = self._require_db()
        changed = 0
        for row in await db.load_applications(include_archived=False):
            if row.get("deletion") is not None:
                from .app_backends import backend_supervisor

                backend_supervisor.close_application(row["id"])
                continue
            try:
                found = discover_icon_src(row["app_dir"], row["entrypoint"])
            except Exception:
                logger.exception(
                    "icon refresh failed for application %s", row["id"]
                )
                continue
            if found != row.get("icon_src"):
                await db.update_application(row["id"], icon_src=found)
                changed += 1
        if changed:
            logger.info("refreshed the icon on %d application(s)", changed)
        return changed

    async def _evaluate(
        self, app_id: str, *, error: str | None = None, broadcast: bool = True,
        request_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Derive terminal status from the filesystem: the entrypoint exists →
        `ready`, it doesn't → `failed`. `error` is the turn-level failure
        message, used only when the entrypoint is also missing (an agent can
        hit an error *after* writing a perfectly good app)."""
        db = self._require_db()
        row = await db.get_application(app_id)
        if row is None or row.get("deletion") is not None:
            return None
        built = self.is_built(row)
        if built:
            fields = {
                "status": STATUS_READY,
                "error": None,
                "last_built_at": _now(),
            }
        else:
            fields = {
                "status": STATUS_FAILED,
                "error": error
                or (
                    f"The build finished but {row['entrypoint']} was never "
                    f"written. Ask for changes to try again."
                ),
            }
        # Re-discover the app's own icon on every evaluation, including a
        # failed one: an agent can write a perfectly good logo in a build that
        # also errored, and an icon deleted since the last build must clear
        # rather than linger. Never touches `icon` — a user's typed emoji is
        # theirs and always wins in the UI.
        try:
            fields["icon_src"] = discover_icon_src(row["app_dir"], row["entrypoint"])
        except Exception:
            logger.exception("icon discovery failed for application %s", app_id)

        # A build turn rewrote the code, so a backend still running is running
        # the old one, and its dependencies may have changed
        # (application-backends.md §6).
        try:
            from .app_backends import backend_supervisor

            await backend_supervisor.on_rebuild(app_id, row["app_dir"])
        except Exception:
            logger.exception("failed restarting backend for application %s", app_id)

        if request_id is not None and not self._completion_is_current(row["session_id"], request_id):
            return None
        fields["updated_at"] = _now()
        await db.update_application(app_id, **fields)
        updated = await db.get_application(app_id)
        if broadcast:
            await self._broadcast_application("application_updated", updated)
        return updated

    async def _on_broadcast(self, msg: dict[str, Any]) -> None:
        """Progress can mark a build running; only completion ends it."""
        sid = msg.get("session_id")
        if not sid or self.db is None:
            return
        if msg.get("type") != "status" or msg.get("status") != "running":
            return
        updates = []
        rows = await self.db.get_applications_for_session(sid)
        for candidate in rows:
            async with self._build_updates.setdefault(candidate["id"], asyncio.Lock()):
                row = await self.db.get_application(candidate["id"])
                if row is None or row.get("deletion") is not None or row["session_id"] != sid:
                    continue
                session = self.session_mgr.get_session(sid) if self.session_mgr is not None else None
                # Progress can outlive its writer and completion. Read the
                # current execution projection instead of replaying running.
                if (session is None or session.status is not SessionStatus.running
                        or self.session_mgr.dispatch_snapshot(sid)["paused"]):
                    continue
                if row["status"] != STATUS_BUILDING:
                    await self.db.update_application(row["id"], status=STATUS_BUILDING, updated_at=_now())
                    updates.append(await self.db.get_application(row["id"]))
        for updated in updates:
            await self._broadcast_application("application_updated", updated)

    def _completion_is_current(self, session_id: str, request_id: str) -> bool:
        assert self.session_mgr is not None
        if self.session_mgr.get_session(session_id) is None:
            return True
        dispatch = self.session_mgr.dispatch_snapshot(session_id)
        return not dispatch["items"] and dispatch["active_request_id"] in {None, request_id}

    async def _on_completion(self, session_id: str, completion: RequestCompletion) -> None:
        """Also observe direct chat in the build session when publication fails."""
        if self.db is None or self.session_mgr is None:
            return
        updates = []
        rows = await self.db.get_applications_for_session(session_id)
        for candidate in rows:
            async with self._build_updates.setdefault(candidate["id"], asyncio.Lock()):
                row = await self.db.get_application(candidate["id"])
                if row is None or row.get("deletion") is not None or row["session_id"] != session_id:
                    continue
                if not self._completion_is_current(session_id, completion.request_id):
                    continue
                if completion.recording_state != "committed" or completion.outcome is None:
                    await self.db.update_application(
                        row["id"], status=STATUS_FAILED,
                        error=completion.reason or f"The build has no confirmed result ({completion.state}).",
                        updated_at=_now(),
                    )
                    updates.append(await self.db.get_application(row["id"]))
                else:
                    outcome = completion.outcome
                    updated = await self._evaluate(row["id"], error=(
                        outcome.message or "The build turn ended with an error."
                    ) if outcome.status != "success" else None, broadcast=False, request_id=completion.request_id)
                    if updated is not None:
                        updates.append(updated)
        # A subscriber may submit more build work. It never holds the
        # projection lock while that work emits its own progress/completion.
        for updated in updates:
            await self._broadcast_application("application_updated", updated)

    # ---------------------------------------------------------------- prompts

    @staticmethod
    def compose_build_prompt(
        *,
        name: str,
        description: str,
        app_dir: str,
        entrypoint: str = DEFAULT_ENTRYPOINT,
        instructions: str = "",
    ) -> str:
        """The brief handed to the agent on the first build turn.

        The constraints aren't stylistic — they're what makes the result
        renderable at all: Octopus serves the directory as static files inside
        an iframe, with no build step and no server (applications.md §6).
        """
        extra = (instructions or "").strip()
        extra_block = f"\n\nAdditional instructions from the user:\n{extra}" if extra else ""
        return (
            f"Build a web application called \"{name}\".\n\n"
            f"What the user wants:\n{description}{extra_block}\n\n"
            f"Build it in this directory (it already exists and is your "
            f"working directory):\n  {app_dir}\n\n"
            f"How it will be run — this is a hard contract, not a preference:\n"
            f"- Octopus serves this directory as STATIC FILES and renders it "
            f"inside an iframe. There is no build step, no dev server, and no "
            f"backend.\n"
            f"- The entry point MUST be `{entrypoint}` at the root of that "
            f"directory, loadable directly by a browser.\n"
            f"- Plain HTML/CSS/JS (ES modules are fine). Any library must come "
            f"from a CDN <script>/<link> tag or be vendored as a file in the "
            f"directory. Never require `npm install`, bundling, or a "
            f"transpile step.\n"
            f"- Persist state in the browser (localStorage) — there is no "
            f"server to talk to.\n"
            f"- It is rendered at whatever size the pane happens to be, so it "
            f"must look right on both a wide desktop pane and a ~400px phone "
            f"width.\n"
            f"- Make it genuinely good: real layout, real styling, real empty "
            f"states. Not a wireframe.\n"
            f"\nIf it needs SERVER-SIDE work — running a program, talking to "
            f"a service that blocks browser requests, or storing more than a "
            f"few megabytes — it can have a real backend:\n"
            f"- Write an executable `start.sh` at the root that starts a "
            f"server in the FOREGROUND on `127.0.0.1:$PORT`. Use `exec` so "
            f"the server IS the process; a script that backgrounds it and "
            f"returns looks like a crash to Octopus.\n"
            f"- Put dependency installation in an executable `install.sh`, "
            f"installing into `$APP_RUNTIME_DIR`. It runs before the first "
            f"start and after every rebuild, so keep it idempotent.\n"
            f"- Keep the app's own data in `$APP_DATA_DIR`. A rebuild rewrites "
            f"your code and never touches that directory — anything you want "
            f"to survive goes there, nowhere else.\n"
            f"- The page reaches the backend at `api/…` relative to itself "
            f"(Octopus proxies `/apps/<id>/api/*`). Everything else is served "
            f"as a static file from this directory.\n"
            f"- No backend? Don't write the scripts. A static app stays "
            f"static.\n\n"
            f"It can also TALK TO THE USER'S OWN AGENTS. If the app is about "
            f"discussing, summarizing, explaining, drafting or deciding "
            f"anything, use this rather than shipping an API key for some "
            f"other model — there is no key to configure and the user's "
            f"agents already have their tools and memory:\n"
            f"- `POST agent/ask` (relative to the page) with JSON "
            f"`{{message, context?, conversation_id?, agent?, title?}}` "
            f"returns `{{conversation_id, reply}}`. `context` is the material "
            f"— the article, the selection, the row — kept separate from the "
            f"person's message.\n"
            f"- `POST agent/chat` takes the same body and streams the answer "
            f"back as Server-Sent Events: `conversation` (carries the id), "
            f"`delta` (text as it is written), `message`, `tool`, then one "
            f"`done` or `error`. Use it for anything chat-shaped; a reply "
            f"that appears word by word is the difference between fast and "
            f"broken.\n"
            f"- Keep the `conversation_id` from the first reply and send it "
            f"back with every later message — that is what makes it a "
            f"conversation instead of a series of strangers. "
            f"`GET agent/conversations` and "
            f"`GET agent/conversations/<id>` restore the panel after a "
            f"reload; `GET agent/agents` lists who can be addressed by name "
            f"(omit `agent` to get this application's own).\n"
            f"- From a backend script, the same API is at "
            f"`$OCTOPUS_AGENT_API` with the header "
            f"`X-Octopus-App-Token: $OCTOPUS_APP_TOKEN`.\n"
            f"- `agent/` and `api/` are reserved prefixes: don't put files "
            f"there.\n"
            f"- The agent replies in prose and will not edit this app's code "
            f"— that happens in this build session, not through the running "
            f"app.\n\n"
            f"- Give it an icon: either `icon.svg` at the root (square, and "
            f"legible at 22px), or a `<link rel=\"icon\">` in "
            f"`{entrypoint}` pointing at a file in this directory. Octopus "
            f"picks it up automatically and shows it in the sidebar, so the "
            f"app isn't anonymous there.\n"
            f"- Also ship `apple-touch-icon.png` for phone home screens, and "
            f"link it from `{entrypoint}`:\n"
            f"    <link rel=\"apple-touch-icon\" href=\"apple-touch-icon.png\">\n"
            f"  Four rules that are easy to get wrong: it must be a **PNG** "
            f"(Safari ignores SVG here); **opaque**, with no alpha (iOS "
            f"composites transparency onto black); **180x180**; and "
            f"**full-bleed square** with no rounded corners of its own (iOS "
            f"applies its own mask, and your corners would show as dark "
            f"notches). Keep the href RELATIVE — an absolute "
            f"`/apple-touch-icon.png` resolves against Octopus, not your "
            f"app.\n\n"
            f"When you're done, verify `{entrypoint}` exists in that directory "
            f"and end with a one-paragraph summary of what you built."
        )

    @staticmethod
    def compose_change_prompt(
        *, request: str, app_dir: str, entrypoint: str = DEFAULT_ENTRYPOINT
    ) -> str:
        """Follow-up turn in the same build session — the agent already has the
        original brief in its transcript, so this stays short."""
        return (
            f"Update the application in {app_dir}.\n\n"
            f"Requested change:\n{request}\n\n"
            f"The same contract still applies: `{entrypoint}` stays the entry "
            f"point, the page is served as static files with no build step, "
            f"and anything server-side goes through the `start.sh` / "
            f"`install.sh` scripts (data in `$APP_DATA_DIR`). "
            f"When you're done, verify `{entrypoint}` still exists and briefly "
            f"say what changed."
        )

    # -------------------------------------------------------------- broadcast

    async def _broadcast_application(self, kind: str, row: dict[str, Any] | None) -> None:
        """Push a row change to every WS client. No `session_id` key: these are
        global events (the sidebar shows applications regardless of which
        session is open), and the client's snapshot-dedup only applies to
        events that carry one."""
        if self.session_mgr is None or row is None:
            return
        await self.session_mgr._broadcast(
            {"type": kind, "application_id": row["id"], "application": row},
            # As for agents: the row carries its owner, so the frame says whose
            # it is rather than leaving the bus to guess from a session that
            # is not there (multi-tenancy.md §7).
            user_id=row.get("user_id"),
        )


application_manager = ApplicationManager()
