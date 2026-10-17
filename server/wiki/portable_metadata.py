"""Untrusted portable comments supply data, never extractor/index state."""

import json

VERSION = 1
_STRINGS = {"title", "description", "type", "status", "resource", "uid"}
_KINDS = {"original", "source", "concept", "topic", "submission"}
_REF_FIELDS = {"resource", "doc", "path", "revision", "location", "id"}


def validate(value):
    if not isinstance(value, dict):
        raise ValueError("Metadata must be an object")
    # Also excludes NaN/Infinity and guards downstream serialization.
    json.dumps(value, allow_nan=False)
    result = {}
    for name, item in value.items():
        if name in _STRINGS:
            valid = isinstance(item, str)
        elif name == "kind":
            valid = isinstance(item, str) and item in _KINDS
        elif name in {"tags", "aliases"}:
            valid = isinstance(item, list) and all(isinstance(part, str) for part in item)
        elif name == "sources":
            valid = isinstance(item, list) and all(
                isinstance(ref, dict)
                and any(isinstance(ref.get(key), str) and ref[key] for key in ("resource", "doc", "path"))
                and all(key in _REF_FIELDS and isinstance(part, str) for key, part in ref.items())
                for ref in item
            )
        elif name in {"x_wiki", "generated"}:
            fields = {"profile", "body_dialect", "representation_of"} if name == "x_wiki" else {"by", "at"}
            valid = isinstance(item, dict) and all(key in fields and isinstance(part, str) for key, part in item.items())
        elif name == "verified":
            valid = isinstance(item, list) and all(isinstance(part, dict) for part in item)
        else:
            # Unknown extensions remain in the original comment; they cannot
            # forge parsed offsets, extraction quality, identity or diagnostics.
            continue
        if not valid:
            raise ValueError(f"Invalid portable metadata field: {name}")
        result[name] = item
    return result
