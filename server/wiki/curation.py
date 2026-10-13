"""Curator operations: source-version checks and lossless original layout."""

import json
import os
import re
from pathlib import Path
from urllib.parse import quote

from .history import WikiHistory
from .links import anchor_exists, local_links, resolve_link, source_span
from .sections import original_text, region, top_offset
from .store import WikiError, is_maintainable_text, revision


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

    def validate_links(self, path, content, candidate=None):
        links = list(local_links(content))
        if not links:
            return
        self.store.synchronize()
        with self.store.connect() as db:
            documents = [
                {
                    "path": row["path"],
                    "title": row["title"],
                    "metadata": self.store.indexed_metadata(row),
                }
                for row in db.execute("SELECT path,title,metadata FROM documents")
            ]
        parent = Path(path).parent.as_posix()
        for target, anchor, kind in links:
            if not target:
                if anchor and not anchor_exists(candidate if candidate is not None else content, anchor):
                    raise WikiError(f"New local anchor does not exist: {anchor}", 422)
                continue
            if candidate is not None and path in {
                os.path.normpath(os.path.join(parent, target)).replace(os.sep, "/"),
                target if kind == "wikilink" else "",
                target + ".md" if kind == "wikilink" else "",
            }:
                if anchor and not anchor_exists(candidate, anchor):
                    raise WikiError(f"New local anchor does not exist: {target}#{anchor}", 422)
                continue
            resolved = resolve_link(target, parent, documents, wiki=kind == "wikilink")
            if resolved:
                self.store.path(resolved["path"])
                if anchor:
                    text = candidate if candidate is not None and resolved["path"] == path else self.store.path(resolved["path"]).read_text(encoding="utf-8-sig")
                    if not anchor_exists(text, anchor):
                        raise WikiError(f"New local anchor does not exist: {target}#{anchor}", 422)
                continue
            relative = os.path.normpath(os.path.join(parent, target)).replace(
                os.sep, "/"
            )
            if kind == "markdown_link" and self.store.path(relative).is_file():
                if anchor:
                    raise WikiError(f"Cannot verify an anchor in this non-note asset: {target}#{anchor}", 422)
                continue
            raise WikiError(
                f"New local link does not resolve unambiguously: {target}", 422
            )

    def organize(
        self, path, expected_revision, key_note, tags, relations, sources, actor, reason,
        publication=None, aliases=None, reuse_existing_summary=False, task=None,
        frontmatter_patch=None, item_doc=None,
    ):
        if frontmatter_patch is not None:
            from .normalize import organize

            return organize(self, path, expected_revision, key_note, tags, aliases, relations, sources, actor, reason, frontmatter_patch, task, item_doc, reuse_existing_summary)
        try:
            return self._organize(
                path,
                expected_revision,
                key_note,
                tags or [],
                relations,
                sources or [],
                actor,
                reason,
                publication,
                aliases,
                reuse_existing_summary,
                task,
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
                    "aliases": aliases,
                    "reuse_existing_summary": reuse_existing_summary,
                }
                if publication:
                    operation |= {"job_id": publication["job_id"], "source_path": path}
                row = self.history.record(
                    path, raw, raw, actor, reason, "conflict", proposal=operation
                )
            raise WikiError(
                f"{exc}; proposal retained as change {row['id']}", 409
            ) from exc

    def _organize(
        self, path, expected_revision, key_note, tags, relations, sources, actor, reason,
        publication=None, aliases=None, reuse_existing_summary=False, task=None,
    ):
        """Supplement an assigned note without rewriting any user-owned bytes."""
        with self.history.locked():
            if task:
                from .tasks import tasks

                tasks.require_path(task, self.store, path)
                tasks.require_read(task, self.store, path, expected_revision)
                for ref in sources + relations:
                    tasks.require_source_read(task, self.store, ref)
            source = self.store.read(
                path, length=1, expected_revision=expected_revision
            )
            if not is_maintainable_text(source["path"]):
                raise WikiError("Excluded assets are not note templates", 422)
            refs = self.sources(sources)
            linked = self.sources(relations)
            for entry, relationship in zip(linked, relations, strict=True):
                with self.store.connect() as db:
                    text = db.execute("SELECT text FROM documents WHERE id=?", (entry["doc"],)).fetchone()[0]
                if source_span(text, entry["location"]) is None:
                    raise WikiError("A relation needs an existing line, heading or block locator", 422)
                if not relationship["explanation"].strip():
                    raise WikiError("A relation needs a concrete explanation", 422)
            destination = path
            if Path(path).suffix.lower() not in {".md", ".markdown"}:
                destination = path + ".md"
                target = self.store.path(destination)
                if task:
                    tasks.require_path(task, self.store, destination)
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
                if task:
                    if target.exists():
                        tasks.require_read(task, self.store, destination, destination_revision)
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
            supplied_points = bool(key_note and key_note.strip())
            if supplied_points and reuse_existing_summary:
                raise WikiError("Choose supplementary Key note or explicit reuse of existing points", 422)
            if (
                not supplied_points and not reuse_existing_summary
                and not (key_note is None and any(part["role"] == "key_note" for part in previous))
            ):
                raise WikiError(
                    "Supply Key note or explicitly choose reuse_existing_summary after reading its content",
                    422,
                )
            if reuse_existing_summary and not re.sub(r"(?ms)<!--.*?-->|^#{1,6}\s+[^\n]*$", "", base[top_offset(base):]).strip():
                raise WikiError("An empty note has no existing points to reuse", 422)
            if supplied_points:
                additions.append(
                    region("key_note", "## Key note\n\n" + key_note.strip(), provenance)
                )
            elif key_note is None and not reuse_existing_summary:
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
            normalized_aliases = list(dict.fromkeys(alias.strip() for alias in aliases if alias.strip())) if aliases is not None else json.loads(owned["metadata"]).get("aliases", []) if owned else []
            if aliases is None:
                additions.extend(part for part in previous if part["role"] == "aliases")
            elif normalized_aliases:
                additions.append(region("aliases", "<!-- octopus-aliases " + json.dumps(normalized_aliases, ensure_ascii=False).replace("-->", "--\\u003e") + " -->", {"aliases": normalized_aliases}))
            if linked:
                links = []
                for entry, relationship in zip(linked, relations, strict=True):
                    links.append(
                        f"- [[{entry['path']}]]：{relationship['explanation']}"
                    )
                additions.append(
                    region(
                        "relations",
                        "## 关联\n\n" + "\n".join(links),
                        {"sources": linked},
                    )
                )
            offset = top_offset(base)
            head = "".join(
                part["raw"]
                for part in additions
                if part["role"] in {"key_note", "tags", "aliases"}
            )
            tail = "".join(
                part["raw"] for part in additions if part["role"] == "relations"
            )
            text = base[:offset] + head + base[offset:] + tail
            for addition in additions:
                self.validate_links(destination, addition["raw"], candidate=text)
            metadata = {
                "author": "Weber 整理",
                "tags": normalized,
                "aliases": normalized_aliases,
                "opening_points_decision": "supplement" if supplied_points else "reuse" if reuse_existing_summary else "preserve_owned",
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
                publication=publication | {"source_doc": source["id"], "source_path": path, "input_revision": source["revision"]} if publication else None,
            )
            return result | {"original_preserved": True, "source_path": path}

    def publish(self, path, content, kind, sources, expected_revision, actor, reason, task=None, frontmatter_patch=None, tags=None, aliases=None, item_doc=None):
        if (
            not path.startswith(("Library/", "Wiki/"))
            or Path(path).suffix.lower() != ".md"
            or kind not in {"source", "concept", "topic"}
        ):
            raise WikiError(
                "Publish needed Markdown concepts or topic pages inside Library/"
            )
        with self.history.locked():
            if task:
                from .tasks import tasks

                tasks.require_path(task, self.store, path)
                for source in sources or []:
                    tasks.require_source_read(task, self.store, source)
            existing = self.store.path(path)
            if existing.exists():
                raise WikiError("Publish creates new pages; preserve existing text with organize or restructure", 403)
            self.validate_links(path, content)
            try:
                refs = self.sources(sources) if sources is not None else None
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
            import uuid

            from . import format, operations

            fields = {"type": {"source": "Reference", "concept": "Concept", "topic": "Topic"}[kind]} | (frontmatter_patch or {})
            refs = [ref | {"resource": ref["path"]} for ref in refs] if refs is not None else None
            raw = format.patch(content, fields=fields, tags=tags, aliases=aliases, sources=refs, uid="urn:uuid:" + str(uuid.uuid4()), actor=actor).encode()
            context = operations.publication(task, self.store, item_doc=item_doc)
            return self.history.write_locked(
                path, raw, expected_revision, actor, reason, publication=context, operation="publish"
            )

    def blocks(self, doc, **kwargs):
        from .raw_blocks import read

        return read(self, doc, **kwargs)

    def restructure(self, path, expected_revision, order, additions, actor, reason, task=None, frontmatter_patch=None, tags=None, aliases=None, item_doc=None):
        with self.history.locked():
            if task:
                from .tasks import tasks

                tasks.require_path(task, self.store, path)
                tasks.require_read(task, self.store, path, expected_revision)
                for addition in additions:
                    for source in addition.get("sources", []):
                        tasks.require_source_read(task, self.store, source)
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
            from . import format, operations

            text = "".join(result)
            for addition in additions:
                self.validate_links(path, addition["content"], candidate=text)
            document = self.store.read(path, length=1, expected_revision=expected_revision)
            text = format.patch(text, fields=frontmatter_patch, tags=tags, aliases=aliases, uid=document["metadata"]["uid"] if frontmatter_patch is not None else None, actor=actor if frontmatter_patch is not None else None)
            context = operations.publication(task, self.store, document, item_doc)
            return self.history.write_locked(path, text.encode(), expected_revision, actor, reason, operations.management(self.store, path, text), publication=context, operation="restructure")
