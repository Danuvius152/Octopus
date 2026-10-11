"""Curator operations: source-version checks and lossless original layout."""

import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import quote

from .extract import blocks
from .history import WikiHistory
from .links import local_links, resolve_link
from .sections import original_text, region, top_offset
from .store import WikiError, revision


class WikiCuration:
    def __init__(self, store):
        self.store = store
        self.history = WikiHistory(store)

    def sources(self, sources):
        result = []
        for source in sources:
            document = self.store.read(
                source["doc"], length=1, expected_revision=source["revision"]
            )
            if not source.get("location"):
                raise WikiError(
                    "Every source requires a line, section or page location"
                )
            result.append(
                {
                    "doc": document["id"],
                    "path": document["path"],
                    "revision": document["revision"],
                    "location": source["location"],
                }
            )
        return result

    def validate_links(self, path, content):
        links = list(local_links(content))
        if not links:
            return
        self.store.refresh()
        with self.store.connect() as db:
            documents = [
                {
                    "path": row["path"],
                    "title": row["title"],
                    "metadata": json.loads(row["metadata"]),
                }
                for row in db.execute("SELECT path,title,metadata FROM documents")
            ]
        parent = Path(path).parent.as_posix()
        for target, _anchor, kind in links:
            if not target:
                continue
            resolved = resolve_link(target, parent, documents, wiki=kind == "wikilink")
            if resolved:
                self.store.path(resolved["path"])
                continue
            relative = os.path.normpath(os.path.join(parent, target)).replace(
                os.sep, "/"
            )
            if kind == "markdown_link" and self.store.path(relative).is_file():
                continue
            raise WikiError(
                f"New local link does not resolve unambiguously: {target}", 422
            )

    def organize(
        self, path, expected_revision, key_note, tags, relations, sources, actor, reason
    ):
        try:
            return self._organize(
                path,
                expected_revision,
                key_note,
                tags,
                relations,
                sources,
                actor,
                reason,
            )
        except WikiError as exc:
            if exc.status != 409:
                raise
            with self.history.locked():
                target = self.store.path(path)
                raw = target.read_bytes() if target.is_file() else None
                operation = {
                    "path": path,
                    "expected_revision": expected_revision,
                    "key_note": key_note,
                    "tags": tags,
                    "relations": relations,
                    "sources": sources,
                }
                row = self.history.record(
                    path, raw, raw, actor, reason, "conflict", proposal=operation
                )
            raise WikiError(
                f"{exc}; proposal retained as change {row['id']}", 409
            ) from exc

    def _organize(
        self, path, expected_revision, key_note, tags, relations, sources, actor, reason
    ):
        """Supplement an assigned note without rewriting any user-owned bytes."""
        with self.history.locked():
            self.validate_links(path, key_note or "")
            source = self.store.read(
                path, length=1, expected_revision=expected_revision
            )
            if source["kind"] == "asset":
                raise WikiError("Excluded assets are not note templates", 422)
            refs = self.sources(sources)
            linked = self.sources(relations)
            destination = path
            if Path(path).suffix.lower() not in {".md", ".markdown"}:
                destination = path + ".md"
                target = self.store.path(destination)
                if target.exists():
                    with self.store.connect() as db:
                        paired = db.execute(
                            "SELECT 1 FROM managed_notes WHERE path=?", (destination,)
                        ).fetchone()
                    if not paired:
                        raise WikiError(
                            "The paired reading-note path is already owned by another document",
                            409,
                        )
                base = (
                    target.read_bytes().decode("utf-8")
                    if target.exists()
                    else (f"# {Path(path).stem}\n\n[原文]({quote(Path(path).name)})\n")
                )
                destination_revision = (
                    revision(base.encode()) if target.exists() else None
                )
                refs.append(
                    {
                        "doc": source["id"],
                        "path": path,
                        "revision": source["revision"],
                        "location": "原文全文",
                    }
                )
            else:
                try:
                    base = self.store.path(path).read_bytes().decode("utf-8")
                except UnicodeError:
                    raise WikiError("Non-UTF-8 Markdown requires encoding review", 422)
                destination_revision = expected_revision
            with self.store.connect() as db:
                owned = db.execute(
                    "SELECT * FROM managed_notes WHERE path=?", (destination,)
                ).fetchone()
            if owned is None and "<!-- octopus-section " in base:
                raise WikiError(
                    "Managed regions have no trusted ownership record; review this move or import before organizing",
                    409,
                )
            previous = json.loads(owned["regions"]) if owned else []
            base = original_text(base, previous)
            additions = []
            provenance = {
                "sources": refs + linked,
                "source_revision": source["revision"],
            }
            existing_summary = re.search(
                r"(?mi)^#{1,6}\s+(?:Key note|摘要|要点|核心观点|总结)\s*$",
                base[: top_offset(base) + 1500],
            )
            if (
                not existing_summary
                and not (key_note and key_note.strip())
                and not any(part["role"] == "key_note" for part in previous)
            ):
                raise WikiError(
                    "Read the note and supply Key note when it has no opening points",
                    422,
                )
            if key_note is not None and key_note.strip() and not existing_summary:
                additions.append(
                    region("key_note", "## Key note\n\n" + key_note.strip(), provenance)
                )
            elif key_note is None:
                additions.extend(
                    part for part in previous if part["role"] == "key_note"
                )
            normalized = list(
                dict.fromkeys(re.sub(r"[^\w/-]+", "-", tag).strip("-/") for tag in tags)
            )
            normalized = [tag for tag in normalized if tag]
            if normalized:
                additions.append(
                    region(
                        "tags",
                        " ".join("#" + tag for tag in normalized),
                        {"tags": normalized},
                    )
                )
            if linked:
                links = []
                for entry, relationship in zip(linked, relations, strict=True):
                    relative = os.path.relpath(
                        entry["path"], Path(destination).parent.as_posix()
                    )
                    links.append(
                        f"- [{Path(entry['path']).stem}]({quote(relative)}): {relationship['explanation']}"
                    )
                additions.append(
                    region(
                        "relations",
                        "## 关联\n\n" + "\n".join(links),
                        {"sources": linked},
                    )
                )
            for addition in additions:
                self.validate_links(destination, addition["raw"])
            offset = top_offset(base)
            head = "".join(
                part["raw"]
                for part in additions
                if part["role"] in {"key_note", "tags"}
            )
            tail = "".join(
                part["raw"] for part in additions if part["role"] == "relations"
            )
            text = base[:offset] + head + base[offset:] + tail
            metadata = {
                "author": "Weber 整理",
                "tags": normalized,
                "sources": [ref for ref in refs + linked if ref["path"] != destination],
                "original_revision": revision(base.encode()),
                "organized_from_revision": source["revision"],
            }
            management = {
                "path": destination,
                "base_hash": revision(base.encode()),
                "rendered_revision": revision(text.encode()),
                "regions": json.dumps(additions, ensure_ascii=False),
                "metadata": json.dumps(metadata, ensure_ascii=False),
            }
            result = self.history.write_locked(
                destination,
                text.encode(),
                destination_revision,
                actor,
                reason,
                management,
            )
            return result | {"original_preserved": True, "source_path": path}

    def publish(self, path, content, kind, sources, expected_revision, actor, reason):
        if (
            not path.startswith(("Library/", "Wiki/"))
            or Path(path).suffix.lower() != ".md"
            or kind not in {"source", "concept", "topic"}
        ):
            raise WikiError(
                "Publish needed Markdown concepts or topic pages inside Library/"
            )
        if not sources:
            raise WikiError("Derived knowledge must cite current sources")
        with self.history.locked():
            self.validate_links(path, content)
            try:
                refs = self.sources(sources)
            except WikiError as exc:
                existing = self.store.path(path)
                row = self.history.record(
                    path,
                    existing.read_bytes() if existing.is_file() else None,
                    content.encode(),
                    actor,
                    reason + "; source conflict",
                    "conflict",
                )
                raise WikiError(
                    f"Source conflict: {exc}; proposal retained as change {row['id']}",
                    409,
                )
            metadata = {"kind": kind, "sources": refs, "author": "Weber 整理"}
            raw = (
                "<!-- octopus-wiki "
                + json.dumps(metadata, ensure_ascii=False)
                + " -->\n\n"
                + content
                + "\n\n> Weber 整理：以上为新增整理内容，请按所列来源核验。\n"
            ).encode()
            existing = self.store.path(path)
            if existing.is_file():
                current = self.store.read(path, length=1)
                if current["kind"] == "original":
                    raise WikiError(
                        "An original cannot be replaced by derived knowledge", 403
                    )
            return self.history.write_locked(
                path, raw, expected_revision, actor, reason
            )

    def blocks(self, doc):
        document = self.store.read(doc, length=1)
        if Path(document["path"]).suffix.lower() != ".md":
            raise WikiError("Lossless restructuring supports UTF-8 Markdown originals")
        raw = self.store.path(document["path"]).read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeError:
            raise WikiError("Original encoding requires manual review")
        parts = blocks(text)
        return {
            "path": document["path"],
            "revision": revision(raw),
            "blocks": [
                {
                    "id": str(index),
                    "content": part,
                    "sha256": hashlib.sha256(part.encode()).hexdigest(),
                }
                for index, part in enumerate(parts)
            ],
        }

    def restructure(self, path, expected_revision, order, additions, actor, reason):
        with self.history.locked():
            current = self.blocks(path)
            if current["revision"] != expected_revision:
                raw = self.store.path(path).read_bytes()
                row = self.history.record(
                    path,
                    raw,
                    raw,
                    actor,
                    reason,
                    "conflict",
                    proposal={
                        "path": path,
                        "expected_revision": expected_revision,
                        "order": order,
                        "additions": additions,
                    },
                )
                raise WikiError(
                    f"Original changed; layout proposal retained as {row['id']}", 409
                )
            parts = current["blocks"]
            if sorted(order) != sorted(p["id"] for p in parts):
                raise WikiError("Every original block must appear exactly once")
            if (
                parts
                and parts[0]["content"].lstrip("\ufeff").splitlines()[0] == "---"
                and order[0] != "0"
            ):
                raise WikiError("Original frontmatter must stay first")
            inserted = {}
            for addition in additions:
                self.validate_links(path, addition["content"])
                position = addition["before"]
                if (
                    position < 0
                    or position > len(parts)
                    or (
                        position == 0
                        and parts
                        and parts[0]["content"].lstrip("\ufeff").splitlines()[0]
                        == "---"
                    )
                ):
                    raise WikiError("Invalid addition position")
                refs = self.sources(addition.get("sources", []))
                attribution = (
                    "\n\n<!-- Weber 整理; sources: "
                    + json.dumps(refs, ensure_ascii=False)
                    + " -->\n> Weber 整理"
                    + ("，来源待核验" if not refs else "，来源见记录")
                    + "\n\n"
                )
                inserted.setdefault(position, []).append(
                    attribution + addition["content"] + "\n\n"
                )
            result = []
            mapping = {p["id"]: p["content"] for p in parts}
            for index in range(len(order) + 1):
                result.extend(inserted.get(index, []))
                if index < len(order):
                    result.append(mapping[order[index]])
            return self.history.write_locked(
                path, "".join(result).encode(), expected_revision, actor, reason
            )
