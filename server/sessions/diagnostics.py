"""Bounded, safe human-readable diagnostics; raw data stays in classifiers."""

import re

_SECRETS = re.compile(
    r"(?i)(authorization[\s\"']*[:=][\s\"']*(?:bearer\s+)?|"
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|token)"
    r"[\s\"']*[:=][\s\"']*)([^\s\"'&,}\]]+)"
)
_QUOTED_SECRET = re.compile(
    r"(?is)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|token)"
    r"[\s\"']*[:=]\s*)([\"'])(.*?)\2"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_KEY = re.compile(r"\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{8,}")
_URL_AUTH = re.compile(r"(https?://)[^\s/@]+:[^\s/@]+@")


def safe_diagnostic(text: str | None, *, limit: int = 2000) -> str | None:
    if not text:
        return None
    value = _QUOTED_SECRET.sub(r"\1\2[redacted]\2", text)
    value = _SECRETS.sub(r"\1[redacted]", value)
    value = _BEARER.sub("Bearer [redacted]", value)
    value = _KEY.sub("[redacted]", value)
    value = _URL_AUTH.sub(r"\1[redacted]@", value)
    value = "".join(c for c in value if c >= " " or c in "\n\t").strip()
    return value[:limit] + ("…" if len(value) > limit else "")
