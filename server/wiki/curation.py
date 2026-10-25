"""Curator operations: source-version checks and lossless original layout."""

import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

from . import portable_metadata, resolver
from .formats import require_markdown_maintenance
from .history import WikiHistory
from .links import anchor_exists, source_span
from .references import local_references, path_uri, source_reference
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
            reference = source_reference(source)
            if len(reference.identities) > 1:
                with self.store.connect() as db:
                    selected = resolver.resolve(db, reference, "")
                if selected.target != document["id"] or selected.status != "resolved":
                    raise WikiError("Explicit source identities must agree", 422)
            result.append(
                {
                    "doc": document["id"],
                    "resource": document["id"],
                    "path": document["path"],
                    "revision": document["revision"],
                    "location": source["location"],
                }
            )
        return result

    def validate_links(self, path, content, candidate=None, *, managed_metadata=None):
        from . import format

        text = candidate if candidate is not None else content
        parsed = format.parse(text)
        links = list(local_references(text, parsed=parsed))
        if not links:
            return
        self.store.synchronize()
        with self.store.connect() as db:
            original = db.execute("SELECT id,text FROM documents WHERE path=?", (path,)).fetchone()
            native = {}
            try:
                native = portable_metadata.from_document(parsed)
            except (ValueError, TypeError, RecursionError):
                pass
            native |= parsed.metadata(native)
            if managed_metadata is None:
                owned = db.execute("SELECT metadata FROM managed_notes WHERE path=?", (path,)).fetchone()
                managed_metadata = json.loads(owned[0]) if owned else {}
            metadata = native | managed_metadata
            metadata["aliases"] = list(dict.fromkeys([*native.get("aliases", []), *managed_metadata.get("aliases", [])]))
            overlay = {"id": original["id"] if original else "candidate:" + path, "path": path,
                       "title": native.get("title") or parsed.title(Path(path).stem),
                       "text": text, "metadata": metadata}
            if original is not None and candidate is not None:
                previous = Counter((ref.selection, ref.fragment) for ref in local_references(original["text"]))
                introduced = Counter((ref.selection, ref.fragment) for ref in links) - previous
                candidates = links
                links = []
                for ref in candidates:
                    selection = ref.selection, ref.fragment
                    if introduced[selection]:
                        links.append(ref)
                        introduced[selection] -= 1
            parent = Path(path).parent.as_posix()
            for reference in links:
                resolved = resolver.resolve(db, reference, parent, source_doc=overlay["id"], overlay=overlay, check_anchor=False)
                if resolved.status == "resolved":
                    target_text = text
                    if resolved.target != overlay["id"]:
                        target = db.execute("SELECT path FROM documents WHERE id=?", (resolved.target,)).fetchone()
                        file = self.store.path(target["path"])
                        if not file.is_file():
                            raise WikiError(f"New local link target disappeared: {reference.raw_resource}", 409)
                        if reference.fragment:
                            target_text = file.read_text(encoding="utf-8-sig")
                    if reference.fragment and not anchor_exists(target_text, reference.fragment, parsed=parsed if resolved.target == overlay["id"] else None):
                        raise WikiError(f"New local anchor does not exist: {reference.raw_resource}", 422)
                    continue
                relative = os.path.normpath(os.path.join(parent, reference.value)).replace(os.sep, "/")
                if reference.relation == "markdown_link" and reference.namespace == "path" and self.store.path(relative).is_file() and resolved.status == "unresolved":
                    if reference.fragment:
                        raise WikiError(f"Cannot verify an anchor in this non-note asset: {reference.raw_resource}", 422)
                    continue
                raise WikiError(f"New local link does not resolve unambiguously ({resolved.status}): {reference.raw_resource}", 422)

    def organize(
        self, path, expected_revision, key_note, tags, relations, sources, actor, reason,
        publication=None, aliases=None, reuse_existing_summary=False, task=None,
        frontmatter_patch=None, item_doc=None,
    ):
        if task:
            from .tasks import tasks

            with self.history.locked():
                try:
                    tasks.require_path(task, self.store, path)
                except WikiError as exc:
                    if (exc.status != 403 or not relations or key_note is not None
                            or any(value is not None for value in (tags, aliases, sources, frontmatter_patch, item_doc))
                            or reuse_existing_summary):
                        raise
                    return self._navigation(path, expected_revision, relations, actor, reason, task)
        if frontmatter_patch is not None:
            from .normalize import organize

            return organize(self, path, expected_revision, key_note, tags, aliases, relations, sources, actor, reason, frontmatter_patch, task, item_doc, reuse_existing_summary)
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

    def _navigation(self, path, expected_revision, relations, actor, reason, task):
        """Append host-rendered batch links without authorizing the index body."""
        from . import grants
        from .material_proof import Trace
        from .tasks import tasks

        # The caller holds the normal history lock. Missing targets cannot use
        # this branch to acquire a new-page reservation or write permission.
        if not self.store.path(path).is_file():
            raise WikiError("Supporting links require an existing navigation page", 403)
        tasks.require_read(task, self.store, path, expected_revision)
        source = self.store.read(path, length=1, expected_revision=expected_revision)
        for relation in relations:
            tasks.require_source_read(task, self.store, relation)
            if not relation["explanation"].strip():
                raise WikiError("A relation needs a concrete explanation", 422)
        linked = self.sources(relations)
        proofs = [dict(entry, explanation=relation["explanation"]) for entry, relation in zip(linked, relations, strict=True)]
        with self.store.connect() as db:
            context = grants.navigation(db, task, self.store, source, proofs)
            existing = {row[0] for row in db.execute("SELECT target_doc FROM wiki_edges WHERE source_doc=? AND status='resolved'", (source["id"],))}
            owned = db.execute("SELECT * FROM managed_notes WHERE path=?", (path,)).fetchone()
        pending = {entry["doc"]: entry for entry in linked if entry["doc"] not in existing}
        try:
            text = self.store.path(path).read_bytes().decode("utf-8")
        except UnicodeError as exc:
            raise WikiError("Non-UTF-8 originals require encoding review", 422) from exc
        trace = Trace(text)
        management = dict(owned) if owned else {
            "path": path, "base_hash": revision(trace.before), "regions": "[]", "metadata": "{}",
        }
        if pending:
            newline = "\r\n" if "\r\n" in text else "\n"
            addition = region("navigation_links", "\n".join(f"- [[{path_uri(entry['path'])}]]" for entry in pending.values()),
                              {"sources": list(pending.values())})
            addition["raw"] = addition["raw"].replace("\n", newline)
            trace.edit([(len(text), len(text), addition["raw"])])
            text = trace.text
            # Do not recompute the old base or drop edited regions: those are
            # retained evidence of human edits, not newly reviewed content.
            management |= {"rendered_revision": revision(trace.raw),
                           "regions": json.dumps([*json.loads(management["regions"]), addition], ensure_ascii=False)}
            self.validate_links(path, text, candidate=text, managed_metadata=json.loads(management["metadata"]))
        result = self.history.write_locked(path, trace.raw, expected_revision, actor, reason, management,
                                           publication=context, operation="navigation_link",
                                           origin_builder=trace.transition(context))
        return result | {"original_preserved": True, "source_path": path}

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
                for ref in (sources or []) + (relations or []):
                    tasks.require_source_read(task, self.store, ref)
            source = self.store.read(
                path, length=1, expected_revision=expected_revision
            )
            if task:
                from .operations import publication as write_context

                publication = write_context(task, self.store, source)
            require_markdown_maintenance(source["path"])
            refs = self.sources(sources) if sources is not None else []
            linked = self.sources(relations or [])
            for entry, relationship in zip(linked, relations or [], strict=True):
                with self.store.connect() as db:
                    text = db.execute("SELECT text FROM documents WHERE id=?", (entry["doc"],)).fetchone()[0]
                if source_span(text, entry["location"]) is None:
                    raise WikiError("A relation needs an existing line, heading or block locator", 422)
                if not relationship["explanation"].strip():
                    raise WikiError("A relation needs a concrete explanation", 422)
            try:
                base = self.store.path(path).read_bytes().decode("utf-8")
            except UnicodeError:
                raise WikiError("Non-UTF-8 Markdown requires encoding review", 422)
            with self.store.connect() as db:
                owned = db.execute(
                    "SELECT * FROM managed_notes WHERE path=?", (path,)
                ).fetchone()
            if owned is None and "<!-- octopus-section " in base:
                raise WikiError(
                    "Managed regions have no trusted ownership record; review this move or import before organizing",
                    409,
                )
            previous = json.loads(owned["regions"]) if owned else []
            owned_metadata = json.loads(owned["metadata"]) if owned else {}
            from .material_proof import Trace

            trace = Trace(base)
            rendered_base = base
            base = original_text(base, previous)
            offset = top_offset(base)
            trace.edit([(rendered_base.index(part["raw"]), rendered_base.index(part["raw"]) + len(part["raw"]), "") for part in previous])
            additions = []
            # Omitted sources retain the saved or native evidence. Retained
            # citations keep their recorded identity/version, even when stale;
            # validating them as new citations would silently refresh evidence.
            if sources is None:
                refs = owned_metadata.get("sources", source["metadata"].get("sources", []))
            provenance = {
                "sources": refs + linked,
                "source_revision": source["revision"],
            }
            supplied_points = bool(key_note and key_note.strip())
            if supplied_points and sources is None:
                if any(
                    ref["status"] not in {"indexed", "external"}
                    for ref in source["metadata"].get("source_refs", [])
                ):
                    raise WikiError(
                        "A new Key note requires explicit current sources when retained evidence is stale or missing",
                        422,
                    )
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
            if reuse_existing_summary and not re.sub(r"(?ms)<!--.*?-->|^#{1,6}\s+[^\n]*$", "", base[offset:]).strip():
                raise WikiError("An empty note has no existing points to reuse", 422)
            if supplied_points:
                additions.append(
                    region("key_note", "## Key note\n\n" + key_note.strip(), provenance)
                )
            elif key_note is None and not reuse_existing_summary:
                additions.extend(
                    part for part in previous if part["role"] == "key_note"
                )
            normalized = owned_metadata.get("tags", []) if tags is None else list(
                dict.fromkeys(re.sub(r"[^\w/-]+", "-", tag).strip("-/") for tag in tags)
            )
            normalized = [tag for tag in normalized if tag]
            if tags is None:
                additions.extend(part for part in previous if part["role"] == "tags")
            elif normalized:
                additions.append(
                    region(
                        "tags",
                        " ".join("#" + tag for tag in normalized),
                        {"tags": normalized},
                    )
                )
            normalized_aliases = list(dict.fromkeys(alias.strip() for alias in aliases if alias.strip())) if aliases is not None else owned_metadata.get("aliases", [])
            if aliases is None:
                additions.extend(part for part in previous if part["role"] == "aliases")
            elif normalized_aliases:
                additions.append(region("aliases", "<!-- octopus-aliases " + json.dumps(normalized_aliases, ensure_ascii=False).replace("-->", "--\\u003e") + " -->", {"aliases": normalized_aliases}))
            if relations is None:
                additions.extend(part for part in previous if part["role"] == "relations")
            elif linked:
                links = []
                for entry, relationship in zip(linked, relations, strict=True):
                    links.append(
                        f"- [[{path_uri(entry['path'])}]]：{relationship['explanation']}"
                    )
                additions.append(
                    region(
                        "relations",
                        "## 关联\n\n" + "\n".join(links),
                        {"sources": linked},
                    )
                )
            additions.extend(part for part in previous if part["role"] == "navigation_links")
            head = "".join(
                part["raw"]
                for part in additions
                if part["role"] in {"key_note", "tags", "aliases"}
            )
            tail = "".join(
                part["raw"] for part in additions if part["role"] in {"relations", "navigation_links"}
            )
            text = base[:offset] + head + base[offset:] + tail
            trace.edit([(offset, offset, head), (len(base), len(base), tail)])
            metadata = {
                "author": "Weber 整理",
                "tags": normalized,
                "aliases": normalized_aliases,
                "opening_points_decision": "supplement" if supplied_points else "reuse" if reuse_existing_summary else "preserve_owned",
                "original_revision": revision(base.encode()),
                "organized_from_revision": source["revision"],
            }
            if sources is not None or "sources" in owned_metadata or linked:
                metadata["sources"] = [ref for ref in refs + linked if ref.get("path") != path]
            self.validate_links(path, text, candidate=text, managed_metadata=metadata)
            management = {
                "path": path,
                "base_hash": revision(base.encode()),
                "rendered_revision": revision(text.encode()),
                "regions": json.dumps(additions, ensure_ascii=False),
                "metadata": json.dumps(metadata, ensure_ascii=False),
            }
            result = self.history.write_locked(
                path,
                text.encode(),
                expected_revision,
                actor,
                reason,
                management,
                publication=publication | {"source_doc": source["id"], "source_path": path, "input_revision": source["revision"]} if publication else None,
                origin_builder=trace.transition(publication, allow_seed=True),
            )
            return result | {"original_preserved": True, "source_path": path}

    def publish(self, path, content, kind, sources, expected_revision, actor, reason, task=None, frontmatter_patch=None, tags=None, aliases=None, item_doc=None, reservation_id=None):
        require_markdown_maintenance(path)
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
            raw = format.patch(content, fields=fields, tags=tags, aliases=aliases, sources=refs, uid="urn:uuid:" + str(uuid.uuid4()), actor=actor).encode()
            self.validate_links(path, raw.decode(), candidate=raw.decode(), managed_metadata={})
            context = operations.publication(task, self.store, item_doc=item_doc, path=path, reservation_id=reservation_id)
            return self.history.write_locked(
                path, raw, expected_revision, actor, reason, publication=context, operation="publish"
            )

    def blocks(self, doc, **kwargs):
        from .raw_blocks import read

        return read(self, doc, **kwargs)

    def restructure(self, path, expected_revision, order, additions, actor, reason, task=None, frontmatter_patch=None, tags=None, aliases=None, item_doc=None):
        from . import layout

        with self.history.locked():
            require_markdown_maintenance(self.store.read(path, length=1)["path"])
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
            inserted: dict[int, list[Any]] = {}
            addition_details = []
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
                structural = layout.headings_only(addition["content"])
                content = addition["content"] if structural else attribution + addition["content"] + "\n\n"
                inserted.setdefault(position, []).append((content, structural))
                addition_details.append({"before": position, "kind": "structure" if structural else "explanation", "sources": refs})
            result = []
            mapping = {p["id"]: p["content"] for p in parts}
            heading_fragments = []
            newline = "\r\n" if "\r\n" in "".join(mapping.values()) else "\n"
            rendered_characters, tail = 0, ""
            for index in range(len(order) + 1):
                additions_here = inserted.get(index, [])
                rendered = []
                for number, (content, structural) in enumerate(additions_here):
                    if structural:
                        following = additions_here[number + 1][0] if number + 1 < len(additions_here) else mapping[order[index]] if index < len(order) else ""
                        content = layout.insert_heading(content, tail, following, newline)
                        heading_fragments.append((rendered_characters, content))
                    result.append(content)
                    rendered.append(content)
                    rendered_characters += len(content)
                    tail = layout.trailing_context(tail + content)
                inserted[index] = rendered
                if index < len(order):
                    original_part = mapping[order[index]]
                    result.append(original_part)
                    rendered_characters += len(original_part)
                    tail = layout.trailing_context(tail + original_part)
            from . import format, material_proof, operations

            text = "".join(result)
            layout.validate_headings(text, heading_fragments)
            original_text = "".join(part["content"] for part in parts)
            trace = material_proof.Trace(original_text)
            offsets, offset = {}, 0
            for part in parts:
                end = offset + len(part["content"].encode())
                offsets[part["id"]] = (offset, end)
                offset = end
            chunks: list[bytes | tuple[int, int]] = []
            for index in range(len(order) + 1):
                chunks.extend(value.encode() for value in inserted.get(index, []))
                if index < len(order):
                    chunks.append(offsets[order[index]])
            trace.replace(chunks)
            document = self.store.read(path, length=1, expected_revision=expected_revision)
            replaced = [name for name, value in (("tags", tags), ("aliases", aliases)) if value is not None]
            legacy_edits = operations.replaced_regions(self.store, path, text, replaced)
            trace.edit(legacy_edits)
            for start, end, replacement in reversed(legacy_edits):
                text = text[:start] + replacement + text[end:]
            text = format.patch(text, fields=frontmatter_patch, tags=tags, aliases=aliases, uid=document["metadata"].get("uid") if frontmatter_patch is not None else None, actor=actor if frontmatter_patch is not None else None)
            trace.metadata(text)
            management = operations.management(self.store, path, text, replaced=replaced)
            self.validate_links(path, text, candidate=text, managed_metadata=json.loads(management["metadata"]) if management else {})
            context = operations.publication(task, self.store, document, item_doc)
            return self.history.write_locked(path, text.encode(), expected_revision, actor, reason, management, publication=context, operation="restructure", operation_details={"additions": addition_details}, origin_builder=trace.transition(context, allow_seed=True))
