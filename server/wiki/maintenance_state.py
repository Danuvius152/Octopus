"""Maintenance observations over derived facts; no write authority is granted."""

import base64
import json
import re
import sqlite3
from pathlib import PurePosixPath

from .formats import is_markdown_maintenance
from .maintenance_contract import DAILY_SCOPE as DAILY_SCOPE
from .maintenance_contract import MAINTENANCE_OPERATIONS as MAINTENANCE_OPERATIONS
from .maintenance_contract import PURPOSE, digest
from .maintenance_projection import bounds, settle
from .maintenance_view import MaintenanceSnapshot
from .read_response import COMPACT_READ_MAX_BYTES, model_result_bytes
from .store import WikiError


def exact_document(db, selector):
    """An explicit identity never falls back to another same-spelling note."""
    alias = db.execute("SELECT doc_id FROM uid_aliases WHERE uid=?", (selector.casefold(),)).fetchone()
    identifier = alias[0] if alias else selector
    identity = db.execute("SELECT id FROM document_identities WHERE id=?", (identifier,)).fetchone()
    if identity is None:
        identity = db.execute("SELECT id FROM document_identities WHERE path=?", (selector,)).fetchone()
    if identity:
        row = db.execute("SELECT id,path,revision FROM documents WHERE id=?", (identity[0],)).fetchone()
        return row if row is not None and is_markdown_maintenance(row["path"]) else None
    return None


def selected_document(db, selector):
    row = exact_document(db, selector)
    if row is not None:
        return row
    # Retired identities and explicit paths must not become fuzzy title hits.
    if "/" in selector or selector.casefold().startswith("urn:uuid:") or re.fullmatch(r"[0-9a-fA-F]{32}", selector):
        return None
    matches = [r for r in db.execute("SELECT id,path,revision,title FROM documents")
               if is_markdown_maintenance(r["path"])
               and selector in {PurePosixPath(r["path"]).name, r["title"]}]
    if len(matches) > 1:
        raise WikiError("The selected name is ambiguous; use its exact current path", 403)
    return matches[0] if matches else None


def _cursor(value, filters, generation):
    if value is None:
        return 0
    try:
        if len(value) > 2048:
            raise ValueError()
        decoded = json.loads(base64.urlsafe_b64decode(value.encode()))
        if decoded["filters"] != filters or type(decoded["offset"]) is not int or decoded["offset"] < 0:
            raise ValueError()
        observed_generation = decoded["generation"]
        if not isinstance(observed_generation, str) or not re.fullmatch(r"[0-9a-f]{64}", observed_generation):
            raise ValueError()
    except (ValueError, KeyError, TypeError) as exc:
        raise WikiError("Invalid maintenance cursor or changed filters", 422) from exc
    if observed_generation != generation:
        raise WikiError("stale_cursor: Maintenance state changed; restart this observation", 409)
    return decoded["offset"]


def status(store, registration, user_id, *, doc=None, directory="Library", view="state", limit=20, cursor=None, actor="human", model_response=False):
    if view not in {"state", "links"} or not 1 <= limit <= 50 or (view == "links" and doc is None):
        raise WikiError("Invalid maintenance status selection", 422)
    scope = PurePosixPath(directory)
    if not directory or scope.is_absolute() or ".." in scope.parts or "." in scope.parts or "\\" in directory:
        raise WikiError("Use a literal vault-relative directory", 422)
    directory = scope.as_posix()
    with store.operation():
        try:
            settled = settle(store, doc=doc, directory=directory)
            with store.connect() as db:
                db.execute("BEGIN")
                snapshot = MaintenanceSnapshot(store, registration, user_id, db, actor)
                filters = digest({"doc": doc, "directory": directory, "view": view, "contract": PURPOSE})
                offset = _cursor(cursor, filters, snapshot.generation)
                response_scope = "selected_document" if doc is not None else directory
                row = exact_document(db, doc) if doc is not None else None
                identifiers = [row["id"]] if row is not None else [] if doc is not None else None
                snapshot.scope_ready(identifiers=identifiers, directory=directory)
                if not settled:
                    snapshot.ready = False
                    if snapshot.index["index_status"] == "ready":
                        snapshot.index["index_status"] = "updating"
                if not snapshot.ready:
                    return {"ready": False, **snapshot.index, "items": [], "total": None, "next_cursor": None,
                            "generation": snapshot.generation, "scope": response_scope}
                if doc is not None:
                    if row is None:
                        raise WikiError("Current Markdown document not found", 404)
                    identifier = row["id"]
                    if view == "links":
                        values = snapshot.impact(identifier)
                        total = len(values)
                        selected = values[offset:offset + limit]
                    else:
                        rows = db.execute("SELECT * FROM maintenance_projection WHERE doc_id=?", (identifier,)).fetchall()
                        if not rows:
                            raise WikiError("Maintenance projection is updating; retry the observation", 503)
                        total = 1
                        selected = [snapshot.project(identifier, value) for value in snapshot.load_page(rows[offset:offset + limit])]
                else:
                    parameters = bounds(directory)
                    total = db.execute("""SELECT count(*) FROM maintenance_projection
                        WHERE candidate=1 AND path>=? AND path<?""", parameters).fetchone()[0]
                    rows = db.execute("""SELECT * FROM maintenance_projection
                        WHERE candidate=1 AND path>=? AND path<? ORDER BY path,doc_id LIMIT ? OFFSET ?""",
                                      (*parameters, limit, offset)).fetchall()
                    selected = [snapshot.project(value["current"]["id"], value) for value in snapshot.load_page(rows)]

                def continuation(following):
                    return base64.urlsafe_b64encode(json.dumps(
                        {"generation": snapshot.generation, "filters": filters, "offset": following},
                        separators=(",", ":")).encode()).decode() if following is not None else None

                def response():
                    end = offset + len(selected)
                    return {"ready": True, **snapshot.index, "items": selected, "total": total,
                            "next_cursor": continuation(end if end < total else None),
                            "generation": snapshot.generation, "scope": response_scope, "view": view,
                            "coverage": "Indexed current state and known accepted receipt paths; not a before-move snapshot or write grant."}

                result = response()
                if model_response:
                    from .model_response import page

                    return page(selected, result, offset, total, continuation)
                while model_result_bytes(result) > COMPACT_READ_MAX_BYTES:
                    if len(selected) <= 1:
                        raise WikiError("Maintenance metadata exceeds its response limit", 422)
                    selected.pop()
                    result = response()
                return result
        except sqlite3.DatabaseError as error:
            raise WikiError("Maintenance state is unavailable; durable state retained", 503) from error
