"""One YAML/Markdown projection for indexing, reading and controlled writes.

Markdown tokens supply structure, while all positions point into the original
extracted text. Parsing never modifies the file or certifies its content.
"""

import json
import re
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

import yaml
from markdown_it import MarkdownIt
from markdown_it.rules_core import StateCore
from markdown_it.token import Token

from .bounded_yaml import compose as compose_metadata
from .bounded_yaml import load as load_metadata
from .line_coordinates import LineCoordinates

VERSION = "okf-parser-v11"
_FRONT = re.compile(r"\A\ufeff?---\r?\n(.*?)\r?\n---(?:\r?\n|$)", re.S)
_BLOCK_ID = re.compile(r"\^([A-Za-z0-9-]+)[ \t]*(?=\r\n|\r|\n|$)")
_WIKILINK = re.compile(r"\[\[([^\]\n]+)\]\]")
_markdown = MarkdownIt("commonmark")


def _inline_reference_inputs(children: list[Token]) -> list[tuple[str, str]]:
    """Capture link syntax before escaped/entity text joins literal prose."""
    references: list[tuple[str, str]] = []
    text_parts: list[str] = []
    nonliteral: set[int] = set()
    length = 0

    def flush() -> None:
        nonlocal length
        text = "".join(text_parts)
        position = 0
        while match := _WIKILINK.search(text, position):
            start, end = match.start(), match.end()
            if not nonliteral.intersection((start, start + 1, end - 2, end - 1)):
                references.append(("wikilink", match[1].split("|", 1)[0]))
                position = end
            else:
                # A literal link may follow escaped opening brackets in
                # the same text run; the rejected example cannot hide it.
                position = start + 1
        text_parts.clear()
        nonliteral.clear()
        length = 0

    for child in children:
        if child.type in {"text", "text_special"}:
            if child.type == "text_special":
                nonliteral.update(length + index for index, char in enumerate(child.content) if char in "[]")
            text_parts.append(child.content)
            length += len(child.content)
            continue
        flush()
        if child.type in {"link_open", "image"}:
            resource = child.attrGet("href" if child.type == "link_open" else "src")
            if resource:
                references.append(("markdown_link", resource))
    flush()
    return references


def _reference_inputs(state: StateCore) -> None:
    for token in state.tokens:
        if token.type == "inline":
            token.meta["local_reference_inputs"] = _inline_reference_inputs(token.children or [])


_markdown.core.ruler.before("text_join", "local_reference_inputs", _reference_inputs)
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


def heading_role(heading: str) -> str | None:
    """A parenthetical attribution does not change a standard section's role."""
    label = heading.strip().casefold()
    return _HEADINGS.get(re.sub(r"\s*(?:（[^（）]*）|\([^()]*\))$", "", label).strip())


def unlinked_text(inline: Token) -> str:
    """Visible prose outside links/code, using this parser's inline grammar."""
    parts, linked = [], 0
    for token in inline.children or []:
        if token.type == 'link_open':
            linked += 1
        elif token.type == 'link_close':
            linked -= 1
        elif token.type in {'text', 'text_special'} and not linked:
            parts.append(_WIKILINK.sub('', token.content))
        elif token.type in {'softbreak', 'hardbreak'} and not linked:
            parts.append(' ')
    return ' '.join(''.join(parts).split())


@dataclass
class DocumentFormat:
    fields: dict[str, Any] = field(default_factory=dict)
    body_start: int = 0
    sections: list[dict] = field(default_factory=list)
    headings: list[dict] = field(default_factory=list)
    html_anchors: set[str] = field(default_factory=set)
    html_comments: list[str] = field(default_factory=list)
    block_spans: dict[str, tuple[int, int] | None] = field(default_factory=dict)
    error: str | None = None
    footnotes: list[str] = field(default_factory=list)
    # Owned by this parse of the current text, never cached or persisted.
    # Rendering can mutate tokens (notably image.alt); projections must not
    # treat token attributes as an immutable source representation.
    tokens: list[Token] = field(default_factory=list, repr=False)
    env: dict[str, Any] = field(default_factory=dict, repr=False)
    line_offsets: list[int] = field(default_factory=list, repr=False)

    def title(self, fallback: str) -> str:
        """Project the first actual top-level H1 without rescanning source."""
        return next((heading["title"] for heading in self.headings if heading["level"] == 1 and heading["depth"] == 0), fallback)

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
    offsets.extend(start + end for end in LineCoordinates(text).break_ends)
    if offsets[-1] < start + len(text):
        offsets.append(start + len(text))
    return offsets


def heading_slug(title: str) -> str:
    """Keep the existing Unicode/link spelling used by Wiki heading anchors."""
    title = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", title)
    return "-".join("".join(char for char in title.casefold() if char.isspace() or char in "_-" or unicodedata.category(char)[0] in {"L", "N"}).split())


@dataclass(frozen=True)
class _HTMLContext:
    tag: str
    namespace: str
    integration: bool


class _HTMLAnchors(HTMLParser):
    """Track namespace/text boundaries for comments and anchors, not a DOM."""

    CDATA_CONTENT_ELEMENTS = ("script", "style", "textarea", "title", "xmp", "iframe", "noembed", "noframes")
    _VOID = frozenset("area base basefont bgsound br col embed frame hr img input keygen link meta param source track wbr".split())
    _MATH_TEXT = frozenset({"mi", "mo", "mn", "ms", "mtext"})
    _FOREIGN_BREAKOUT = frozenset((
        "b big blockquote body br center code dd div dl dt em embed h1 h2 h3 h4 h5 h6 head hr i img "
        "li listing menu meta nobr ol p pre ruby s small span strong strike sub sup table tt u ul var"
    ).split())

    def __init__(self) -> None:
        super().__init__()
        self.anchors: set[str] = set()
        self.comments: list[str] = []
        self._elements: list[_HTMLContext] = []

    def _html_content(self, tag: str) -> bool:
        if not self._elements:
            return True
        parent = self._elements[-1]
        return (parent.namespace == "html" or parent.integration
                or (parent.namespace == "math" and (
                    (parent.tag in self._MATH_TEXT and tag not in {"mglyph", "malignmark"})
                    or (parent.tag == "annotation-xml" and tag == "svg"))))

    def _start_element(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        if tag in self._FOREIGN_BREAKOUT or (tag == "font" and any(key in {"color", "face", "size"} for key, _ in attrs)):
            while not self._html_content(tag):
                self._elements.pop()
        namespace = ({"svg": "svg", "math": "math"}.get(tag, "html")
                     if self._html_content(tag) else self._elements[-1].namespace)
        integration = (namespace == "svg" and tag in {"foreignobject", "desc", "title"}) or (
            namespace == "math" and tag == "annotation-xml"
            and any(key == "encoding" and (value or "").lower() in {"text/html", "application/xhtml+xml"} for key, value in attrs))
        if namespace != "html" or tag not in self._VOID:
            self._elements.append(_HTMLContext(tag, namespace, integration))
        self.anchors.update(value for key, value in attrs if key in {"id", "name"} and value is not None)
        return namespace

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start_element(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        namespace = self._start_element(tag, attrs)
        if namespace != "html":
            self._elements.pop()  # Foreign elements honor the self-closing flag.
        elif tag in self.CDATA_CONTENT_ELEMENTS or tag == "plaintext":
            # HTML ignores the self-closing flag on non-void text elements.
            self.set_cdata_mode(tag)

    def set_cdata_mode(self, elem: str, **options: Any) -> None:
        # The stdlib selects text mode by tag name. An SVG/MathML element
        # with that name still tokenizes its children as foreign content.
        if self._elements and self._elements[-1].namespace == "html":
            super().set_cdata_mode(elem, **options)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"br", "p"}:
            while not self._html_content(tag):
                self._elements.pop()
        for index in range(len(self._elements) - 1, -1, -1):
            if self._elements[index].tag == tag:
                del self._elements[index:]
                break

    def handle_comment(self, data: str) -> None:
        self.comments.append(data)


def _explicit_locations(result: DocumentFormat, text: str) -> None:
    """Project explicit anchors only from actual HTML and prose tokens."""
    html = _HTMLAnchors()

    def feed(token: Token) -> None:
        # Generated paragraphs/code can leave foreign content too. Feed their
        # tags, but never feed code or escaped text as literal HTML markup.
        if token.type in {"html_block", "html_inline"}:
            html.feed(token.content)
        elif token.type in {"fence", "code_block"}:
            html.feed("<pre><code></code></pre>")
        elif token.type == "code_inline":
            html.feed("<code></code>")
        elif token.nesting:
            html.feed(f"<{'/' if token.nesting == -1 else ''}{token.tag}>")
        elif token.type in {"image", "hr", "hardbreak"}:
            html.feed(f"<{token.tag}>")

    roots = [token for token in result.tokens if token.level == 0 and token.map is not None]
    spans = []
    for index, root in enumerate(roots):
        assert root.map is not None
        start = result.line_offsets[root.map[0]]
        following = roots[index + 1].map if index + 1 < len(roots) else None
        limit = result.line_offsets[following[0]] if following is not None else len(text)
        end = result.line_offsets[root.map[1]]
        # Reference definitions have no block tokens. Retain separator
        # whitespace without swallowing such unrelated source between blocks.
        while end < limit and text[end] in " \t\r\n":
            end += 1
        spans.append((start, end))
    root_index = -1
    for token in result.tokens:
        if token.level == 0 and token.map is not None:
            root_index += 1
        feed(token)
        literal_ids = set()
        if token.children and token.map is not None:
            source = text[result.line_offsets[token.map[0]]:result.line_offsets[token.map[1]]]
            literal_ids = {marker[1] for marker in _BLOCK_ID.finditer(source)}
        for index, child in enumerate(token.children or []):
            feed(child)
            if child.type != "text" or root_index < 0:
                continue
            # The marker must end an actual prose line, rather than code,
            # a link label or text followed by more inline markup.
            following_child = token.children[index + 1] if token.children and index + 1 < len(token.children) else None
            if following_child is not None and following_child.type not in {"softbreak", "hardbreak"}:
                continue
            marker = re.search(r"\^([A-Za-z0-9-]+)[ \t]*$", child.content)
            if marker is None or marker[1] not in literal_ids:
                continue
            identifier = "^" + marker[1]
            first = root_index
            if roots[first].type == "paragraph_open" and token.content.strip() == identifier:
                # A standalone ID belongs to the preceding complete prose or
                # list block. It cannot turn a code example into a citation.
                first -= 1
                if first < 0 or roots[first].type not in {"paragraph_open", "heading_open", "bullet_list_open", "ordered_list_open", "blockquote_open"}:
                    continue
                if spans[first][1] != spans[root_index][0]:
                    continue
            span = (spans[first][0], spans[root_index][1])
            result.block_spans[identifier] = None if identifier in result.block_spans else span
    html.close()
    result.html_anchors = html.anchors
    result.html_comments = html.comments


def parse(text: str) -> DocumentFormat:
    match = _FRONT.match(text)
    result = DocumentFormat(body_start=match.end() if match else int(text.startswith("\ufeff")))
    if match:
        try:
            fields = load_metadata(match[1])
            if isinstance(fields, dict) and all(isinstance(key, str) for key in fields):
                result.fields = fields
            elif fields is not None:
                result.error = "Frontmatter must be a mapping; original retained"
        except yaml.YAMLError as exc:
            result.error = f"Invalid or excessive YAML; original retained ({type(exc).__name__})"
    body = text[result.body_start:]
    offsets = result.line_offsets = markdown_line_offsets(body, result.body_start)
    boundaries = [(result.body_start, "body", "")]
    active_role, active_heading, active_level = "body", "", 0
    tokens = result.tokens = _markdown.parse(body, result.env)
    slugs: dict[str, int] = {}
    containers: list[tuple[int, int]] = []
    outlines: dict[int, list[dict]] = {}
    for index, token in enumerate(tokens):
        for child in token.children or []:
            if child.type == "text":
                result.footnotes.extend(label for label in re.findall(r"\[\^([^\]\s]+)\]", child.content) if label not in result.footnotes)
        if token.nesting == -1 and token.type != "heading_close":
            containers.pop()
        if token.map is None:
            continue
        start, end = (offsets[number] for number in token.map)
        if token.type == "heading_open":
            title, level = tokens[index + 1].content, int(token.tag[1:])
            slug = heading_slug(title)
            suffix = slugs.get(slug, 0)
            slugs[slug] = suffix + 1
            scope, section_end = containers[-1] if containers else (-1, len(text))
            peers = outlines.setdefault(scope, [])
            while peers and peers[-1]["level"] >= level:
                peers.pop()["section_end"] = start
            entry = {"start": start, "end": end, "title": title, "level": level, "depth": token.level,
                     "slug": slug + (f"-{suffix}" if suffix else ""), "section_end": section_end}
            result.headings.append(entry)
            peers.append(entry)
        elif token.nesting == 1:
            containers.append((index, end))
        if token.type == "heading_open" and token.level == 0:
            heading = tokens[index + 1].content
            active_heading = heading
            level = int(token.tag[1:])
            selected = heading_role(heading)
            if selected is not None:
                active_role, active_level = selected, level
            elif not active_level or level <= active_level:
                active_role, active_level = "body", level
            boundaries.append((start, active_role, active_heading))
        elif token.type in {"fence", "code_block"}:
            boundaries.extend(((start, "example", active_heading), (end, active_role, active_heading)))
    ordered = sorted({start: (role, heading) for start, role, heading in boundaries}.items())
    for index, (start, (role, heading)) in enumerate(ordered):
        end = ordered[index + 1][0] if index + 1 < len(ordered) else len(text)
        if text[start:end].strip():
            result.sections.append({"start": start, "end": end, "role": role, "title": heading})
    _explicit_locations(result, text)
    return result


def _yaml_patch(source, updates, newline):
    """Patch selected mapping entries, keeping other YAML spelling/comments."""
    from .store import WikiError

    try:
        node = compose_metadata(source)
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
        values = load_metadata(source)
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
            old_value = load_metadata(source).get(name)
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
