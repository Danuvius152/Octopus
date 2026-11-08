"""Compact indexed navigation, exact identity lookup and stable pagination."""

import base64
import hashlib
import json
import sqlite3
from contextlib import nullcontext

from . import search_index
from .formats import MARKDOWN_QUERY_CONTRACT, MARKDOWN_SQL
from .line_coordinates import LineCoordinates
from .read_response import COMPACT_ERROR_LIMIT, compact_index
from .store import WikiError


def pagination(store, cursor, offset, filters, *, db=None):
    if type(offset) is not int or not 0 <= offset <= 2**63 - 1:
        raise WikiError("Invalid pagination offset", 422)
    with store.connect() if db is None else nullcontext(db) as connection:
        epoch = connection.execute("SELECT epoch FROM index_state WHERE singleton=1").fetchone()[0]
    fingerprint = hashlib.sha256(json.dumps(filters, sort_keys=True).encode()).hexdigest()
    if cursor is not None:
        if offset:
            raise WikiError("cursor and offset cannot be combined", 422)
        try:
            if not isinstance(cursor, str) or not cursor or len(cursor) > 2048:
                raise ValueError()
            value = json.loads(base64.b64decode(cursor.encode("ascii"), altchars=b"-_", validate=True))
            if not isinstance(value, dict) or not isinstance(value["filters"], str) or value["filters"] != fingerprint:
                raise ValueError()
            for field in ("offset", "epoch"):
                if type(value[field]) is not int or not 0 <= value[field] <= 2**63 - 1:
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


def compact(store, row, hit=None, terms=(), *, origin: int = 0):
    metadata = store.indexed_metadata(row)
    sections = []
    for part in (hit["sections"][:2] if hit else []):
        role = next((section["role"] for section in metadata.get("body_sections", []) if section["start"] <= part["start"] < section["end"]), "body")
        sections.append({"id": f"{row['id']}:{row['revision']}:{part['start']}:{part['end']}", "heading_path": [part["title"]], "section_role": role, "start": part["start"], "end": part["end"]})
    snippet, snippet_start = search_index.preview(row, hit, terms, origin=origin) if hit else (metadata.get("description", "")[:240], 0)
    matched = [name for name, value in (("title", row["title"]), ("path", row["path"]), ("aliases", " ".join(metadata.get("aliases", []))), ("description", metadata.get("description", "")), ("body", row["text"][hit["start"] - origin:hit["end"] - origin] if hit else "")) if terms and any(term in search_index.normalize(value) for term in terms)]
    return {key: row[key] for key in ("id", "path", "title", "revision")} | {
        "uid": metadata.get("uid"), "doc_role": metadata.get("doc_role", "unknown"),
        "description": metadata.get("description", "")[:400], "freshness": "indexed",
        "matched_fields": matched if hit else ["identity"], "sections": sections,
        "matched_section_count": len(hit["sections"]) if hit else 0,
        "snippet": snippet[:240], "snippet_start": snippet_start,
        **({"resource_error": metadata["resource_error"], "error": metadata.get("error"), "stale": True}
           if metadata.get("resource_error") else {}),
    }


def lexical_page(db: sqlite3.Connection, ranges: list[tuple[str, int, int]]) -> list[sqlite3.Row]:
    """Fetch the selected chapters in page order within the caller's snapshot."""
    if not ranges:
        return []
    # A legal search page has at most 50 rows (200 bound variables).
    parameters = [value for ordinal, (doc, start, end) in enumerate(ranges)
                  for value in (doc, start, end, ordinal)]
    values = ",".join("(?,?,?,?)" for _ in ranges)
    # SQLite text substr stops at U+0000. Preserve the existing text for that
    # accepted input; all ordinary rows carry only the best chapter's text.
    return db.execute("WITH page_ranges(doc_id,start,end,ordinal) AS (VALUES " + values + """ )
      SELECT d.id,d.path,d.title,d.revision,d.metadata,m.metadata AS managed_metadata,
        CASE WHEN instr(d.text,char(0))=0
          THEN substr(d.text,page_ranges.start+1,page_ranges.end-page_ranges.start)
          ELSE d.text END AS text,
        CASE WHEN instr(d.text,char(0))=0 THEN page_ranges.start ELSE 0 END AS text_origin
      FROM page_ranges JOIN documents d ON d.id=page_ranges.doc_id
      LEFT JOIN managed_notes m ON m.path=d.path ORDER BY page_ranges.ordinal""", parameters).fetchall()


def exact(db: sqlite3.Connection, query: str, kind: str | None, directory: str | None,
          include_assets: bool, titles: bool = False, *, limit: int, offset: int,
          view: str) -> tuple[bool, int, list[sqlite3.Row]]:
    # Resolve registered identities before scope filtering. A filtered or
    # retired identity must not turn into a same-spelling title elsewhere.
    # Only portable UIDs are case-insensitive; filesystem paths and IDs are not.
    identified = [row[0] for row in db.execute("""SELECT id FROM document_identities WHERE id=?
        UNION SELECT id FROM documents WHERE path=?
        UNION SELECT doc_id FROM uid_aliases WHERE uid=?""", (query, query, query.casefold()))]
    arguments: list[str | int]
    if identified:
        where = "d.id IN (" + ",".join("?" for _ in identified) + ")"
        arguments = list(identified)
    elif titles:
        where = "(n.title=? OR EXISTS(SELECT 1 FROM json_each(n.aliases) WHERE value=?))"
        arguments = [search_index.normalize(query), search_index.normalize(query)]
    else:
        return False, 0, []
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
    source = "FROM documents d JOIN search_documents n ON n.doc_id=d.id"
    total = db.execute("SELECT count(*) " + source + " WHERE " + where, arguments).fetchone()[0]
    if not total or offset >= total:
        return bool(identified), total, []
    columns = "d.id,d.path,d.title,d.revision,d.metadata" if view == "compact" else "d.*"
    rows = db.execute("SELECT " + columns + ",m.metadata AS managed_metadata " + source
                      + " LEFT JOIN managed_notes m ON m.path=d.path WHERE " + where
                      + " ORDER BY d.path LIMIT ? OFFSET ?", [*arguments, limit, offset]).fetchall()
    return bool(identified), total, rows


def search(store, query, limit, offset, kind, include_assets, directory, intent, view, cursor, model_response=False):
    if intent not in {None, "lookup", "question", "overview", "compare"} or view not in {"full", "compact"}:
        raise WikiError("Unknown search intent/view", 422)
    with store.connect() as db:
        db.execute("BEGIN")
        offset, epoch, fingerprint = pagination(store, cursor, offset, {"query": query, "kind": kind, "assets": include_assets, "directory": directory, "intent": intent, "view": view, "query_contract": MARKDOWN_QUERY_CONTRACT}, db=db)
        identified, exact_total, exact_rows = exact(db, query, kind, directory, include_assets,
                                                   titles=intent == "lookup", limit=limit, offset=offset, view=view)
        if identified or exact_total:
            total, terms, mode = exact_total, [], "exact" if exact_total <= 1 else "ambiguous"
            selection = [(row, None) for row in exact_rows]
        else:
            hits, total, terms, mode = search_index.select(db, query, kind, include_assets, offset, limit, directory, exclude_roles=intent != "lookup", deduplicate=intent != "lookup", comparison=intent == "compare", diversify=intent == "overview", topic=intent in {None, "question", "overview"})
            if view == "compact":
                rows = lexical_page(db, [(hit["doc_id"], hit["start"], hit["end"]) for hit in hits])
                selection = list(zip(rows, hits, strict=True))
            else:
                selection = [(db.execute("SELECT d.*,m.metadata AS managed_metadata FROM documents d LEFT JOIN managed_notes m ON m.path=d.path WHERE d.id=?", (hit["doc_id"],)).fetchone(), hit) for hit in hits]
        items = []
        for row, hit in selection:
            if view == "compact":
                items.append(compact(store, row, hit, terms, origin=row["text_origin"] if hit else 0))
            else:
                if hit:
                    snippet, position = search_index.preview(row, hit, terms)
                    sections = [search_index.public_section(part) for part in hit["sections"]]
                else:
                    snippet, position = row["text"][:400], 0
                    sections = [{"title": row["title"], "start": 0, "end": len(row["text"]), "line_start": 1, "line_end": LineCoordinates(row["text"]).line(len(row["text"]))}]
                items.append(store.navigation(row, db=db) | {"snippet": snippet, "snippet_start": position, "score": -hit["cost"] if hit else 0.0, "section": sections[0], "sections": sections})
        state = store._index_state(db, error_limit=COMPACT_ERROR_LIMIT if view == "compact" else None)
    next_offset = offset + limit if offset + limit < total else None
    explanation = {
        "exact": "Resolved identity or exact lookup title/alias.",
        "ambiguous": "Multiple exact lookup titles/aliases; disambiguate before reading.",
        "lexical": "Requires all query term groups in the document; not necessarily a contiguous phrase. Ordered body terms take precedence over scattered terms at equal coverage.",
        "comparison": "Comparison candidates may cover different terms in separate chapters.",
        "lexical_partial": (
            "Complete term coverage returned zero results. Relaxed whole concepts in one body section "
            "of at most 6000 original characters, including a substantive subsection's heading context. "
            "Mixed-script objects and structured identifiers stay in the document; at most one other concept "
            "may be absent locally. Lexical coverage does not establish semantic relevance; read to confirm."
        ),
    }[mode]
    result = {"items": items, "total": total, "next_offset": next_offset, "next_cursor": continuation(next_offset, epoch, fingerprint), "match_mode": mode, "match_explanation": explanation, "query_terms": terms, "view": view, "intent": intent or "question", **(compact_index(state) if view == "compact" else state)}
    if model_response:
        from .model_response import page

        return page(items, result, offset, total, lambda following: continuation(following, epoch, fingerprint))
    return result
