"""Credential masking for detailed local logs and bounded UI diagnostics."""

import re
import traceback
from typing import Literal

_SECRET_NAMES = r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|token)"
_SECRETS = re.compile(
    r"(?i)(authorization[\s\"']*[:=][\s\"']*(?:bearer\s+)?|"
    + _SECRET_NAMES + r"[\s\"']*[:=][\s\"']*)([^\s\"'&,}\]]+)"
)
_QUOTED_SECRET = re.compile(
    r"(?is)(" + _SECRET_NAMES + r"[\s\"']*[:=]\s*)([\"'])(.*?)\2"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_KEY = re.compile(r"\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{8,}")
_URL_AUTH = re.compile(r"(https?://)[^\s/@]+:[^\s/@]+@")
# Until a URL authority closes, its text may still become userinfo. Retaining
# only a fixed suffix would publish a long password before its trailing @.
_OPEN_URL_AUTHORITY = re.compile(r"https?://[^\s/@]*$")
_OPEN_SECRET_FIELD = re.compile(
    r"(?i)(?P<name>authorization|" + _SECRET_NAMES + r"|\bbearer)"
    r"[\s\"']*(?P<separator>[:=])?[\s\"']*$"
)
_QUOTED_START = re.compile(
    r"(?is)" + _SECRET_NAMES + r"[\s\"']*[:=]\s*([\"'])"
)


class DiagnosticStream:
    """Bounded masking before stderr is retained or sent to the existing log.

    Keep enough raw overlap for captured secrets and field prefixes. Long
    credential values are masked once, then drained until their delimiter;
    ordinary long lines continue to be logged in full in bounded pieces.
    """

    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        self.secrets = tuple(secret for secret in secrets if secret)
        self._keep = max([256, *(len(secret) for secret in self.secrets)])
        self._buffer = ""
        self._continuation: re.Pattern[str] | None = None
        self._field_phase: Literal["separator", "value"] | None = None

    def feed(self, text: str, *, final: bool = False) -> list[str]:
        if self._field_phase is not None:
            text, self._buffer = (self._buffer + text).lstrip(), ""
            if self._field_phase == "separator":
                text = re.sub(r"^[\s\"']*", "", text)
                if not text:
                    return []
                self._field_phase = "value" if text[0] in ":=" else None
                if self._field_phase is not None:
                    text = text[1:].lstrip()
            if self._field_phase == "value":
                # An Authorization value may start with Bearer across reads.
                # Only that six-character prefix is retained while undecided.
                if not final and "bearer".startswith(text.lower()):
                    self._buffer = text
                    return []
                if text[:6].lower() == "bearer" and len(text) > 6 and text[6].isspace():
                    text = text[6:].lstrip()
                if not text:
                    return []
                if text[0] in "\"'":
                    self._continuation = re.compile(f"[^{re.escape(text[0])}]*")
                    text = text[1:]
                else:
                    self._continuation = re.compile(r"[^\s\"'&,}\]]*")
                self._field_phase = None
        if self._continuation is not None:
            consumed = self._continuation.match(text)
            assert consumed is not None
            text = text[consumed.end():]
            if not text and not final:
                return []
            self._continuation = None
        self._buffer += text
        output = []
        while self._buffer:
            size = len(self._buffer)
            if not final and size <= self._keep:
                break
            cut = size if final else min(8192, size - self._keep)
            newline = self._buffer.rfind("\n", 0, cut)
            if newline >= 0:
                cut = newline + 1
            spans: list[tuple[int, int, re.Pattern[str] | None, Literal["separator", "value"] | None]] = []
            for secret in self.secrets:
                start = self._buffer.find(secret)
                while start >= 0:
                    spans.append((start, start + len(secret), None, None))
                    start = self._buffer.find(secret, start + len(secret))
            for match in _QUOTED_START.finditer(self._buffer):
                quote = match[1]
                end = self._buffer.find(quote, match.end())
                spans.append((match.start(), end + 1 if end >= 0 else size,
                              re.compile(f"[^{re.escape(quote)}]*") if end < 0 else None, None))
            for match in _OPEN_SECRET_FIELD.finditer(self._buffer):
                phase: Literal["separator", "value"] = (
                    "value" if match["separator"] or match["name"].lower() == "bearer" else "separator")
                spans.append((match.start(), size, None, phase))
            for pattern, group, token_chars in (
                (_SECRETS, 0, r"[^\s\"'&,}\]]*"),
                (_BEARER, 0, r"[A-Za-z0-9._~+/=-]*"),
                (_KEY, 0, r"[A-Za-z0-9_-]*"),
                (_URL_AUTH, 0, ""),
                (_OPEN_URL_AUTHORITY, 0, r"[^\s/@]*"),
            ):
                for match in pattern.finditer(self._buffer):
                    start, end = match.span(group)
                    spans.append((start, end, re.compile(token_chars) if end == size and token_chars else None, None))
            crossing = [span for span in spans if span[0] < cut < span[1]]
            unfinished = [span[0] for span in spans if span[1] == size and (span[2] is not None or span[3] is not None)]
            if final and unfinished:
                output.append(diagnostic_text(self._buffer[:min(unfinished)], secrets=self.secrets) + "[redacted]")
                self._buffer = ""
                break
            if crossing:
                start, end, continuing, field_phase = min(crossing, key=lambda span: (span[0], -span[1]))
                if start:
                    cut = start
                elif end == size and (continuing is not None or field_phase is not None):
                    output.append("[redacted]")
                    self._continuation = continuing
                    self._field_phase = field_phase
                    self._buffer = ""
                    break
                else:
                    cut = end
            output.append(diagnostic_text(self._buffer[:cut], secrets=self.secrets))
            self._buffer = self._buffer[cut:]
        return output


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
