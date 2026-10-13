"""In-place article adaptation by insertion, never by body regeneration."""

from pathlib import Path
from urllib.parse import quote

from . import format, operations
from .sections import top_offset
from .store import WikiError, is_maintainable_text


def organize(curation, path, expected_revision, key_note, tags, aliases, relations, sources, actor, reason, fields, task=None, item_doc=None, reuse=False):
    from .tasks import tasks

    store = curation.store
    with curation.history.locked():
        tasks.require_path(task, store, path)
        tasks.require_read(task, store, path, expected_revision)
        source = store.read(path, length=1, expected_revision=expected_revision)
        if not is_maintainable_text(source["path"]):
            raise WikiError("Excluded assets are not articles", 422)
        destination = path
        if Path(path).suffix.lower() not in {".md", ".markdown"}:
            destination = path + ".md"
            tasks.require_path(task, store, destination)
            if store.path(destination).exists():
                raise WikiError("The reading page already exists; maintain its current blocks", 409)
            with store.connect() as db:
                extracted = db.execute("SELECT text FROM documents WHERE id=?", (source["id"],)).fetchone()[0]
            text = f"# {source['title']}\n\n[原文]({quote(Path(path).name)})\n\n" + extracted
            output_revision = None
        else:
            try:
                text = store.path(path).read_bytes().decode("utf-8")
            except UnicodeError as exc:
                raise WikiError("Non-UTF-8 originals require encoding review", 422) from exc
            output_revision = expected_revision
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
        offset = top_offset(text)
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
        refs = None
        if sources is not None:
            for ref in sources:
                tasks.require_source_read(task, store, ref)
            refs = [{"resource": ref["path"], "revision": ref["revision"], "location": ref["location"]} for ref in curation.sources(sources)]
        if destination != path:
            refs = [*(refs or []), {"resource": source["path"], "revision": source["revision"], "location": "原文全文"}]
        if relations:
            for relation in relations:
                tasks.require_source_read(task, store, relation)
            linked = curation.sources(relations)
            candidate += newline * 2 + "## 相关笔记" + newline * 2 + newline.join(f"- [[{ref['path']}]]：{relation['explanation']}" for ref, relation in zip(linked, relations, strict=True)) + newline
        curation.validate_links(destination, inserts, candidate=candidate)
        if relations:
            curation.validate_links(destination, candidate[candidate.rfind("## 相关笔记"):], candidate=candidate)
        candidate = format.patch(candidate, fields=fields, tags=tags, aliases=aliases, sources=refs, uid=source["metadata"]["uid"] if destination == path else None, actor=actor, representation_of=source["id"] if destination != path else None)
        context = operations.publication(task, store, source, item_doc)
        result = curation.history.write_locked(destination, candidate.encode(), output_revision, actor, reason, operations.management(store, destination, candidate), publication=context, operation="normalize")
        return result | {"source_path": path, "original_preserved": True}
