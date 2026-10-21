"""Credential masking for detailed local logs and bounded UI diagnostics."""

import re
import traceback

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


def diagnostic_text(text: str, *, secrets: tuple[str, ...] = ()) -> str:
    """Mask credential values without shortening or flattening local detail."""
    value = text
    for secret in sorted(set(secrets) - {""}, key=len, reverse=True):
        value = value.replace(secret, "[redacted]")
    value = _QUOTED_SECRET.sub(r"\1\2[redacted]\2", value)
    value = _SECRETS.sub(r"\1[redacted]", value)
    value = _BEARER.sub("Bearer [redacted]", value)
    value = _KEY.sub("[redacted]", value)
    return _URL_AUTH.sub(r"\1[redacted]@", value)


def exception_diagnostic(error: BaseException, *, secrets: tuple[str, ...] = ()) -> str:
    """Preserve the standard traceback and exception chain, masking credentials."""
    return diagnostic_text("".join(traceback.format_exception(error)), secrets=secrets)


def safe_diagnostic(text: str | None, *, limit: int = 2000) -> str | None:
    """Keep the existing bounded, control-character-free UI diagnostic contract."""
    if not text:
        return None
    value = diagnostic_text(text)
    value = "".join(c for c in value if c >= " " or c in "\n\t").strip()
    return value[:limit] + ("…" if len(value) > limit else "")
