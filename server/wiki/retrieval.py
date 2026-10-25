"""Compact indexed navigation, exact identity lookup and stable pagination."""

import base64
import hashlib
import json

from . import search_index
from .formats import MARKDOWN_QUERY_CONTRACT, MARKDOWN_SQL
from .read_response import compact_index
from .store import WikiError


def pagination(store, cursor, offset, filters, *, db=None):
    epoch = store._index_state(db)["index_epoch"]
    fingerprint = hashlib.sha256(json.dumps(filters, sort_keys=True).encode()).hexdigest()
    if cursor:
        if offset:
            raise WikiError("cursor and offset cannot be combined", 422)
        try:
            if len(cursor) > 2048:
                raise ValueError()
            value = json.loads(base64.urlsafe_b64decode(cursor.encode()))
            if value["filters"] != fingerprint or not isinstance(value["offset"], int) or value["offset"] < 0:
                raise ValueError()
        except (ValueError, KeyError, TypeError) as exc:
            raise WikiError("Invalid pagination cursor or changed filters", 422) from exc
        if value["epoch"] != epoch:
            raise WikiError("Index changed; restart this list from its first page", 409)
        offset = value["offset"]
    return offset, epoch, fingerprint


def continuation(offset, epoch, fingerprint):
    if offset is None:
        return None
    return base64.urlsafe_b64encode(json.dumps({"offset": offset, "epoch": epoch, "filters": fingerprint}, separators=(",", ":")).encode()).decode()


def compact(store, row, hit=None, terms=()):
    metadata = store.indexed_metadata(row)
    sections = []
    for part in (hit["sections"][:2] if hit else []):
        role = next((section["role"] for section in metadata.get("body_sections", []) if section["start"] <= part["start"] < section["end"]), "body")
        sections.append({"id": f"{row['id']}:{row['revision']}:{part['start']}:{part['end']}", "heading_path": [part["title"]], "section_role": role, "start": part["start"], "end": part["end"]})
    snippet, snippet_start = search_index.preview(row, hit, terms) if hit else (metadata.get("description", "")[:240], 0)
    matched = [name for name, value in (("title", row["title"]), ("path", row["path"]), ("aliases", " ".join(metadata.get("aliases", []))), ("description", metadata.get("description", "")), ("body", row["text"][hit["start"]:hit["end"]] if hit else "")) if terms and any(term in search_index.normalize(value) for term in terms)]
    return {key: row[key] for key in ("id", "path", "title", "revision")} | {
        "uid": metadata.get("uid"), "doc_role": metadata.get("doc_role", "unknown"),
        "description": metadata.get("description", "")[:400], "freshness": "indexed",
        "matched_fields": matched if hit else ["identity"], "sections": sections,
        "matched_section_count": len(hit["sections"]) if hit else 0,
        "snippet": snippet[:240], "snippet_start": snippet_start,
    }


def exact(db, query, kind, directory, include_assets, titles=False):
    # Resolve registered identities before scope filtering. A filtered or
    # retired identity must not turn into a same-spelling title elsewhere.
    # Only portable UIDs are case-insensitive; filesystem paths and IDs are not.
    identified = [row[0] for row in db.execute("""SELECT id FROM document_identities WHERE id=?
        UNION SELECT id FROM documents WHERE path=?
        UNION SELECT doc_id FROM uid_aliases WHERE uid=?""", (query, query, query.casefold()))]
    if identified:
        where = "d.id IN (" + ",".join("?" for _ in identified) + ")"
        arguments = identified
    elif titles:
        where = "(n.title=? OR EXISTS(SELECT 1 FROM json_each(n.aliases) WHERE value=?))"
        arguments = [search_index.normalize(query), search_index.normalize(query)]
    else:
        return False, []
    where += " AND " + MARKDOWN_SQL
    if kind:
        where += " AND d.kind=?"
        arguments.append(kind)
    elif not include_assets:
        where += " AND d.kind<>'asset'"
    if directory:
        prefix = directory.rstrip("/") + "/"
        where += " AND substr(d.path,1,?)=?"
        arguments.extend((len(prefix), prefix))
    rows = db.execute("SELECT d.*,m.metadata AS managed_metadata FROM documents d JOIN search_documents n ON n.doc_id=d.id LEFT JOIN managed_notes m ON m.path=d.path WHERE " + where + " ORDER BY d.path", arguments).fetchall()
    return bool(identified), rows


def search(store, query, limit, offset, kind, include_assets, directory, intent, view, cursor, model_response=False):
    if intent not in {None, "lookup", "question", "overview", "compare"} or view not in {"full", "compact"}:
        raise WikiError("Unknown search intent/view", 422)
    with store.connect() as db:
        db.execute("BEGIN")
        offset, epoch, fingerprint = pagination(store, cursor, offset, {"query": query, "kind": kind, "assets": include_assets, "directory": directory, "intent": intent, "view": view, "query_contract": MARKDOWN_QUERY_CONTRACT}, db=db)
        identified, exact_rows = exact(db, query, kind, directory, include_assets, titles=intent == "lookup")
        if identified or exact_rows:
            total, terms, mode = len(exact_rows), [], "exact" if len(exact_rows) <= 1 else "ambiguous"
            selection = [(row, None) for row in exact_rows[offset:offset + limit]]
        else:
            hits, total, terms, mode = search_index.select(db, query, kind, include_assets, offset, limit, directory, exclude_roles=intent != "lookup", deduplicate=intent != "lookup", comparison=intent == "compare", diversify=intent == "overview")
            selection = [(db.execute("SELECT d.*,m.metadata AS managed_metadata FROM documents d LEFT JOIN managed_notes m ON m.path=d.path WHERE d.id=?", (hit["doc_id"],)).fetchone(), hit) for hit in hits]
        items = []
        for row, hit in selection:
            if view == "compact":
                items.append(compact(store, row, hit, terms))
            else:
                if hit:
                    snippet, position = search_index.preview(row, hit, terms)
                    sections = [search_index.public_section(part) for part in hit["sections"]]
                else:
                    snippet, position = row["text"][:400], 0
                    sections = [{"title": row["title"], "start": 0, "end": len(row["text"]), "line_start": 1, "line_end": row["text"].count("\n") + 1}]
                items.append(store.navigation(row, db=db) | {"snippet": snippet, "snippet_start": position, "score": -hit["cost"] if hit else 0.0, "section": sections[0], "sections": sections})
        state = store._index_state(db)
    next_offset = offset + limit if offset + limit < total else None
    result = {"items": items, "total": total, "next_offset": next_offset, "next_cursor": continuation(next_offset, epoch, fingerprint), "match_mode": mode, "query_terms": terms, "view": view, "intent": intent or "question", **(compact_index(state) if view == "compact" else state)}
    if model_response:
        from .model_response import page

        return page(items, result, offset, total, lambda following: continuation(following, epoch, fingerprint))
    return result
