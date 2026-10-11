"""Existing files are authoritative; the SQLite index can be rebuilt."""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import os
import re
import sqlite3
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from .extract import SUPPORTED, extract


class WikiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def revision(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def asset_reason(relative: str) -> str | None:
    """Classify known asset directories by purpose, independently of suffix."""
    path = PurePosixPath(relative)
    if path.suffix.lower() == ".pdf":
        return "PDF processing is explicitly excluded"
    if path.parts[0].casefold() == "codex skills":
        return "Agent tool resources"
    if any(
        part.casefold() in {"tmp", "temp", "static", "assets", "node_modules"}
        for part in path.parts[:-1]
    ):
        return "Temporary extraction or supporting static asset"
    return None


def vault_read(function):
    @functools.wraps(function)
    def locked(self, *args, **kwargs):
        from .history import WikiHistory

        history = WikiHistory(self)
        with self.connect() as db:
            pending = db.execute(
                "SELECT 1 FROM changes WHERE status='prepared' LIMIT 1"
            ).fetchone()
        with history.locked(shared=not pending):
            return function(self, *args, **kwargs)

    return locked


class WikiStore:
    def __init__(self, root: Path, state: Path, vault_id: str) -> None:
        self.root = root.resolve(strict=True)
        self.state = state
        self.vault_id = vault_id
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db_path = state / "index.sqlite3"
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,path TEXT UNIQUE NOT NULL,revision TEXT NOT NULL,
                title TEXT NOT NULL,kind TEXT NOT NULL,text TEXT NOT NULL,
                pages TEXT NOT NULL,quality TEXT NOT NULL,metadata TEXT NOT NULL,
                signature TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS managed_notes (
                path TEXT PRIMARY KEY,base_hash TEXT NOT NULL,rendered_revision TEXT NOT NULL,
                regions TEXT NOT NULL,metadata TEXT NOT NULL
            )""")
            # Identity and ownership are durable ledgers, separate from extracted rows.
            db.execute("""CREATE TABLE IF NOT EXISTS document_identities (
                id TEXT PRIMARY KEY,path TEXT UNIQUE,file_key TEXT NOT NULL
            )""")
            for row in db.execute("SELECT id,path FROM documents").fetchall():
                file = self.path(row["path"])
                key = self.file_key(file.stat()) if file.is_file() else ""
                db.execute(
                    "INSERT OR IGNORE INTO document_identities VALUES(?,?,?)",
                    (row["id"], row["path"], key),
                )

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def path(self, relative: str) -> Path:
        logical = PurePosixPath(relative)
        if (
            not relative
            or logical.is_absolute()
            or "\\" in relative
            or any(p in {"", ".", "..", ".git"} for p in relative.split("/"))
        ):
            raise WikiError("Invalid vault-relative path", 404)
        target = self.root.joinpath(*logical.parts)
        if not target.resolve().is_relative_to(self.root):
            raise WikiError("Path escapes vault", 404)
        for parent in [target, *target.parents]:
            if parent == self.root:
                break
            if parent.is_symlink():
                raise WikiError("Symlink access is not supported", 404)
        return target

    @staticmethod
    def file_key(stat) -> str:
        return f"{stat.st_dev}:{stat.st_ino}:{stat.st_mtime_ns}"

    def move_identity(self, old: str, new: str, db=None) -> None:
        """Move trusted identity/ownership without touching any note bytes."""
        if db is None:
            with self.connect() as connection:
                self.move_identity(old, new, connection)
            return
        self.path(old)
        target = self.path(new)
        identity = db.execute(
            "SELECT id FROM document_identities WHERE path=?", (old,)
        ).fetchone()
        if identity is None:
            return
        # A prior deleted document at the destination keeps its ID as a tombstone.
        db.execute(
            "UPDATE document_identities SET path=NULL WHERE path=? AND id<>?",
            (new, identity[0]),
        )
        db.execute("DELETE FROM documents WHERE path=? AND id<>?", (new, identity[0]))
        db.execute(
            "UPDATE document_identities SET path=?,file_key=? WHERE id=?",
            (new, self.file_key(target.stat()), identity[0]),
        )
        db.execute(
            "UPDATE documents SET path=?,signature='' WHERE id=?", (new, identity[0])
        )
        owned = db.execute(
            "SELECT 1 FROM managed_notes WHERE path=?", (old,)
        ).fetchone()
        if owned:
            db.execute("DELETE FROM managed_notes WHERE path=?", (new,))
            db.execute("UPDATE managed_notes SET path=? WHERE path=?", (new, old))

    def identity(self, db, relative, stat) -> str:
        key = self.file_key(stat)
        identity = db.execute(
            "SELECT id FROM document_identities WHERE path=?", (relative,)
        ).fetchone()
        if identity is None:
            candidates = [
                row
                for row in db.execute(
                    "SELECT id,path FROM document_identities WHERE file_key=? AND path IS NOT NULL",
                    (key,),
                )
                if not self.path(row["path"]).exists()
            ]
            if len(candidates) == 1:
                self.move_identity(candidates[0]["path"], relative, db)
                identity = candidates[0]
        if identity:
            doc_id = identity["id"]
            db.execute(
                "UPDATE document_identities SET file_key=? WHERE id=?", (key, doc_id)
            )
        else:
            doc_id = uuid.uuid5(uuid.UUID(self.vault_id), relative).hex
            if db.execute(
                "SELECT 1 FROM document_identities WHERE id=?", (doc_id,)
            ).fetchone():
                doc_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO document_identities VALUES(?,?,?)", (doc_id, relative, key)
            )
        return doc_id

    def _index(self, db: sqlite3.Connection, relative: str) -> sqlite3.Row:
        path = self.path(relative)
        if not path.is_file() or path.suffix.lower() not in SUPPORTED:
            raise WikiError("Document not found", 404)
        raw = path.read_bytes()
        digest = revision(raw)
        stat = path.stat()
        doc_id = self.identity(db, relative, stat)
        signature = f"{stat.st_mtime_ns}:{stat.st_size}:v3"
        existing = db.execute(
            "SELECT * FROM documents WHERE path=?", (relative,)
        ).fetchone()
        if (
            existing is not None
            and existing["signature"]
            and existing["revision"] == digest
            and json.loads(existing["metadata"]).get("pipeline_version")
            == "octopus-extract-v3"
        ):
            db.execute(
                "UPDATE documents SET signature=? WHERE id=?",
                (signature, existing["id"]),
            )
            return db.execute(
                "SELECT * FROM documents WHERE id=?", (existing["id"],)
            ).fetchone()
        try:
            extracted = extract(raw, path.suffix.lower())
        except Exception as exc:
            extracted = {
                "text": "",
                "pages": [],
                "quality": "error",
                "metadata": {"error": f"{type(exc).__name__}: {exc}"},
            }
        text = extracted["text"]
        metadata = extracted["metadata"]
        front = re.match(
            r"\A\ufeff?---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text[:65536], re.S
        )
        if front:
            try:
                fields = yaml.safe_load(front[1])
                if isinstance(fields, dict):
                    for name in ("tags", "aliases"):
                        value = fields.get(name, [])
                        if isinstance(value, str):
                            value = [value]
                        if isinstance(value, list):
                            metadata[name] = [v for v in value if isinstance(v, str)]
            except yaml.YAMLError:
                metadata["frontmatter_error"] = "Invalid YAML; original retained"
        portable = re.search(r"<!-- octopus-wiki\s+(.*?)\s*-->", text, re.S)
        if portable:
            try:
                supplied = json.loads(portable[1])
                if not isinstance(supplied, dict):
                    raise ValueError("Metadata must be an object")
                kind = supplied.get("kind", "original")
                sources = supplied.get("sources", [])
                if (
                    not isinstance(kind, str)
                    or not isinstance(sources, list)
                    or any(
                        not isinstance(ref, dict)
                        or not all(
                            isinstance(ref.get(key), str)
                            for key in ("doc", "revision", "location")
                        )
                        for ref in sources
                    )
                ):
                    raise ValueError("Invalid document kind or source relationships")
                for name in ("tags", "aliases"):
                    if name in supplied and (
                        not isinstance(supplied[name], list)
                        or any(not isinstance(value, str) for value in supplied[name])
                    ):
                        raise ValueError("Tags and aliases must be string lists")
                # Extraction quality and tool versions cannot be supplied by a document.
                metadata |= {
                    k: v for k, v in supplied.items() if k not in extracted["metadata"]
                }
            except (ValueError, TypeError):
                metadata["metadata_error"] = "Invalid octopus-wiki metadata"
        kind = metadata.get("kind", "original")
        if kind not in {"original", "source", "concept", "topic", "submission"}:
            kind = "original"
        exclusion = asset_reason(relative)
        if exclusion:
            kind = "asset"
            metadata["asset_reason"] = exclusion
        title = next(
            (line[2:].strip() for line in text.splitlines() if line.startswith("# ")),
            path.stem,
        )
        db.execute(
            "INSERT OR REPLACE INTO documents VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                doc_id,
                relative,
                digest,
                title,
                kind,
                text,
                json.dumps(extracted["pages"]),
                extracted["quality"],
                json.dumps(metadata, ensure_ascii=False),
                signature,
            ),
        )
        row = db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
        assert row is not None
        return row

    @vault_read
    def refresh(self) -> None:
        with self.connect() as db:
            found = set()
            for directory, dirs, files in os.walk(self.root, followlinks=False):
                dirs[:] = sorted(
                    d
                    for d in dirs
                    if not d.startswith(".") and not (Path(directory) / d).is_symlink()
                )
                for name in sorted(files):
                    path = Path(directory) / name
                    if (
                        name.startswith(".")
                        or path.is_symlink()
                        or path.suffix.lower() not in SUPPORTED
                    ):
                        continue
                    relative = path.relative_to(self.root).as_posix()
                    found.add(relative)
                    stat = path.stat()
                    old = db.execute(
                        "SELECT signature FROM documents WHERE path=?", (relative,)
                    ).fetchone()
                    if old is None or old[0] != f"{stat.st_mtime_ns}:{stat.st_size}:v3":
                        self._index(db, relative)
            for row in db.execute("SELECT id,path FROM documents").fetchall():
                if row["path"] not in found:
                    db.execute("DELETE FROM documents WHERE id=?", (row["id"],))

    def public(self, row: sqlite3.Row) -> dict[str, Any]:
        metadata = json.loads(row["metadata"])
        from .sections import managed_metadata

        managed, stale = managed_metadata(
            self, row["path"], self.path(row["path"]).read_bytes()
        )
        native_tags = metadata.get("tags", [])
        metadata |= managed
        if native_tags or managed.get("tags"):
            metadata["tags"] = list(
                dict.fromkeys([*native_tags, *managed.get("tags", [])])
            )
        with self.connect() as db:
            for ref in metadata.get("sources", []):
                source_id = ref.get("doc", "")
                # A missing durable ID must stay missing; a reused path is not its source.
                identified = bool(re.fullmatch(r"[0-9a-f]{32}", source_id))
                source = db.execute(
                    "SELECT revision,path,signature FROM documents WHERE "
                    + ("id=?" if identified else "path=?"),
                    (source_id if identified else source_id or ref.get("path"),),
                ).fetchone()
                current = source["revision"] if source else None
                if source:
                    try:
                        file = self.path(source["path"])
                        stat = file.stat()
                        if (
                            f"{stat.st_mtime_ns}:{stat.st_size}:v3"
                            != source["signature"]
                        ):
                            current = revision(file.read_bytes())
                    except (OSError, WikiError):
                        current = None
                if source:
                    ref["path"] = source["path"]
                if current != ref.get("revision"):
                    stale = True
        return {
            k: row[k] for k in ("id", "path", "revision", "title", "kind", "quality")
        } | {
            "metadata": metadata,
            "pages": json.loads(row["pages"]),
            "stale": stale,
            "characters": len(row["text"]),
        }

    @vault_read
    def search(
        self,
        query: str,
        limit: int = 12,
        offset: int = 0,
        kind: str | None = None,
        include_assets: bool = False,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 50 or offset < 0 or len(query) > 1000:
            raise WikiError("Invalid search pagination or query length", 422)
        self.refresh()
        terms = query.lower().split()
        if not terms:
            raise WikiError("Search query is required")
        # Contiguous Chinese questions need lexical pieces without a remote
        # embedding service. Exact words retain a much larger score.
        expanded = set(terms)
        for chunk in re.findall(r"[\u3400-\u9fff]+", query):
            for width in (2, 3, 4):
                expanded.update(
                    chunk[i : i + width] for i in range(len(chunk) - width + 1)
                )
        with self.connect() as db:
            ranked = []
            for row in db.execute("SELECT * FROM documents"):
                if row["kind"] == "asset" and not include_assets and kind != "asset":
                    continue
                if kind and row["kind"] != kind:
                    continue
                text = row["text"].lower()
                score = sum(
                    20 * (t in row["title"].lower())
                    + 8 * (t in row["path"].lower())
                    + min(text.count(t), 8)
                    for t in terms
                )
                score = score * 20 + sum(
                    (5 if t in row["title"].lower() else 1)
                    for t in expanded
                    if t in text
                )
                if not score:
                    continue
                start = max(
                    0, next((text.find(t) for t in terms if t in text), 0) - 100
                )
                ranked.append(
                    self.public(row)
                    | {
                        "snippet": row["text"][start : start + 400],
                        "snippet_start": start,
                        "score": score,
                    }
                )
            ranked.sort(key=lambda item: (-item["score"], item["path"]))
            end = offset + limit
            return {
                "items": ranked[offset:end],
                "total": len(ranked),
                "next_offset": end if end < len(ranked) else None,
            }

    @vault_read
    def documents(
        self,
        *,
        kind=None,
        quality=None,
        source=None,
        offset=0,
        limit=30,
        include_assets=False,
        directory=None,
    ):
        if offset < 0 or not 1 <= limit <= 100:
            raise WikiError("Invalid document pagination", 422)
        self.refresh()
        with self.connect() as db:
            items = []
            for row in db.execute("SELECT * FROM documents ORDER BY title,path"):
                if row["kind"] == "asset" and not include_assets and kind != "asset":
                    continue
                if directory and not row["path"].startswith(
                    directory.rstrip("/") + "/"
                ):
                    continue
                if (
                    kind
                    and row["kind"] != kind
                    or quality
                    and row["quality"] != quality
                ):
                    continue
                document = self.public(row)
                if source and not any(
                    s.get("doc") == source or s.get("path") == source
                    for s in document["metadata"].get("sources", [])
                ):
                    continue
                items.append(document)
        return {
            "items": items[offset : offset + limit],
            "total": len(items),
            "next_offset": offset + limit if offset + limit < len(items) else None,
        }

    @vault_read
    def graph(self, doc):
        self.refresh()
        center = self.read(doc, length=1)
        with self.connect() as db:
            rows = db.execute("SELECT * FROM documents").fetchall()
        documents = [self.public(row) for row in rows]
        nodes, edges = {center["id"]: center}, []
        for row, document in zip(rows, documents, strict=True):
            for source in document["metadata"].get("sources", []):
                if document["id"] == center["id"] or source.get("doc") == center["id"]:
                    nodes[document["id"]] = document
                    linked = next(
                        (r for r in rows if r["id"] == source.get("doc")), None
                    )
                    if linked:
                        nodes[linked["id"]] = self.public(linked)
                    edges.append(
                        {
                            "from": document["id"],
                            "to": source.get("doc"),
                            "revision": source.get("revision"),
                            "location": source.get("location"),
                            "relation": "derived_from",
                        }
                    )
            from .links import local_links, resolve_link

            for target, anchor, relation in local_links(row["text"]):
                linked = resolve_link(
                    target,
                    PurePosixPath(document["path"]).parent.as_posix(),
                    documents,
                    wiki=relation == "wikilink",
                )
                if linked and center["id"] in {document["id"], linked["id"]}:
                    nodes[document["id"]] = document
                    nodes[linked["id"]] = linked
                    edges.append(
                        {
                            "from": document["id"],
                            "to": linked["id"],
                            "revision": linked["revision"],
                            "location": anchor,
                            "relation": relation,
                        }
                    )
        return {"nodes": list(nodes.values()), "edges": edges}

    @vault_read
    def read(
        self,
        doc: str,
        *,
        start: int = 0,
        length: int = 16000,
        expected_revision: str | None = None,
        page: int | None = None,
    ) -> dict[str, Any]:
        if start < 0 or not 1 <= length <= 24000 or (page is not None and page < 1):
            raise WikiError("Invalid read range", 422)
        with self.connect() as db:
            old = db.execute(
                "SELECT path FROM document_identities WHERE id=? OR path=?", (doc, doc)
            ).fetchone()
        if old and old[0] and not self.path(old[0]).is_file():
            self.refresh()
        with self.connect() as db:
            old = db.execute(
                "SELECT path FROM document_identities WHERE id=? OR path=?", (doc, doc)
            ).fetchone()
            row = self._index(db, old[0] if old and old[0] else doc)
            if expected_revision and expected_revision != row["revision"]:
                raise WikiError("Document changed; read the current revision", 409)
            text = row["text"]
            page_end = len(text)
            page_offset = 0
            if page is not None:
                pages = json.loads(row["pages"])
                if page < 1 or page > len(pages):
                    raise WikiError("PDF page not found", 404)
                selected = pages[page - 1]
                page_offset = selected["start"]
                start += selected["start"]
                page_end = selected["end"]
            start = min(start, page_end)
            end = min(page_end, start + length)
            return self.public(row) | {
                "content": text[start:end],
                "page": page,
                "start": start - page_offset,
                "end": end - page_offset,
                "next_start": end - page_offset if end < page_end else None,
                "line_start": text[:start].count("\n") + 1,
                "line_end": text[:end].count("\n") + 1,
            }
