"""In-place article adaptation by insertion, never by body regeneration."""

import json

from . import format, material_proof, operations
from .formats import require_markdown_maintenance
from .references import path_uri
from .sections import top_offset
from .store import WikiError


def organize(curation, path, expected_revision, key_note, tags, aliases, relations, sources, actor, reason, fields, task=None, item_doc=None, reuse=False):
    from .tasks import tasks

    store = curation.store
    with curation.history.locked():
        tasks.require_path(task, store, path)
        tasks.require_read(task, store, path, expected_revision)
        source = store.read(path, length=1, expected_revision=expected_revision)
        require_markdown_maintenance(source["path"])
        destination = path
        try:
            text = store.path(path).read_bytes().decode("utf-8")
        except UnicodeError as exc:
            raise WikiError("Non-UTF-8 originals require encoding review", 422) from exc
        output_revision = expected_revision
        trace = material_proof.Trace(text)
        replaced = [name for name, value in (("tags", tags), ("aliases", aliases), ("sources", sources)) if value is not None]
        edits = operations.replaced_regions(store, path, text, replaced)
        trace.edit(edits)
        for start, end, replacement in reversed(edits):
            text = text[:start] + replacement + text[end:]
        newline = "\r\n" if "\r\n" in text else "\n"
        parsed = format.parse(text)
        article = fields.get("x_wiki", {}).get("profile") == "article-v1"
        existing = any(part["role"] == "key_note" for part in parsed.sections)
        if key_note and existing:
            raise WikiError("Keep the existing Key note; use revise for a specific correction", 422)
        if key_note and reuse:
            raise WikiError("Choose new Key note or reuse", 422)
        if article and not existing and not key_note:
            raise WikiError("Read and supply the missing Key note; do not claim an absent summary was reused", 422)
        offset = top_offset(text, parsed=parsed)
        inserts = ""
        if article and offset == parsed.body_start:
            inserts += "# " + (fields.get("title") or source["title"]) + newline * 2
        if not existing and key_note:
            inserts += newline + "## Key note" + newline * 2 + key_note.strip().replace("\r\n", "\n").replace("\n", newline) + newline * 2
        if article and not any(part["title"] == "正文" for part in parsed.sections):
            if existing:
                raise WikiError("Use explicit raw layout changes to separate existing Key note and original body", 422)
            inserts += "## 正文" + newline * 2
        candidate = text[:offset] + inserts + text[offset:]
        trace.edit([(offset, offset, inserts)])
        refs = None
        if sources is not None:
            for ref in sources:
                tasks.require_source_read(task, store, ref)
            refs = curation.sources(sources)
        if relations:
            for relation in relations:
                tasks.require_source_read(task, store, relation)
            linked = curation.sources(relations)
            related = newline * 2 + "## 相关笔记" + newline * 2 + newline.join(f"- [[{path_uri(ref['path'])}]]：{relation['explanation']}" for ref, relation in zip(linked, relations, strict=True)) + newline
            trace.edit([(len(candidate), len(candidate), related)])
            candidate += related
            refs = (refs if refs is not None else source["metadata"].get("sources", [])) + linked
            if "sources" not in replaced:
                replaced.append("sources")
        candidate = format.patch(candidate, fields=fields, tags=tags, aliases=aliases, sources=refs, uid=source["metadata"].get("uid"), actor=actor)
        context = operations.publication(task, store, source, item_doc)
        trace.metadata(candidate)
        management = operations.management(store, destination, candidate, replaced=replaced)
        curation.validate_links(destination, candidate, candidate=candidate, managed_metadata=json.loads(management["metadata"]) if management else {})
        result = curation.history.write_locked(destination, candidate.encode(), output_revision, actor, reason, management, publication=context, operation="normalize", origin_builder=trace.transition(context, allow_seed=True))
        return result | {"source_path": path, "original_preserved": True}
