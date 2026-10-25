"""Typed reference inputs: URI tokens and literal file paths have different syntax."""

import posixpath
import re
from dataclasses import asdict, dataclass, replace
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urlsplit
from uuid import UUID


def identity(value):
    if not isinstance(value, str):
        return None
    if re.fullmatch(r"[0-9a-f]{32}", value):
        return "doc", value
    if value.casefold().startswith("urn:uuid:"):
        try:
            return "uid", "urn:uuid:" + str(UUID(value[9:]))
        except ValueError:
            pass
    return None


@dataclass(frozen=True, slots=True)
class ParsedReference:
    relation: str
    raw_resource: str
    namespace: str
    value: str
    fragment: str = ""
    input_type: str = "uri"
    base_policy: str = "source"
    wiki: bool = False
    identities: tuple[tuple[str, str], ...] = ()
    diagnostic: str | None = None

    @property
    def selection(self):
        """Exclude locators and raw spelling from cross-version correspondence."""
        return self.relation, self.namespace, self.value, self.base_policy, self.wiki, self.identities

    def record(self):
        return asdict(self)

    @classmethod
    def restore(cls, value):
        return cls(**(value | {"identities": tuple(tuple(item) for item in value.get("identities", []))}))


def uri_reference(resource, relation, *, wiki=False):
    """Only Markdown href/src and wikilink tokens enter this URI boundary."""
    selected = identity(resource)
    if selected:
        return ParsedReference(relation, resource, *selected, input_type="identity",
                               base_policy="identity", identities=(selected,))
    try:
        url = urlsplit(resource)
    except ValueError:
        return ParsedReference(relation, resource, "invalid", "", diagnostic="invalid_uri")
    if url.scheme or url.netloc:
        return ParsedReference(relation, resource, "external", resource, base_policy="external")
    # Do not repeat these decodes in the resolver, anchors or invalidation keys.
    path, fragment = unquote(url.path), unquote(url.fragment)
    value = posixpath.normpath(path) if path else ""
    if "\x00" in path or "\\" in path:
        return ParsedReference(relation, resource, "invalid", value, fragment,
                               diagnostic="invalid_path")
    return ParsedReference(relation, resource, "path" if path else "self", value,
                           fragment, wiki=wiki)


def literal_reference(path, relation, *, raw_resource=None):
    """An API path is a literal root-relative file name, including #, ? and %."""
    invalid = (not path or path.startswith("/") or "\\" in path or "\x00" in path
               or any(part in {"", ".", ".."} for part in path.split("/")))
    return ParsedReference(relation, path if raw_resource is None else raw_resource,
                           "invalid" if invalid else "path", path,
                           input_type="literal_path", base_policy="vault",
                           diagnostic="invalid_path" if invalid else None)


def path_uri(path):
    """Encode a literal vault path when writing a Markdown/wikilink target."""
    return quote(path, safe="/")


def source_reference(source, relation="derived_from"):
    """Portable source fields choose identity explicitly; path is then evidence."""
    resource = source.get("resource")
    doc = source.get("doc")
    path = source.get("path")
    raw = next((value for value in (resource, doc, path) if isinstance(value, str)), "")
    location = source.get("location")
    fragment = location if isinstance(location, str) else ""
    selectors = []
    if doc is not None:
        selected = identity(doc)
        if selected is None:
            return ParsedReference(relation, raw, "unknown", "", fragment,
                                   input_type="legacy", diagnostic="invalid_explicit_identity")
        selectors.append(selected)
    if (selected := identity(resource)) is not None and selected not in selectors:
        selectors.append(selected)
    if selectors:
        namespace, value = selectors[0]
        return ParsedReference(relation, raw, namespace, value, fragment,
                               input_type="identity", base_policy="identity",
                               identities=tuple(selectors))
    if isinstance(resource, str):
        parsed = uri_reference(resource, relation, wiki=True)
        if parsed.namespace in {"external", "invalid"}:
            return replace(parsed, fragment=fragment)
        if isinstance(path, str):
            if path != resource:
                return ParsedReference(relation, raw, "unknown", "", fragment,
                                       input_type="legacy", diagnostic="conflicting_source_paths")
            return replace(literal_reference(path, relation, raw_resource=raw), fragment=fragment)
        # A resource field alone was historically populated by both URI and
        # literal-path writers. Current file existence cannot disambiguate it.
        if any(char in resource for char in "#?%"):
            return ParsedReference(relation, raw, "unknown", "", fragment,
                                   input_type="legacy", diagnostic="untyped_resource_syntax")
        return ParsedReference(relation, raw, parsed.namespace, parsed.value,
                               fragment, input_type="legacy", wiki=True)
    if isinstance(path, str):
        return replace(literal_reference(path, relation), fragment=fragment)
    return ParsedReference(relation, raw, "invalid", "", fragment,
                           input_type="legacy", diagnostic="missing_source_selector")


def local_references(text, *, body_start=None, parsed=None):
    """Preserve link targets; a supplied parse belongs to this exact text."""
    from .format import _markdown, parse

    if body_start is not None:
        stream = _markdown.parse(text[body_start:])
    else:
        stream = (parsed if parsed is not None else parse(text)).tokens
    for token in stream:
        for relation, resource in token.meta.get("local_reference_inputs", []):
            reference = uri_reference(resource, relation, wiki=relation == "wikilink")
            if reference.namespace != "external":
                yield reference


def candidate_keys(reference, parent, source_doc=None):
    """Indexed lookup order, including absent and lower-priority alternatives."""
    if reference.namespace in {"doc", "uid"}:
        return reference.identities or ((reference.namespace, reference.value),)
    if reference.namespace == "self":
        return (("doc", source_doc),) if source_doc is not None else ()
    if reference.namespace != "path":
        return ()
    value = reference.value
    paths = [value] if reference.base_policy == "vault" else [posixpath.normpath(posixpath.join(parent, value))]
    if reference.wiki and reference.base_policy != "vault":
        paths.append(value.lstrip("/"))
    result = []
    for path in paths:
        if path in {"", ".", ".."} or path.startswith(("../", "/")):
            continue
        for suffix in ("", ".md", ".markdown"):
            key = "path", path + suffix
            if key not in result:
                result.append(key)
    if reference.wiki and "/" not in value:
        name = PurePosixPath(value).name
        for suffix in (".md", ".markdown"):
            if name.casefold().endswith(suffix):
                name = name[:-len(suffix)]
                break
        result.append(("name", name))
    return tuple(result)
