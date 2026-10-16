"""One YAML/Markdown projection for indexing, reading and controlled writes.

Markdown tokens supply structure, while all positions point into the original
extracted text. Parsing never modifies the file or certifies its content.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any

import yaml
from markdown_it import MarkdownIt

VERSION = "okf-parser-v5"
_FRONT = re.compile(r"\A\ufeff?---\r?\n(.*?)\r?\n---(?:\r?\n|$)", re.S)
_markdown = MarkdownIt("commonmark")
_ROLES = {
    "reference": "reference", "source": "source", "synthesis": "synthesis",
    "concept": "concept", "topic": "navigation", "navigation": "navigation",
    "personalrecord": "personal", "template": "template",
    "source snapshot": "source_snapshot", "sourcesnapshot": "source_snapshot",
}
_HEADINGS = {
    "key note": "key_note", "keynote": "key_note",
    "正文": "body", "相关笔记": "related_notes", "整理说明": "curator_note",
}


class MetadataLoader(yaml.SafeLoader):
    """Keep the source spelling of dates instead of inventing serialization."""


MetadataLoader.add_constructor("tag:yaml.org,2002:timestamp", lambda loader, node: loader.construct_scalar(node))


@dataclass
class DocumentFormat:
    fields: dict[str, Any] = field(default_factory=dict)
    body_start: int = 0
    sections: list[dict] = field(default_factory=list)
    headings: list[dict] = field(default_factory=list)
    error: str | None = None
    footnotes: list[str] = field(default_factory=list)

    def metadata(self, legacy: dict) -> dict:
        result: dict[str, Any] = {"format_version": VERSION, "body_start": self.body_start, "body_sections": self.sections, "footnote_ids": self.footnotes}
        for name in ("type", "title", "description", "status", "resource", "uid"):
            if isinstance(self.fields.get(name), str):
                result[name] = self.fields[name]
        for name in ("generated", "x_wiki"):
            if isinstance(self.fields.get(name), dict):
                try:
                    json.dumps(self.fields[name], allow_nan=False)
                    result[name] = self.fields[name]
                except (ValueError, TypeError, RecursionError):
                    result["frontmatter_error"] = "Non-JSON metadata; original retained"
        for name in ("sources", "verified"):
            value = self.fields.get(name)
            if name == "verified" and isinstance(value, dict):
                value = [value]
            if isinstance(value, list) and all(isinstance(item, dict) for item in value):
                try:
                    json.dumps(value, allow_nan=False)
                    result[name] = value
                except (ValueError, TypeError, RecursionError):
                    result["frontmatter_error"] = "Non-JSON metadata; original retained"
        for name in ("tags", "aliases"):
            value = self.fields.get(name)
            if isinstance(value, str):
                value = [value]
            if isinstance(value, list):
                result[name] = [item for item in value if isinstance(item, str)]
        if "type" in result:
            result["doc_role"] = _ROLES.get(result["type"].casefold(), "unknown")
        else:
            result["doc_role"] = {"source": "source", "concept": "concept", "topic": "navigation"}.get(legacy.get("kind"), "unknown")
        if self.error:
            result["frontmatter_error"] = self.error
        return result


def metadata_end(text: str) -> int:
    """Return the exact frontmatter/BOM prefix without parsing the prose."""
    match = _FRONT.match(text)
    return match.end() if match else int(text.startswith("\ufeff"))


def markdown_line_offsets(text: str, start: int = 0) -> list[int]:
    """Map CommonMark token lines onto unchanged source character offsets."""
    offsets = [start]
    # CommonMark normalizes CRLF/CR to LF; other Unicode separators stay in
    # their original line. Keep offsets in the unmodified source coordinates.
    offsets.extend(start + match.end() for match in re.finditer(r"\r\n|\r|\n", text))
    if offsets[-1] < start + len(text):
        offsets.append(start + len(text))
    return offsets


def parse(text: str) -> DocumentFormat:
    result = DocumentFormat()
    match = _FRONT.match(text)
    if match:
        result.body_start = match.end()
        try:
            fields = yaml.load(match[1], Loader=MetadataLoader)
            if isinstance(fields, dict) and all(isinstance(key, str) for key in fields):
                result.fields = fields
            elif fields is not None:
                result.error = "Frontmatter must be a mapping; original retained"
        except yaml.YAMLError:
            result.error = "Invalid YAML; original retained"
    body = text[result.body_start:]
    offsets = markdown_line_offsets(body, result.body_start)
    boundaries = [(result.body_start, "body", "")]
    active_role, active_heading, active_level = "body", "", 0
    tokens = _markdown.parse(body)
    for index, token in enumerate(tokens):
        for child in token.children or []:
            if child.type == "text":
                result.footnotes.extend(label for label in re.findall(r"\[\^([^\]\s]+)\]", child.content) if label not in result.footnotes)
        if token.map is None:
            continue
        start, end = (offsets[number] for number in token.map)
        if token.type == "heading_open":
            result.headings.append({"start": start, "end": end, "title": tokens[index + 1].content, "level": int(token.tag[1:])})
        if token.type == "heading_open" and token.level == 0:
            heading = tokens[index + 1].content
            level = int(token.tag[1:])
            selected = _HEADINGS.get(heading.casefold())
            if selected is not None:
                active_role, active_heading, active_level = selected, heading, level
            elif not active_level or level <= active_level:
                active_role, active_heading, active_level = "body", heading, level
            boundaries.append((start, active_role, active_heading))
        elif token.type in {"fence", "code_block"}:
            boundaries.extend(((start, "example", active_heading), (end, active_role, active_heading)))
    ordered = sorted({start: (role, heading) for start, role, heading in boundaries}.items())
    for index, (start, (role, heading)) in enumerate(ordered):
        end = ordered[index + 1][0] if index + 1 < len(ordered) else len(text)
        if text[start:end].strip():
            result.sections.append({"start": start, "end": end, "role": role, "title": heading})
    return result


def _yaml_patch(source, updates, newline):
    """Patch selected mapping entries, keeping other YAML spelling/comments."""
    from .store import WikiError

    try:
        node = yaml.compose(source, Loader=MetadataLoader)
    except yaml.YAMLError as exc:
        raise WikiError("Invalid YAML requires review before metadata editing", 422) from exc
    if node is None:
        return yaml.safe_dump(updates, allow_unicode=True, sort_keys=False).replace("\n", newline)
    if not isinstance(node, yaml.MappingNode):
        raise WikiError("Frontmatter must be a mapping", 422)
    names = [key.value for key, _ in node.value]
    if len(names) != len(set(names)):
        raise WikiError("Duplicate metadata keys require review", 422)
    if node.flow_style:
        values = yaml.load(source, Loader=MetadataLoader)
        for name, value in updates.items():
            values[name] = (values.get(name, {}) | value) if isinstance(value, dict) and isinstance(values.get(name), dict) else value
        return yaml.safe_dump(values, allow_unicode=True, sort_keys=False).replace("\n", newline)
    edits, remaining = [], dict(updates)
    for index, (key_node, value_node) in enumerate(node.value):
        name = key_node.value
        if name not in remaining:
            continue
        value = remaining.pop(name)
        start = source.rfind("\n", 0, key_node.start_mark.index) + 1
        end = source.rfind("\n", 0, node.value[index + 1][0].start_mark.index) + 1 if index + 1 < len(node.value) else len(source)
        # YAML aliases can point at an earlier node; changing that alias must
        # never cut or mutate its original anchor definition.
        if isinstance(value, dict) and isinstance(value_node, yaml.MappingNode) and not value_node.flow_style and value_node.start_mark.index >= key_node.end_mark.index:
            inner_start = source.rfind("\n", 0, value_node.start_mark.index) + 1
            nested = source[inner_start:end]
            indent = value_node.start_mark.column
            unindented = "\n".join(line[indent:] if line.strip() else line for line in nested.split("\n"))
            replacement = _yaml_patch(unindented, value, newline)
            replacement = "".join(" " * indent + line if line.strip() else line for line in replacement.splitlines(keepends=True))
            edits.append((inner_start, end, replacement))
        else:
            old_value = yaml.load(source, Loader=MetadataLoader).get(name)
            if isinstance(value, dict) and isinstance(old_value, dict):
                value = old_value | value
            replacement = yaml.safe_dump({name: value}, allow_unicode=True, sort_keys=False).replace("\n", newline)
            edits.append((start, end, replacement))
    for start, end, replacement in reversed(edits):
        source = source[:start] + replacement + source[end:]
    if remaining:
        source = source.rstrip("\r\n") + newline + yaml.safe_dump(remaining, allow_unicode=True, sort_keys=False).replace("\n", newline)
    return source


def patch(text, *, fields=None, tags=None, aliases=None, sources=None, uid=None, actor=None, representation_of=None):
    """The single metadata writer. Missing inputs preserve existing fields."""
    from datetime import UTC, datetime

    from .store import WikiError

    updates = dict(fields or {})
    if set(updates) - {"title", "description", "type", "x_wiki"}:
        raise WikiError("Unsupported frontmatter_patch field", 422)
    for name in ("title", "description", "type"):
        if name in updates and (not isinstance(updates[name], str) or not updates[name].strip()):
            raise WikiError(f"{name} must be a nonempty string", 422)
    extension = updates.get("x_wiki", {})
    if not isinstance(extension, dict) or set(extension) - {"profile", "body_dialect"}:
        raise WikiError("Unsupported x_wiki patch", 422)
    if "profile" in extension and extension["profile"] != "article-v1":
        raise WikiError("Unsupported article profile", 422)
    if "body_dialect" in extension and extension["body_dialect"] not in {"obsidian", "markdown"}:
        raise WikiError("Unsupported body dialect", 422)
    if representation_of is not None:
        updates["x_wiki"] = extension | {"representation_of": representation_of}
    parsed = parse(text)
    if extension.get("profile") == "article-v1":
        titles = {section["title"].casefold() for section in parsed.sections}
        if not {"key note", "正文"}.issubset(titles) or not any(heading["level"] == 1 for heading in parsed.headings):
            raise WikiError("article-v1 requires a title, Key note and 正文", 422)
    for name, value in (("tags", tags), ("aliases", aliases), ("sources", sources)):
        if value is not None:
            updates[name] = value
    if uid is not None and not parsed.fields.get("uid"):
        updates["uid"] = uid
    if actor is not None:
        updates["generated"] = {"by": actor, "at": datetime.now(UTC).isoformat()}
    if not updates:
        return text
    newline = "\r\n" if "\r\n" in text else "\n"
    match = _FRONT.match(text)
    if match:
        source = text[match.start(1):match.end(1)] + newline
        patched = _yaml_patch(source, updates, newline)
        result = text[:match.start(1)] + patched.rstrip("\r\n") + text[match.end(1):]
    else:
        bom = "\ufeff" if text.startswith("\ufeff") else ""
        result = bom + "---" + newline + _yaml_patch("", updates, newline) + "---" + newline + text[len(bom):]
    if parse(result).error:
        raise WikiError("Metadata patch could not preserve valid YAML", 422)
    return result
