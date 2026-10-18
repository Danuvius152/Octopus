"""Sparse, revision-checked changes and truthful no-change confirmations."""

import json
import unicodedata

from . import format, material_proof, operations, raw_blocks
from .formats import require_markdown_maintenance
from .store import WikiError


def _require_local_corrections(text, parts, corrected_ranges):
    parsed = format.parse(text)
    for locator, corrected in corrected_ranges.items():
        part = parts[locator]
        headings = [(max(0, heading["start"] - part["start"]), min(len(part["content"]), heading["end"] - part["start"])) for heading in parsed.headings if heading["start"] < part["end"] and heading["end"] > part["start"]]
        # A requested title correction has no prose in its own Markdown
        # block. Only real parsed headings qualify, never fenced examples.
        if all(any(start <= first <= last <= end for start, end in headings) for first, last in corrected):
            continue
        untouched = list(part["content"])
        masked = [(0, max(0, min(len(untouched), parsed.body_start - part["start"]))), *headings, *corrected]
        for first, last in masked:
            untouched[first:last] = [" "] * (last - first)
        if not any(unicodedata.category(char)[0] in {"L", "N"} for char in untouched):
            raise WikiError("Correct the actual words within their block; whole-block rewriting is not a targeted revision", 422)


def revise(curation, body, actor, task):
    from .tasks import tasks

    store = curation.store
    with curation.history.locked():
        document, text, parts = raw_blocks.load(store, body.doc, body.expected_revision)
        path = document["path"]
        require_markdown_maintenance(path)
        tasks.require_path(task, store, path)
        context = operations.publication(task, store, document, body.item_doc)
        if body.operation == "no_change":
            tasks.require_read(task, store, path, body.expected_revision)
            with store.connect() as db:
                receipt = operations.record(db, doc_id=document["id"], path=path, before=document["revision"], after=document["revision"], operation="no_change", reason=body.reason, publication=context, store=store)
            return {"id": document["id"], "path": path, "revision": document["revision"], "changed": False, "change_id": None, "receipt_id": receipt, "operation": "no_change", **store.index_state()}
        changes, fields, tags, aliases, refs = [], None, None, None, None
        candidate = text
        trace = material_proof.Trace(text)
        if body.operation == "confirm_sources":
            tasks.require_read(task, store, path, body.expected_revision)
            refs = [dict(ref) for ref in document["metadata"].get("sources", [])]
            dependencies = document["metadata"].get("source_refs", [])
            seen = set()
            for check in body.checks:
                matched = [index for index, ref in enumerate(refs) if (ref.get("resource") or ref.get("doc") or ref.get("path")) == check.resource]
                dependency = next((ref for ref in dependencies if ref["resource"] == check.resource), None)
                current = store.read(check.doc, length=1, expected_revision=check.revision)
                if len(matched) != 1 or not dependency or current["id"] != dependency["doc"] or matched[0] in seen:
                    raise WikiError("Confirm one existing direct dependency at a time; it cannot be rebound", 422)
                seen.add(matched[0])
                tasks.require_source_read(task, store, check.model_dump())
                refs[matched[0]]["revision"] = check.revision
                refs[matched[0]]["location"] = check.location
        else:
            by_locator = {part["locator"]: part for part in parts}
            intervals = []
            corrected_ranges: dict[str, list[tuple[int, int]]] = {}
            for edit in body.edits:
                part = by_locator.get(edit.locator)
                if part is None or part["sha256"] != edit.expected_block_hash:
                    raise WikiError("Raw block locator/hash changed or belongs to another document", 409)
                if part["start"] + edit.start < document["raw_body_start"]:
                    raise WikiError("Use frontmatter_patch for supported metadata; raw YAML edits are not allowed", 422)
                if not 0 <= edit.start <= edit.end <= len(part["content"]) or part["content"][edit.start:edit.end] != edit.expected_text:
                    raise WikiError("Expected raw words/range do not match", 409)
                if edit.kind in {"addition", "annotation"} and (edit.start != edit.end or edit.expected_text):
                    raise WikiError("Additions and annotations must insert without replacing words", 422)
                if edit.expected_text == edit.replacement:
                    raise WikiError("An empty edit is not no_change", 422)
                if edit.kind == "correction":
                    corrected_ranges.setdefault(edit.locator, []).append((edit.start, edit.end))
                start, end = part["start"] + edit.start, part["start"] + edit.end
                if start == 0 and end == len(text):
                    raise WikiError("Whole-document replacement is not a targeted correction", 422)
                tasks.require_raw_read(task, document, max(part["start"], start - 64), min(part["end"], end + 64))
                intervals.append((start, end, edit.replacement))
                changes.append({"locator": edit.locator, "start": start, "end": end, "expected_text": edit.expected_text, "replacement": edit.replacement, "kind": edit.kind})
            _require_local_corrections(text, by_locator, corrected_ranges)
            ordered = sorted(intervals)
            for previous, current in zip(ordered, ordered[1:], strict=False):
                if current[0] < previous[1] or current[0] == previous[0]:
                    raise WikiError("Edits must not overlap or share an insertion point", 422)
            for start, end, replacement in reversed(ordered):
                candidate = candidate[:start] + replacement + candidate[end:]
            trace.edit(ordered)
            fields, tags, aliases = body.frontmatter_patch, body.tags, body.aliases
            if fields is not None or tags is not None or aliases is not None:
                tasks.require_read(task, store, path, body.expected_revision)
            if body.sources is not None:
                refs = []
                for source in body.sources:
                    tasks.require_source_read(task, store, source.model_dump())
                refs = curation.sources([ref.model_dump() for ref in body.sources])
        if refs == document["metadata"].get("sources"):
            refs = None
        replaced = [name for name, value in (("tags", tags), ("aliases", aliases), ("sources", refs)) if value is not None]
        legacy_edits = operations.replaced_regions(store, path, candidate, replaced)
        trace.edit(legacy_edits)
        for start, end, replacement in reversed(legacy_edits):
            candidate = candidate[:start] + replacement + candidate[end:]
        candidate = format.patch(candidate, fields=fields, tags=tags, aliases=aliases, sources=refs, uid=document["metadata"].get("uid") if fields is not None else None, actor=actor if fields is not None else None)
        trace.metadata(candidate)
        management = operations.management(store, path, candidate, replaced=replaced)
        curation.validate_links(path, candidate, candidate=candidate, managed_metadata=json.loads(management["metadata"]) if management else {})
        allow_seed = body.operation == "confirm_sources" or any(value is not None for value in (fields, tags, aliases))
        result = curation.history.write_locked(path, candidate.encode(), document["revision"], actor, body.reason, management, publication=context, operation=body.operation, operation_details={"changed_ranges": changes}, origin_builder=trace.transition(context, allow_seed=allow_seed))
        return result | {"id": document["id"], "changed_ranges": changes, "original_preserved": False if changes else True, "operation": body.operation}
